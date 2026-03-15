"""
check_app.py  –  Raspberry Pi 4B  |  USB camera  |  Template matching
Assembly sequence:  S1(empty) → S2(product) → S3(+O-ring) → S4(+Hood) → STAMP
GPIO 27 = STAMP button (pull-up, active LOW)
GPIO 17 = relay output (active LOW, 375 ms pulse)

Layout 1280×920
  COL 0 (fixed):  controls bar | workflow panel | STAMP btn | camera 640×480
  COL 1 (flex):   Process Step (S1-S4 horizontal) | Counter | App Log
"""

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import cv2
import json, os, shutil, threading, time, sys
from datetime import datetime, date
from collections import deque

# ── Platform detect ───────────────────────────────────────────────────────────
_IS_PI = os.path.exists("/sys/firmware/devicetree/base/model")

# ── GPIO ─────────────────────────────────────────────────────────────────────
# GPIO_BLOCK_START  ← uncomment on Pi
# try:
#     import RPi.GPIO as GPIO
#     GPIO.setmode(GPIO.BCM)
#     GPIO.setup(27, GPIO.IN,  pull_up_down=GPIO.PUD_UP)
#     GPIO.setup(17, GPIO.OUT, initial=GPIO.HIGH)
#     _HAS_GPIO = True
#     print("[GPIO] initialised OK")
# except Exception as _e:
#     print(f"[GPIO] not available: {_e}")
#     _HAS_GPIO = False
# GPIO_BLOCK_END
_HAS_GPIO = False   # ← remove when deploying on Pi

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR  = os.path.join(BASE_DIR, "template")
STALE_DIR     = os.path.join(BASE_DIR, "template_stale")
HISTORY_DIR   = os.path.join(BASE_DIR, "_history")
ROI_FILE      = os.path.join(BASE_DIR, "roi.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "appsetting.json")
LOG_FILE      = os.path.join(BASE_DIR, "app.log")

for _d in (TEMPLATE_DIR, STALE_DIR, HISTORY_DIR):
    os.makedirs(_d, exist_ok=True)

# ── constants ─────────────────────────────────────────────────────────────────
CAM_W, CAM_H       = 640, 480
CAMERA_FPS         = 12
RELAY_MS           = 375
POST_STAMP_WAIT    = 3.0
DEV_STAMP_COOLDOWN = 3.2
DEFAULT_ROI        = (0, 0, 100, 100)
TMPL_NAMES         = ["s1.png", "s2.png", "s3.png", "s4.png"]
TMPL_LABELS        = ["S1",     "S2",     "S3",     "S4"]
TMPL_KEYS          = ["s1",     "s2",     "s3",     "s4"]
THRESH_MIN         = 50
THRESH_MAX         = 95
MATCH_FREQ_VALUES  = list(range(2, 25))
LOG_MAX_LINES      = 300

STEPS = [
    (0, "Waiting: NO product"),
    (1, "Waiting: Product placed"),
    (2, "Waiting: O-ring placed"),
    (3, "Waiting: Hood placed"),
]
STEP_READY = 4

_S_DEF = {"isRequire": False, "threshold": 75}
DEFAULT_SETTINGS = {
    "isRunning":    False,
    "isDisplayROI": False,
    "isFinalMatch": True,
    "matchFreq":    6,
    "s1": dict(_S_DEF), "s2": dict(_S_DEF),
    "s3": dict(_S_DEF), "s4": dict(_S_DEF),
}

LOG_COLOURS = {
    "INFO":     "#cccccc",
    "OK":       "#44ff88",
    "WARN":     "#ffcc00",
    "ERROR":    "#ff5555",
    "CAMERA":   "#55aaff",
    "MATCH":    "#aaffcc",
    "GPIO":     "#ff99ff",
    "STAMP":    "#ffaa44",
    "STEP":     "#88ddff",
    "SETTINGS": "#aaaaaa",
    "COUNTER":  "#ffdd88",
}


# ══════════════════════════════════════════════════════════════════════════════
#  AppLogger
# ══════════════════════════════════════════════════════════════════════════════
class AppLogger:
    def __init__(self):
        self._queue  = deque()
        self._lock   = threading.Lock()
        self._widget = None
        self._fh     = None
        try:
            self._fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        except Exception:
            pass

    def attach_widget(self, w: tk.Text):
        self._widget = w

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with self._lock:
            self._queue.append((ts, level.upper(), msg))
        if self._fh:
            try:
                self._fh.write(f"[{ts}] [{level:8s}] {msg}\n")
            except Exception:
                pass

    def flush_to_widget(self):
        if not self._widget:
            return
        with self._lock:
            entries = list(self._queue)
            self._queue.clear()
        if not entries:
            return
        w = self._widget
        w.config(state=tk.NORMAL)
        for ts, level, msg in entries:
            tag = level if level in LOG_COLOURS else "INFO"
            w.insert(tk.END, f"[{ts}] ", "TS")
            w.insert(tk.END, f"[{level:8s}]", tag)
            w.insert(tk.END, f" {msg}\n", "MSG")
        lines = int(w.index(tk.END).split(".")[0]) - 1
        if lines > LOG_MAX_LINES:
            w.delete("1.0", f"{lines - LOG_MAX_LINES}.0")
        w.see(tk.END)
        w.config(state=tk.DISABLED)

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass


logger = AppLogger()


# ══════════════════════════════════════════════════════════════════════════════
#  DailyHistory  –  running-time + stamp count per day → _history/yyMMdd.json
# ══════════════════════════════════════════════════════════════════════════════
class DailyHistory:
    """
    Persists per-day counters in _history/yyMMdd.json.
    Fields:  running_seconds (float), stamp_count (int)
    Thread-safe: all writes from Tk main thread only.
    """
    def __init__(self):
        self._today:           str   = ""
        self._running_seconds: float = 0.0
        self._stamp_count:     int   = 0
        self._run_start:       float | None = None   # epoch when Running started
        self._load_today()

    # ── date helpers ──────────────────────────────────────────────────────────
    def _today_key(self) -> str:
        return date.today().strftime("%y%m%d")

    def _history_path(self, key: str) -> str:
        return os.path.join(HISTORY_DIR, f"{key}.json")

    def _load_today(self):
        key = self._today_key()
        self._today = key
        p = self._history_path(key)
        if os.path.exists(p):
            try:
                with open(p) as f:
                    d = json.load(f)
                self._running_seconds = float(d.get("running_seconds", 0))
                self._stamp_count     = int(d.get("stamp_count", 0))
                logger.log(
                    f"History loaded [{key}]: "
                    f"run={self._running_seconds:.0f}s  stamps={self._stamp_count}",
                    "COUNTER"
                )
                return
            except Exception as e:
                logger.log(f"History load error [{key}]: {e}", "WARN")
        self._running_seconds = 0.0
        self._stamp_count     = 0

    def _save(self):
        key = self._today_key()
        # Day rolled over
        if key != self._today:
            logger.log(f"Day changed {self._today} → {key}, resetting counters", "COUNTER")
            self._today           = key
            self._running_seconds = 0.0
            self._stamp_count     = 0
        p = self._history_path(key)
        try:
            with open(p, "w") as f:
                json.dump({
                    "date":             key,
                    "running_seconds":  round(self._running_seconds, 1),
                    "stamp_count":      self._stamp_count,
                    "last_updated":     datetime.now().isoformat(timespec="seconds"),
                }, f, indent=2)
        except Exception as e:
            logger.log(f"History save error: {e}", "ERROR")

    # ── public API (call from Tk main thread) ─────────────────────────────────
    def on_running_start(self):
        self._run_start = time.time()

    def on_running_stop(self):
        if self._run_start is not None:
            self._running_seconds += time.time() - self._run_start
            self._run_start = None
            self._save()

    def tick_running(self):
        """Called every second while running to accumulate live time."""
        if self._run_start is not None:
            # don't update _running_seconds here; just let callers read live value
            pass

    def running_total(self) -> float:
        """Live running seconds including current session."""
        base = self._running_seconds
        if self._run_start is not None:
            base += time.time() - self._run_start
        return base

    def add_stamp(self):
        self._stamp_count += 1
        self._save()
        logger.log(
            f"Stamp #{self._stamp_count} recorded [{self._today}]", "COUNTER"
        )

    @property
    def stamp_count(self) -> int:
        return self._stamp_count

    @staticmethod
    def fmt_duration(seconds: float) -> str:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sc  = divmod(rem, 60)
        if h:
            return f"{h:02d}:{m:02d}:{sc:02d}"
        return f"{m:02d}:{sc:02d}"


# ── camera ────────────────────────────────────────────────────────────────────
def open_camera(index: int = 0):
    candidates = []
    if hasattr(cv2, "CAP_V4L2"):
        candidates.append(("V4L2", lambda: cv2.VideoCapture(index, cv2.CAP_V4L2)))
    candidates.append(("ANY", lambda: cv2.VideoCapture(index)))
    gst = (f"v4l2src device=/dev/video{index} "
           f"! video/x-raw,width={CAM_W},height={CAM_H},framerate={CAMERA_FPS}/1 "
           f"! videoconvert ! video/x-raw,format=BGR "
           f"! appsink max-buffers=1 drop=true")
    candidates.append(("GST", lambda: cv2.VideoCapture(gst, cv2.CAP_GSTREAMER)))

    for name, factory in candidates:
        try:
            cap = factory()
            if not cap.isOpened():
                cap.release()
                logger.log(f"Camera [{name}] isOpened=False", "CAMERA")
                continue
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                logger.log(f"Camera [{name}] read() failed", "CAMERA")
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAM_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
            cap.set(cv2.CAP_PROP_FPS,          CAMERA_FPS)
            w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            logger.log(f"Camera [{name}] OK  {w}x{h} @ {fps:.0f}fps", "CAMERA")
            return cap
        except Exception as e:
            logger.log(f"Camera [{name}] exception: {e}", "CAMERA")
    raise RuntimeError("Cannot open camera. Check USB & /dev/video* permissions.")


# ── settings ──────────────────────────────────────────────────────────────────
def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            merged = dict(DEFAULT_SETTINGS)
            merged.update({k: v for k, v in data.items() if k not in TMPL_KEYS})
            for k in TMPL_KEYS:
                merged[k] = {**_S_DEF, **data.get(k, {})}
            logger.log(f"Settings loaded", "SETTINGS")
            return merged
        except Exception as e:
            logger.log(f"Settings load error: {e}", "WARN")
    logger.log("Using default settings", "SETTINGS")
    return {k: (dict(v) if isinstance(v, dict) else v)
            for k, v in DEFAULT_SETTINGS.items()}


def save_settings(cfg: dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.log(f"Settings save error: {e}", "ERROR")


def load_roi():
    if os.path.exists(ROI_FILE):
        try:
            with open(ROI_FILE) as f:
                roi = tuple(json.load(f)["roi"])
            logger.log(f"ROI loaded: {roi}", "SETTINGS")
            return roi
        except Exception:
            pass
    return DEFAULT_ROI


def save_roi(roi):
    with open(ROI_FILE, "w") as f:
        json.dump({"roi": list(roi)}, f)
    logger.log(f"ROI saved: {roi}", "SETTINGS")


def load_template_pil(name):
    p = os.path.join(TEMPLATE_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return Image.open(p).copy()
    except Exception:
        return None


def load_template_bgr(name):
    p = os.path.join(TEMPLATE_DIR, name)
    if not os.path.exists(p):
        return None
    img = cv2.imread(p, cv2.IMREAD_COLOR)
    if img is None:
        logger.log(f"cv2.imread failed for {name}", "WARN")
    return img


def do_template_match(scene_bgr, tmpl_bgr, roi, threshold_pct) -> tuple[bool, float]:
    """
    Cả scene và template đều là full-frame BGR (640×480).
    Cắt tmpl_bgr theo ROI chính xác → pattern.
    Cắt scene_bgr theo ROI mở rộng 10% → search area (đảm bảo scene >= template).
    Convert cả hai sang gray trước khi matchTemplate.
    """
    if tmpl_bgr is None:
        return False, 0.0
    x, y, w, h = roi
    fh, fw = scene_bgr.shape[:2]

    # template crop – exact ROI
    tx1, ty1 = max(0, x),      max(0, y)
    tx2, ty2 = min(fw, x + w), min(fh, y + h)
    if tx2 <= tx1 or ty2 <= ty1:
        return False, 0.0
    tmpl_crop = cv2.cvtColor(tmpl_bgr[ty1:ty2, tx1:tx2], cv2.COLOR_BGR2GRAY)

    # scene crop – ROI expanded 10%
    px, py = max(1, int(w * 0.10)), max(1, int(h * 0.10))
    sx1 = max(0,  x - px);  sy1 = max(0,  y - py)
    sx2 = min(fw, x + w + px); sy2 = min(fh, y + h + py)
    scene_crop = cv2.cvtColor(scene_bgr[sy1:sy2, sx1:sx2], cv2.COLOR_BGR2GRAY)

    th, tw = tmpl_crop.shape[:2]
    if scene_crop.shape[0] < th or scene_crop.shape[1] < tw:
        return False, 0.0

    res = cv2.matchTemplate(scene_crop, tmpl_crop, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return max_val >= (threshold_pct / 100.0), round(float(max_val), 4)


# ══════════════════════════════════════════════════════════════════════════════
class CheckApp(tk.Tk):
    DISP_W, DISP_H = CAM_W, CAM_H
    TMPL_IMG_W     = 150   # thumbnail max width  in Process Step panel
    TMPL_IMG_H     = 100   # thumbnail max height

    def __init__(self):
        super().__init__()
        self.title("check app")
        self.configure(bg="black")
        self.attributes("-fullscreen", True)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

        cfg = load_settings()

        # ── tk vars ───────────────────────────────────────────────────────────
        self._isRunning    = tk.BooleanVar(value=cfg["isRunning"])
        self._isDisplayROI = tk.BooleanVar(value=cfg["isDisplayROI"])
        self._isFinalMatch = tk.BooleanVar(value=cfg["isFinalMatch"])
        self._matchFreq    = tk.IntVar(value=cfg["matchFreq"])
        self._roi          = list(load_roi())

        self._require_vars = [tk.BooleanVar(value=cfg[k]["isRequire"]) for k in TMPL_KEYS]
        self._thresh_vars  = [tk.IntVar(value=cfg[k]["threshold"])     for k in TMPL_KEYS]

        for v in ([self._isRunning, self._isDisplayROI,
                   self._isFinalMatch, self._matchFreq]
                  + self._require_vars + self._thresh_vars):
            v.trace_add("write", lambda *_: self._save_settings_now())

        # ── template data ─────────────────────────────────────────────────────
        self._tmpl_bgr = []
        for n in TMPL_NAMES:
            g = load_template_bgr(n)
            self._tmpl_bgr.append(g)
            if g is not None:
                logger.log(f"Template loaded: {n}  {g.shape[1]}×{g.shape[0]}", "INFO")
            else:
                logger.log(f"Template NOT found: {n}", "WARN")

        # ── workflow state ────────────────────────────────────────────────────
        self._step           = 0
        self._frame_count    = 0
        self._isReady        = False
        self._busy           = False
        self._status_text    = tk.StringVar(value=STEPS[0][1])
        self._last_dev_stamp = 0.0

        # ── history / counters ────────────────────────────────────────────────
        self._history = DailyHistory()

        # ── ROI drawing ───────────────────────────────────────────────────────
        self._draw_start   = None
        self._draw_rect_id = None

        # ── camera ───────────────────────────────────────────────────────────
        logger.log("Opening camera...", "CAMERA")
        try:
            self._cap = open_camera(0)
        except RuntimeError as e:
            self._cap = None
            logger.log(f"FATAL – camera unavailable: {e}", "ERROR")

        self._frame           = None
        self._lock            = threading.Lock()
        self._running_capture = True
        threading.Thread(target=self._capture_loop, daemon=True).start()

        if _HAS_GPIO:
            threading.Thread(target=self._gpio_poll_loop, daemon=True).start()
            logger.log("GPIO poll thread started", "GPIO")
        else:
            logger.log("GPIO disabled (dev mode)", "GPIO")

        # ── UI widget refs ────────────────────────────────────────────────────
        self._tmpl_photo_refs    = [None] * 4
        self._tmpl_lbl_widgets   = []
        self._thresh_lbl_widgets = []
        self._step_indicators    = []
        self._stamp_btn          = None
        self._log_text           = None
        self._lbl_run_time       = None
        self._lbl_stamp_count    = None

        self._build_ui()

        logger.attach_widget(self._log_text)
        self._poll_log()
        self._update_display()
        self._update_counters()   # start 1-second ticker

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        logger.log(f"App ready  GPIO={_HAS_GPIO}  Pi={_IS_PI}", "INFO")

    # ── log poll ──────────────────────────────────────────────────────────────
    def _poll_log(self):
        logger.flush_to_widget()
        self.after(200, self._poll_log)

    # ── counter tick (every second, Tk main thread) ───────────────────────────
    def _update_counters(self):
        if self._lbl_run_time:
            secs = self._history.running_total()
            self._lbl_run_time.config(
                text=f"Run time:  {DailyHistory.fmt_duration(secs)}"
            )
        if self._lbl_stamp_count:
            self._lbl_stamp_count.config(
                text=f"Stamps:  {self._history.stamp_count}"
            )
        self.after(1000, self._update_counters)

    # ── settings ──────────────────────────────────────────────────────────────
    def _save_settings_now(self):
        cfg = {
            "isRunning":    self._isRunning.get(),
            "isDisplayROI": self._isDisplayROI.get(),
            "isFinalMatch": self._isFinalMatch.get(),
            "matchFreq":    self._matchFreq.get(),
        }
        for i, k in enumerate(TMPL_KEYS):
            cfg[k] = {"isRequire": self._require_vars[i].get(),
                      "threshold": self._thresh_vars[i].get()}
        save_settings(cfg)

    # ══════════════════════════════════════════════════════════════════════════
    #  UI BUILD
    # ══════════════════════════════════════════════════════════════════════════
    def _build_ui(self):
        # Root is fullscreen black canvas.
        # All content lives in a centred 1280×920 inner frame.
        # The surrounding area is pure black.
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self._inner = tk.Frame(self, bg="black",
                               width=1280, height=920)
        self._inner.grid(row=0, column=0)   # centred by default in grid
        self._inner.grid_propagate(False)   # hold fixed 1280×920

        # inner grid: 2 columns, 2 rows (same logic as before)
        self._inner.columnconfigure(0, weight=0)   # col0 fixed
        self._inner.columnconfigure(1, weight=1)   # col1 expands
        self._inner.rowconfigure(0, weight=0)      # process step bar
        self._inner.rowconfigure(1, weight=1)      # main content

        self._build_process_step_row()
        self._build_col0()
        self._build_col1()

    # ── COL 0: controls | workflow | STAMP | camera ───────────────────────────
    def _build_col0(self):
        col0 = tk.Frame(self._inner)
        col0.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        col0.columnconfigure(0, weight=1)
        col0.rowconfigure(0, weight=0)   # controls bar
        col0.rowconfigure(1, weight=0)   # workflow panel + STAMP
        col0.rowconfigure(2, weight=0)   # camera canvas

        # ── row 0: controls bar ───────────────────────────────────────────────
        ctrl = tk.Frame(col0, bd=1, relief=tk.RIDGE)
        ctrl.grid(row=0, column=0, sticky="ew", padx=2, pady=2)

        tk.Checkbutton(ctrl, text="Running",
                       variable=self._isRunning,
                       command=self._on_running_toggle
                       ).pack(side=tk.LEFT, padx=6, pady=4)
        tk.Checkbutton(ctrl, text="Display ROI",
                       variable=self._isDisplayROI
                       ).pack(side=tk.LEFT, padx=4, pady=4)

        self._roi_label = tk.Label(ctrl, text=self._roi_text(),
                                   fg="#cc8800", font=("Courier", 9))
        self._roi_label.pack(side=tk.LEFT, padx=4, pady=4)

        tk.Label(ctrl, text="Match/", font=("Arial", 8)
                 ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Combobox(ctrl, textvariable=self._matchFreq,
                     values=MATCH_FREQ_VALUES, width=3, state="readonly"
                     ).pack(side=tk.LEFT, padx=(0, 2))
        tk.Label(ctrl, text="fr", font=("Arial", 8)
                 ).pack(side=tk.LEFT, padx=(0, 8))

        tk.Checkbutton(ctrl, text="FinalMatch",
                       variable=self._isFinalMatch
                       ).pack(side=tk.LEFT, padx=4, pady=4)

        # ── row 1: workflow panel (2-row, 2-col grid) ───────────────────────────
        #   wf_outer row0: [col0: step indicators] [col1: STAMP button]
        #   wf_outer row1: [col0+1: status label]
        wf_outer = tk.Frame(col0, bd=1, relief=tk.SUNKEN)
        wf_outer.grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        wf_outer.columnconfigure(0, weight=1)
        wf_outer.columnconfigure(1, weight=0)
        wf_outer.rowconfigure(0, weight=0)
        wf_outer.rowconfigure(1, weight=0)

        # wf row0 col0: step indicators
        wf_steps = tk.Frame(wf_outer, bg="#1a1a2e")
        wf_steps.grid(row=0, column=0, sticky="nsew")

        for desc in ["S1 – Empty", "S2 – Product",
                     "S3 – O-ring", "S4 – Hood", "● READY"]:
            lbl = tk.Label(wf_steps, text=desc,
                           font=("Arial", 9, "bold"),
                           bg="#1a1a2e", fg="#444466",
                           anchor="w", padx=8, pady=1)
            lbl.pack(fill=tk.X)
            self._step_indicators.append(lbl)

        # wf row0 col1: STAMP button
        self._stamp_btn = tk.Button(
            wf_outer, text="STAMP",
            font=("Arial", 13, "bold"),
            bg="#222244", fg="#aaaaff",
            activebackground="#3333aa", activeforeground="white",
            relief=tk.RAISED, bd=3, width=10,
            command=self._on_stamp_ui_click,
        )
        self._stamp_btn.grid(row=0, column=1, sticky="nsew", padx=8, pady=6)

        # wf row1: status label spanning both columns
        self._status_lbl = tk.Label(wf_outer,
                                    textvariable=self._status_text,
                                    font=("Arial", 9, "italic"),
                                    fg="#cc8800", anchor="w", bg="#1a1a2e")
        self._status_lbl.grid(row=1, column=0, columnspan=2,
                              sticky="ew", padx=6, pady=2)

        # ── row 2: camera canvas ──────────────────────────────────────────────
        self._canvas = tk.Canvas(col0,
                                 width=self.DISP_W, height=self.DISP_H,
                                 bg="#111111", cursor="crosshair")
        self._canvas.grid(row=2, column=0, padx=2, pady=2)

        if self._cap is None:
            self._canvas.create_text(
                self.DISP_W // 2, self.DISP_H // 2,
                text="⚠ Camera not available\nCheck USB & /dev/video*",
                fill="red", font=("Arial", 14, "bold"), justify=tk.CENTER,
            )

        self._canvas.bind("<ButtonPress-1>",   self._roi_mouse_press)
        self._canvas.bind("<B1-Motion>",       self._roi_mouse_drag)
        self._canvas.bind("<ButtonRelease-1>", self._roi_mouse_release)

        self._update_step_ui()

    # ── _build_process_step_row: full-width row 0 spanning both columns ────────
    def _build_process_step_row(self):
        ps_outer = tk.LabelFrame(self._inner, text="Process Step",
                                 font=("Arial", 9, "bold"),
                                 bd=1, relief=tk.RIDGE)
        ps_outer.grid(row=0, column=0, columnspan=2,
                      sticky="ew", padx=6, pady=(4, 2))
        for c in range(4):
            ps_outer.columnconfigure(c, weight=1)

        for idx, (label, fname) in enumerate(zip(TMPL_LABELS, TMPL_NAMES)):
            s_frame = tk.LabelFrame(ps_outer, text=label,
                                    bd=1, relief=tk.RIDGE,
                                    font=("Arial", 8, "bold"))
            s_frame.grid(row=0, column=idx, sticky="nsew", padx=4, pady=4)
            s_frame.columnconfigure(0, weight=1)
            s_frame.rowconfigure(0, weight=0)   # ctrl_row: Require + slider + Capture
            s_frame.rowconfigure(1, weight=1)   # thumbnail

            # row 0: Require checkbox | threshold label | slider | Capture button
            ctrl_row = tk.Frame(s_frame)
            ctrl_row.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 1))

            tk.Checkbutton(ctrl_row, text="Require",
                           font=("Arial", 8),
                           variable=self._require_vars[idx]
                           ).pack(side=tk.LEFT)

            tv_lbl = tk.Label(ctrl_row,
                              text=f"{self._thresh_vars[idx].get()}%",
                              font=("Courier", 8), fg="#005599", width=4)
            tv_lbl.pack(side=tk.LEFT)
            self._thresh_lbl_widgets.append(tv_lbl)

            tk.Scale(ctrl_row,
                     variable=self._thresh_vars[idx],
                     from_=THRESH_MIN, to=THRESH_MAX,
                     orient=tk.HORIZONTAL,
                     length=120,
                     showvalue=False, resolution=1,
                     command=lambda val, i=idx: self._on_thresh_change(val, i)
                     ).pack(side=tk.LEFT, padx=(2, 4))

            tk.Button(ctrl_row, text=f"Capture {label}",
                      font=("Arial", 8),
                      command=lambda f=fname, i=idx: self._capture_template(f, i)
                      ).pack(side=tk.LEFT, padx=(4, 2))

            # row 1: thumbnail
            img_frame = tk.Frame(s_frame, bg="black",
                                 width=self.TMPL_IMG_W,
                                 height=self.TMPL_IMG_H)
            img_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=(2, 4))
            img_frame.pack_propagate(False)

            pil_img = load_template_pil(fname)
            if pil_img:
                pil_img.thumbnail((self.TMPL_IMG_W, self.TMPL_IMG_H), Image.LANCZOS)
                photo = ImageTk.PhotoImage(pil_img)
                self._tmpl_photo_refs[idx] = photo
                lbl_w = tk.Label(img_frame, image=photo, bg="black")
                lbl_w.pack(expand=True, fill=tk.BOTH)
            else:
                lbl_w = tk.Label(img_frame, text="NO IMAGE",
                                 fg="red", bg="black",
                                 font=("Arial", 10, "bold"))
                lbl_w.pack(expand=True)

            self._tmpl_lbl_widgets.append(lbl_w)

    # ── COL 1: Counter | App Log ──────────────────────────────────────────────
    def _build_col1(self):
        col1 = tk.Frame(self._inner)
        col1.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)
        col1.columnconfigure(0, weight=1)
        col1.rowconfigure(0, weight=0)   # Counter
        col1.rowconfigure(1, weight=1)   # App Log

        # ── row 0: Counter panel ──────────────────────────────────────────────
        cnt_frame = tk.LabelFrame(col1, text="Counter  (today)",
                                  font=("Arial", 9, "bold"),
                                  bd=1, relief=tk.RIDGE)
        cnt_frame.grid(row=0, column=0, sticky="ew", padx=2, pady=(0, 4))
        cnt_frame.columnconfigure(0, weight=1)
        cnt_frame.columnconfigure(1, weight=1)
        cnt_frame.columnconfigure(2, weight=0)

        self._lbl_run_time = tk.Label(
            cnt_frame,
            text="Run time:  00:00",
            font=("Courier", 12, "bold"),
            fg="#88ddff", anchor="w", padx=12, pady=6,
        )
        self._lbl_run_time.grid(row=0, column=0, sticky="w")

        self._lbl_stamp_count = tk.Label(
            cnt_frame,
            text="Stamps:  0",
            font=("Courier", 12, "bold"),
            fg="#ffaa44", anchor="w", padx=12, pady=6,
        )
        self._lbl_stamp_count.grid(row=0, column=1, sticky="w")

        # small date label
        tk.Label(cnt_frame,
                 text=date.today().strftime("%Y-%m-%d"),
                 font=("Arial", 8), fg="#666666"
                 ).grid(row=0, column=2, sticky="e", padx=8)

        # ── row 2: App Log ────────────────────────────────────────────────────
        log_frame = tk.LabelFrame(col1, text="App Log",
                                  font=("Arial", 9, "bold"),
                                  bd=1, relief=tk.RIDGE)
        log_frame.grid(row=1, column=0, sticky="nsew", padx=2, pady=(0, 4))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self._log_text = tk.Text(
            log_frame,
            bg="#0d0d0d", fg="#cccccc",
            font=("Courier", 8),
            state=tk.DISABLED,
            wrap=tk.NONE,
            relief=tk.FLAT, bd=0,
            selectbackground="#334455",
        )
        self._log_text.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                            command=self._log_text.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL,
                            command=self._log_text.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self._log_text.config(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tk.Button(log_frame, text="Clear", font=("Arial", 7),
                  pady=0, padx=4, command=self._clear_log
                  ).grid(row=1, column=1, sticky="e", padx=2, pady=1)

        # configure colour tags
        self._log_text.tag_config("TS",  foreground="#556677")
        self._log_text.tag_config("MSG", foreground="#aaaaaa")
        for tag, colour in LOG_COLOURS.items():
            self._log_text.tag_config(tag, foreground=colour)

    # ── log helpers ───────────────────────────────────────────────────────────
    def _clear_log(self):
        if self._log_text:
            self._log_text.config(state=tk.NORMAL)
            self._log_text.delete("1.0", tk.END)
            self._log_text.config(state=tk.DISABLED)
        logger.log("Log cleared", "INFO")

    # ── step highlight ────────────────────────────────────────────────────────
    def _update_step_ui(self):
        active = self._step if self._step < STEP_READY else STEP_READY
        for i, lbl in enumerate(self._step_indicators):
            if i == active:
                lbl.config(fg="#00ff88", bg="#002222")
            elif i < active:
                lbl.config(fg="#226644", bg="#1a1a2e")
            else:
                lbl.config(fg="#444466", bg="#1a1a2e")

        if not self._isRunning.get():
            self._stamp_btn.config(bg="#1a4a1a", fg="#88ff88", text="STAMP  [dev]")
        elif self._isReady:
            self._stamp_btn.config(bg="#004400", fg="#00ff88", text="STAMP  ✔")
        else:
            self._stamp_btn.config(bg="#222244", fg="#555577", text="STAMP")

    # ── running toggle ────────────────────────────────────────────────────────
    def _on_running_toggle(self):
        if self._isRunning.get():
            self._step        = 0
            self._isReady     = False
            self._busy        = False
            self._frame_count = 0
            self._status_text.set(STEPS[0][1])
            self._history.on_running_start()
            logger.log("▶ Running STARTED – workflow reset to step 0", "STEP")
        else:
            self._isReady = False
            self._history.on_running_stop()
            logger.log("■ Running STOPPED", "STEP")
        self._update_step_ui()

    # ── thresh ────────────────────────────────────────────────────────────────
    def _on_thresh_change(self, val, idx):
        v = int(float(val))
        self._thresh_lbl_widgets[idx].config(text=f"{v}%")
        logger.log(f"Threshold {TMPL_LABELS[idx]} → {v}%", "SETTINGS")

    # ── capture template ──────────────────────────────────────────────────────
    def _capture_template(self, fname, idx):
        if self._isRunning.get():
            logger.log(f"Capture {fname} blocked – app is Running", "WARN")
            return
        with self._lock:
            frame = self._frame.copy() if self._frame is not None else None
        if frame is None:
            logger.log(f"Capture {fname} failed – no camera frame", "ERROR")
            return
        dest = os.path.join(TEMPLATE_DIR, fname)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(fname)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.move(dest, os.path.join(STALE_DIR, f"{stem}_{ts}{ext}"))
            logger.log(f"Archived old {fname} → template_stale/", "INFO")
        cv2.imwrite(dest, frame)
        self._tmpl_bgr[idx] = load_template_bgr(fname)
        logger.log(f"Captured {fname}  {frame.shape[1]}×{frame.shape[0]}", "OK")
        self._refresh_template_ui(fname, idx)

    def _refresh_template_ui(self, fname, idx):
        lbl     = self._tmpl_lbl_widgets[idx]
        pil_img = load_template_pil(fname)
        if pil_img:
            pil_img.thumbnail((self.TMPL_IMG_W, self.TMPL_IMG_H), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pil_img)
            self._tmpl_photo_refs[idx] = photo
            lbl.config(image=photo, text="", bg="black")
        else:
            self._tmpl_photo_refs[idx] = None
            lbl.config(image="", text="NO IMAGE",
                       fg="red", bg="black", font=("Arial", 10, "bold"))

    # ── camera capture thread ─────────────────────────────────────────────────
    def _capture_loop(self):
        interval          = 1.0 / CAMERA_FPS
        consecutive_fails = 0
        logger.log(f"Capture thread started  target={CAMERA_FPS}fps", "CAMERA")

        while self._running_capture:
            t0 = time.time()
            if self._cap is None:
                time.sleep(1.0)
                continue

            ret, frame = self._cap.read()
            if not ret or frame is None:
                consecutive_fails += 1
                if consecutive_fails == 1:
                    logger.log("Camera read() failed", "CAMERA")
                if consecutive_fails >= 10:
                    logger.log("10 consecutive failures – reopening camera", "WARN")
                    self._cap.release()
                    time.sleep(1.0)
                    try:
                        self._cap = open_camera(0)
                        consecutive_fails = 0
                        logger.log("Camera reopened OK", "CAMERA")
                    except RuntimeError as e:
                        logger.log(f"Camera reopen failed: {e}", "ERROR")
                        self._cap = None
                time.sleep(0.1)
                continue

            consecutive_fails = 0
            fh, fw = frame.shape[:2]
            if fw != CAM_W or fh != CAM_H:
                frame = cv2.resize(frame, (CAM_W, CAM_H),
                                   interpolation=cv2.INTER_LINEAR)
            with self._lock:
                self._frame = frame
                self._frame_count += 1

            if self._isRunning.get():
                self._maybe_match()

            sleep_t = interval - (time.time() - t0)
            if sleep_t > 0:
                time.sleep(sleep_t)

    # ── matching ──────────────────────────────────────────────────────────────
    def _maybe_match(self):
        if self._busy or self._step == STEP_READY:
            return
        if (self._frame_count % self._matchFreq.get()) != 0:
            return
        with self._lock:
            frame = self._frame.copy() if self._frame is not None else None
        if frame is None:
            return

        tmpl_idx, _ = STEPS[self._step]
        thresh       = self._thresh_vars[tmpl_idx].get()
        passed, score = do_template_match(frame, self._tmpl_bgr[tmpl_idx],
                                          self._roi, thresh)
        logger.log(
            f"Match {TMPL_LABELS[tmpl_idx]}  score={score:.3f}  "
            f"thresh={thresh}%  → {'PASS' if passed else 'fail'}",
            "MATCH"
        )
        if not passed:
            return

        next_step = self._step + 1
        if next_step >= len(STEPS):
            self._step    = STEP_READY
            self._isReady = True
            logger.log("All steps matched → READY", "STEP")
            self.after(0, self._on_ready)
        else:
            self._step = next_step
            desc = STEPS[next_step][1]
            logger.log(f"Step → {TMPL_LABELS[next_step-1]} matched  next: {desc}", "STEP")
            self.after(0, lambda d=desc: (self._status_text.set(d),
                                          self._update_step_ui()))

    def _on_ready(self):
        self._status_text.set("✔ READY – press STAMP")
        self._update_step_ui()

    # ── GPIO poll ─────────────────────────────────────────────────────────────
    def _gpio_poll_loop(self):
        import RPi.GPIO as GPIO
        last = True
        while self._running_capture:
            cur = GPIO.input(27)
            if last and not cur:
                logger.log("GPIO27 falling edge – STAMP triggered", "GPIO")
                self.after(0, self._on_stamp_physical)
            last = cur
            time.sleep(0.02)

    # ── stamp ─────────────────────────────────────────────────────────────────
    def _on_stamp_ui_click(self):
        logger.log("UI STAMP button clicked", "STAMP")
        self._try_stamp()

    def _on_stamp_physical(self):
        logger.log("Physical STAMP button pressed", "STAMP")
        self._try_stamp()

    def _try_stamp(self):
        if self._busy:
            logger.log("STAMP ignored – already busy", "WARN")
            return
        if self._isRunning.get():
            if not self._isReady:
                logger.log("STAMP ignored – not READY", "WARN")
                return
        else:
            now = time.time()
            if now - self._last_dev_stamp < DEV_STAMP_COOLDOWN:
                rem = DEV_STAMP_COOLDOWN - (now - self._last_dev_stamp)
                self._status_text.set(f"⏳ Cooldown {rem:.1f}s")
                logger.log(f"STAMP ignored – dev cooldown {rem:.1f}s", "WARN")
                return
            self._last_dev_stamp = now
        self._busy = True
        logger.log("STAMP sequence starting...", "STAMP")
        threading.Thread(target=self._stamp_sequence, daemon=True).start()

    def _stamp_sequence(self):
        # optional final match
        if self._isRunning.get() and self._isFinalMatch.get():
            with self._lock:
                frame = self._frame.copy() if self._frame is not None else None
            if frame is not None:
                thresh = self._thresh_vars[3].get()
                passed, score = do_template_match(frame, self._tmpl_bgr[3],
                                                  self._roi, thresh)
                logger.log(
                    f"FinalMatch S4  score={score:.3f}  thresh={thresh}%  "
                    f"→ {'PASS' if passed else 'FAIL'}", "STAMP"
                )
                if not passed:
                    self.after(0, lambda: self._status_text.set(
                        "✘ FinalMatch FAIL – re-check Hood"))
                    self._busy = False
                    return

        self.after(0, lambda: self._status_text.set("⚡ STAMPING..."))
        self.after(0, lambda: self._stamp_btn.config(bg="#553300", fg="white"))

        if _HAS_GPIO:
            import RPi.GPIO as GPIO
            GPIO.output(17, GPIO.LOW)
            logger.log(f"Relay ON  pulse={RELAY_MS}ms", "GPIO")
            time.sleep(RELAY_MS / 1000.0)
            GPIO.output(17, GPIO.HIGH)
            logger.log("Relay OFF", "GPIO")
        else:
            logger.log(f"[sim] Relay pulse {RELAY_MS}ms", "STAMP")
            time.sleep(RELAY_MS / 1000.0)

        # record stamp in history (must be called from Tk thread)
        self.after(0, self._history.add_stamp)

        if self._isRunning.get():
            self.after(0, lambda: self._status_text.set(
                f"⏳ Waiting {POST_STAMP_WAIT:.0f}s..."))
            logger.log(f"Post-stamp wait {POST_STAMP_WAIT:.0f}s", "STAMP")
            time.sleep(POST_STAMP_WAIT)
            self._step    = 0
            self._isReady = False
            self._busy    = False
            logger.log("Cycle complete – reset to step 0", "STEP")
            self.after(0, self._reset_cycle)
        else:
            self._busy = False
            logger.log("Dev stamp complete", "STAMP")
            self.after(0, self._update_step_ui)

    def _reset_cycle(self):
        self._status_text.set(STEPS[0][1])
        self._update_step_ui()

    # ── display loop ──────────────────────────────────────────────────────────
    def _update_display(self):
        with self._lock:
            frame = self._frame.copy() if self._frame is not None else None
        if frame is not None:
            if self._isDisplayROI.get():
                x, y, w, h = self._roi
                cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 255), 1)
            rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(Image.fromarray(rgb))
            self._canvas.create_image(0, 0, anchor=tk.NW, image=photo)
            self._canvas._photo = photo
        self.after(int(1000 / CAMERA_FPS), self._update_display)

    # ── ROI mouse ─────────────────────────────────────────────────────────────
    def _roi_text(self):
        x, y, w, h = self._roi
        return f"ROI: ({x},{y},{w},{h})"

    def _roi_mouse_press(self, event):
        if self._isRunning.get():
            return
        self._draw_start = (event.x, event.y)
        if self._draw_rect_id:
            self._canvas.delete(self._draw_rect_id)
            self._draw_rect_id = None

    def _roi_mouse_drag(self, event):
        if self._isRunning.get() or self._draw_start is None:
            return
        x0, y0 = self._draw_start
        if self._draw_rect_id:
            self._canvas.delete(self._draw_rect_id)
        self._draw_rect_id = self._canvas.create_rectangle(
            x0, y0, event.x, event.y, outline="yellow", width=1)

    def _roi_mouse_release(self, event):
        if self._isRunning.get() or self._draw_start is None:
            return
        x0, y0 = self._draw_start
        x1, y1 = event.x, event.y
        rx, ry = min(x0, x1), min(y0, y1)
        rw, rh = abs(x1 - x0), abs(y1 - y0)
        if rw > 2 and rh > 2:
            self._roi = [rx, ry, rw, rh]
            save_roi(tuple(self._roi))
            self._roi_label.config(text=self._roi_text())
            logger.log(f"ROI updated: ({rx},{ry},{rw},{rh})", "INFO")
        self._draw_start = None
        if self._draw_rect_id:
            self._canvas.delete(self._draw_rect_id)
            self._draw_rect_id = None

    # ── cleanup ───────────────────────────────────────────────────────────────
    def _on_close(self):
        logger.log("App closing...", "INFO")
        if self._isRunning.get():
            self._history.on_running_stop()
        self._save_settings_now()
        self._running_capture = False
        if self._cap is not None:
            self._cap.release()
        if _HAS_GPIO:
            import RPi.GPIO as GPIO
            GPIO.cleanup()
            logger.log("GPIO cleanup done", "GPIO")
        logger.close()
        self.destroy()


if __name__ == "__main__":
    app = CheckApp()
    app.mainloop()
