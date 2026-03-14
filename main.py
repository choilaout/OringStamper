"""
check_app.py  –  Raspberry Pi 4B  |  USB camera  |  Template matching
Assembly sequence:  S1(empty) → S2(product) → S3(+O-ring) → S4(+Hood) → STAMP
GPIO 27 = STAMP button (pull-up, active LOW)
GPIO 17 = relay output (active LOW, 375 ms pulse)

── Camera backend strategy (Pi4 / V4L2) ────────────────────────────────────
On Pi4 the default OpenCV build uses GStreamer which fails for plain USB cams.
We force V4L2 via cv2.CAP_V4L2.  If that also fails we fall back to a
GStreamer pipeline string that works with the standard Pi4 apt OpenCV build.

To deploy:
  sudo apt install python3-opencv python3-pil python3-pil.imagetk python3-rpi.gpio
  pip3 install --break-system-packages opencv-python-headless pillow RPi.GPIO

── GPIO toggle ─────────────────────────────────────────────────────────────
Search for "# GPIO_BLOCK_START" and uncomment to enable on Pi.
"""

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import cv2
import json, os, shutil, threading, time, sys
from datetime import datetime

# ── Platform detect ───────────────────────────────────────────────────────────
_IS_PI = os.path.exists("/sys/firmware/devicetree/base/model")

# ── GPIO ─────────────────────────────────────────────────────────────────────
# GPIO_BLOCK_START  ← uncomment everything between the two markers on Pi
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(27, GPIO.IN,  pull_up_down=GPIO.PUD_UP)   # STAMP button
    GPIO.setup(17, GPIO.OUT, initial=GPIO.HIGH)            # relay (active LOW)
    _HAS_GPIO = True
    print("[GPIO] initialised OK")
except Exception as _e:
    print(f"[GPIO] not available: {_e}")
    _HAS_GPIO = False
GPIO_BLOCK_END
# _HAS_GPIO = False   # ← remove / comment this line when deploying on Pi

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR  = os.path.join(BASE_DIR, "template")
STALE_DIR     = os.path.join(BASE_DIR, "template_stale")
ROI_FILE      = os.path.join(BASE_DIR, "roi.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "appsetting.json")

os.makedirs(TEMPLATE_DIR, exist_ok=True)
os.makedirs(STALE_DIR,    exist_ok=True)

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


# ── camera open helper ────────────────────────────────────────────────────────
def open_camera(index: int = 0):
    """
    Try multiple backends in order until one returns a valid frame.
    Returns an opened cv2.VideoCapture or raises RuntimeError.

    Order tried:
      1. V4L2 explicit  (cv2.CAP_V4L2)          – best for USB on Pi4 + apt-opencv
      2. Any backend    (cv2.CAP_ANY / 0)        – works on Windows / other Linux
      3. GStreamer v4l2src pipeline               – fallback for custom GStreamer builds
    """
    candidates = []

    # 1. V4L2 direct (Pi4, Linux)
    if hasattr(cv2, "CAP_V4L2"):
        candidates.append(
            lambda: cv2.VideoCapture(index, cv2.CAP_V4L2)
        )

    # 2. Default / any backend
    candidates.append(
        lambda: cv2.VideoCapture(index)
    )

    # 3. GStreamer pipeline (Pi4 with gst-python opencv)
    gst_pipe = (
        f"v4l2src device=/dev/video{index} "
        f"! video/x-raw,width={CAM_W},height={CAM_H},framerate={CAMERA_FPS}/1 "
        f"! videoconvert "
        f"! video/x-raw,format=BGR "
        f"! appsink max-buffers=1 drop=true"
    )
    candidates.append(
        lambda: cv2.VideoCapture(gst_pipe, cv2.CAP_GSTREAMER)
    )

    for attempt, factory in enumerate(candidates, 1):
        try:
            cap = factory()
            if not cap.isOpened():
                cap.release()
                print(f"[Camera] attempt {attempt}: isOpened=False, skipping")
                continue
            # verify we can actually read a frame
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                print(f"[Camera] attempt {attempt}: read() failed, skipping")
                continue
            # configure resolution + fps (best-effort; ignored by GST pipeline)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAM_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
            cap.set(cv2.CAP_PROP_FPS,          CAMERA_FPS)
            print(f"[Camera] attempt {attempt}: OK  "
                  f"({int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
                  f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))} "
                  f"@ {cap.get(cv2.CAP_PROP_FPS):.0f}fps)")
            return cap
        except Exception as e:
            print(f"[Camera] attempt {attempt}: exception – {e}")

    raise RuntimeError(
        "Cannot open camera. Check USB connection and /dev/video* permissions.\n"
        "Try:  ls /dev/video*   and   sudo usermod -aG video $USER"
    )


# ── settings helpers ──────────────────────────────────────────────────────────
def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            merged = dict(DEFAULT_SETTINGS)
            merged.update({k: v for k, v in data.items() if k not in TMPL_KEYS})
            for k in TMPL_KEYS:
                merged[k] = {**_S_DEF, **data.get(k, {})}
            return merged
        except Exception:
            pass
    return {k: (dict(v) if isinstance(v, dict) else v)
            for k, v in DEFAULT_SETTINGS.items()}


def save_settings(cfg: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def load_roi():
    if os.path.exists(ROI_FILE):
        try:
            with open(ROI_FILE) as f:
                return tuple(json.load(f)["roi"])
        except Exception:
            pass
    return DEFAULT_ROI


def save_roi(roi):
    with open(ROI_FILE, "w") as f:
        json.dump({"roi": list(roi)}, f)


def load_template_pil(name):
    p = os.path.join(TEMPLATE_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return Image.open(p).copy()
    except Exception:
        return None


def load_template_gray(name):
    p = os.path.join(TEMPLATE_DIR, name)
    if not os.path.exists(p):
        return None
    img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    return img


def do_template_match(scene_bgr, tmpl_gray, roi, threshold_pct) -> bool:
    if tmpl_gray is None:
        return False
    x, y, w, h = roi
    fh, fw = scene_bgr.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(fw, x + w), min(fh, y + h)
    if x2 <= x1 or y2 <= y1:
        return False
    crop = cv2.cvtColor(scene_bgr[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    th, tw = tmpl_gray.shape[:2]
    if crop.shape[0] < th or crop.shape[1] < tw:
        return False
    res = cv2.matchTemplate(crop, tmpl_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return max_val >= (threshold_pct / 100.0)


# ══════════════════════════════════════════════════════════════════════════════
class CheckApp(tk.Tk):
    DISP_W, DISP_H = CAM_W, CAM_H

    def __init__(self):
        super().__init__()
        self.title("check app")
        self.geometry("1280x720")
        self.resizable(False, False)

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
        self._tmpl_gray = [load_template_gray(n) for n in TMPL_NAMES]

        # ── workflow state ────────────────────────────────────────────────────
        self._step           = 0
        self._frame_count    = 0
        self._isReady        = False
        self._busy           = False
        self._status_text    = tk.StringVar(value=STEPS[0][1])
        self._last_dev_stamp = 0.0

        # ── ROI drawing ───────────────────────────────────────────────────────
        self._draw_start   = None
        self._draw_rect_id = None

        # ── camera ───────────────────────────────────────────────────────────
        try:
            self._cap = open_camera(0)
        except RuntimeError as e:
            # Show error in a label and continue without camera
            self._cap = None
            print(f"[Camera] FATAL: {e}", file=sys.stderr)

        self._frame           = None
        self._lock            = threading.Lock()
        self._running_capture = True
        threading.Thread(target=self._capture_loop, daemon=True).start()

        # ── GPIO poll ─────────────────────────────────────────────────────────
        if _HAS_GPIO:
            threading.Thread(target=self._gpio_poll_loop, daemon=True).start()

        # ── UI ───────────────────────────────────────────────────────────────
        self._tmpl_photo_refs    = [None] * 4
        self._tmpl_lbl_widgets   = []
        self._thresh_lbl_widgets = []
        self._step_indicators    = []
        self._stamp_btn          = None
        self._cam_status_lbl     = None   # shows camera error on canvas

        self._build_ui()
        self._update_display()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ════ COL 0 ═══════════════════════════════════════════════════════════
        col0 = tk.Frame(self)
        col0.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        col0.rowconfigure(0, weight=0)
        col0.rowconfigure(1, weight=0)
        col0.rowconfigure(2, weight=0)
        col0.rowconfigure(3, weight=1)

        # top controls bar
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

        # workflow panel
        wf_outer = tk.Frame(col0, bd=1, relief=tk.SUNKEN)
        wf_outer.grid(row=1, column=0, sticky="ew", padx=2, pady=2)

        wf_bg = tk.Frame(wf_outer, bg="#1a1a2e")
        wf_bg.pack(fill=tk.X)

        for desc in ["S1 – Empty", "S2 – Product", "S3 – O-ring", "S4 – Hood", "● READY"]:
            lbl = tk.Label(wf_bg, text=desc,
                           font=("Arial", 9, "bold"),
                           bg="#1a1a2e", fg="#444466",
                           anchor="w", padx=8, pady=1)
            lbl.pack(fill=tk.X)
            self._step_indicators.append(lbl)

        self._status_lbl = tk.Label(wf_outer,
                                    textvariable=self._status_text,
                                    font=("Arial", 9, "italic"),
                                    fg="#cc8800", anchor="w", bg="#1a1a2e")
        self._status_lbl.pack(fill=tk.X, padx=6, pady=2)

        # STAMP button
        self._stamp_btn = tk.Button(
            col0,
            text="STAMP",
            font=("Arial", 13, "bold"),
            bg="#222244", fg="#aaaaff",
            activebackground="#3333aa", activeforeground="white",
            relief=tk.RAISED, bd=3,
            width=12, height=1,
            command=self._on_stamp_ui_click,
        )
        self._stamp_btn.grid(row=2, column=0, sticky="e", padx=10, pady=4)

        # camera canvas
        self._canvas = tk.Canvas(col0,
                                 width=self.DISP_W, height=self.DISP_H,
                                 bg="#111111", cursor="crosshair")
        self._canvas.grid(row=3, column=0, padx=2, pady=2)

        # camera error overlay (shown when _cap is None)
        if self._cap is None:
            self._canvas.create_text(
                self.DISP_W // 2, self.DISP_H // 2,
                text="⚠ Camera not available\nCheck USB & /dev/video*",
                fill="red", font=("Arial", 14, "bold"),
                justify=tk.CENTER,
            )

        self._canvas.bind("<ButtonPress-1>",   self._roi_mouse_press)
        self._canvas.bind("<B1-Motion>",       self._roi_mouse_drag)
        self._canvas.bind("<ButtonRelease-1>", self._roi_mouse_release)

        self._update_step_ui()

        # ════ COL 1 ═══════════════════════════════════════════════════════════
        col1 = tk.Frame(self)
        col1.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        for r in range(4):
            col1.rowconfigure(r, weight=1)
        col1.columnconfigure(0, weight=1)

        for idx, (label, fname) in enumerate(zip(TMPL_LABELS, TMPL_NAMES)):
            s_frame = tk.LabelFrame(col1, text=label, bd=1, relief=tk.RIDGE)
            s_frame.grid(row=idx, column=0, sticky="nsew", padx=4, pady=3)
            s_frame.rowconfigure(0, weight=0)
            s_frame.rowconfigure(1, weight=1)
            s_frame.columnconfigure(0, weight=1)

            cr = tk.Frame(s_frame)
            cr.grid(row=0, column=0, sticky="ew", padx=4, pady=2)

            tk.Button(cr, text=f"Capture {label}",
                      command=lambda f=fname, i=idx: self._capture_template(f, i)
                      ).pack(side=tk.LEFT, padx=(4, 8))

            tk.Checkbutton(cr, text="Require",
                           variable=self._require_vars[idx]
                           ).pack(side=tk.LEFT, padx=(0, 4))

            tv_lbl = tk.Label(cr, text=f"{self._thresh_vars[idx].get()}%",
                              font=("Courier", 9), fg="#005599", width=4)
            tv_lbl.pack(side=tk.LEFT)
            self._thresh_lbl_widgets.append(tv_lbl)

            tk.Scale(cr,
                     variable=self._thresh_vars[idx],
                     from_=THRESH_MIN, to=THRESH_MAX,
                     orient=tk.HORIZONTAL, length=130,
                     showvalue=False, resolution=1,
                     command=lambda val, i=idx: self._on_thresh_change(val, i)
                     ).pack(side=tk.LEFT, padx=(2, 6))

            img_frame = tk.Frame(s_frame, bg="black")
            img_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=2)

            pil_img = load_template_pil(fname)
            if pil_img:
                pil_img.thumbnail((180, 120), Image.LANCZOS)
                photo = ImageTk.PhotoImage(pil_img)
                self._tmpl_photo_refs[idx] = photo
                lbl_w = tk.Label(img_frame, image=photo, bg="black")
                lbl_w.pack(expand=True, fill=tk.BOTH)
            else:
                lbl_w = tk.Label(img_frame, text="NO IMAGE",
                                 fg="red", bg="black",
                                 font=("Arial", 11, "bold"))
                lbl_w.pack(expand=True)

            self._tmpl_lbl_widgets.append(lbl_w)

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

        is_running = self._isRunning.get()
        if not is_running:
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
        else:
            self._isReady = False
        self._update_step_ui()

    # ── thresh ────────────────────────────────────────────────────────────────
    def _on_thresh_change(self, val, idx):
        self._thresh_lbl_widgets[idx].config(text=f"{int(float(val))}%")

    # ── capture template ──────────────────────────────────────────────────────
    def _capture_template(self, fname, idx):
        if self._isRunning.get():
            return
        with self._lock:
            frame = self._frame.copy() if self._frame is not None else None
        if frame is None:
            return
        dest = os.path.join(TEMPLATE_DIR, fname)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(fname)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.move(dest, os.path.join(STALE_DIR, f"{stem}_{ts}{ext}"))
        cv2.imwrite(dest, frame)
        self._tmpl_gray[idx] = load_template_gray(fname)
        self._refresh_template_ui(fname, idx)

    def _refresh_template_ui(self, fname, idx):
        lbl     = self._tmpl_lbl_widgets[idx]
        pil_img = load_template_pil(fname)
        if pil_img:
            pil_img.thumbnail((180, 120), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pil_img)
            self._tmpl_photo_refs[idx] = photo
            lbl.config(image=photo, text="", bg="black")
        else:
            self._tmpl_photo_refs[idx] = None
            lbl.config(image="", text="NO IMAGE",
                       fg="red", bg="black",
                       font=("Arial", 11, "bold"))

    # ── camera capture thread ─────────────────────────────────────────────────
    def _capture_loop(self):
        interval = 1.0 / CAMERA_FPS
        consecutive_fails = 0

        while self._running_capture:
            t0 = time.time()

            if self._cap is None:
                time.sleep(1.0)
                continue

            ret, frame = self._cap.read()

            if not ret or frame is None:
                consecutive_fails += 1
                if consecutive_fails >= 10:
                    # try to reopen
                    print("[Camera] Too many read failures – attempting reopen...")
                    self._cap.release()
                    time.sleep(1.0)
                    try:
                        self._cap = open_camera(0)
                        consecutive_fails = 0
                        print("[Camera] Reopened OK")
                    except RuntimeError as e:
                        print(f"[Camera] Reopen failed: {e}")
                        self._cap = None
                time.sleep(0.1)
                continue

            consecutive_fails = 0

            # ensure BGR frame is correct size
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
        passed = do_template_match(frame, self._tmpl_gray[tmpl_idx],
                                   self._roi, self._thresh_vars[tmpl_idx].get())
        if not passed:
            return
        next_step = self._step + 1
        if next_step >= len(STEPS):
            self._step    = STEP_READY
            self._isReady = True
            self.after(0, self._on_ready)
        else:
            self._step = next_step
            desc = STEPS[next_step][1]
            self.after(0, lambda d=desc: (self._status_text.set(d),
                                          self._update_step_ui()))

    def _on_ready(self):
        self._status_text.set("✔ READY – press STAMP")
        self._update_step_ui()

    # ── GPIO poll ────────────────────────────────────────────────────────────
    def _gpio_poll_loop(self):
        import RPi.GPIO as GPIO
        last = True
        while self._running_capture:
            cur = GPIO.input(27)
            if last and not cur:
                self.after(0, self._on_stamp_physical)
            last = cur
            time.sleep(0.02)

    # ── stamp ─────────────────────────────────────────────────────────────────
    def _on_stamp_ui_click(self):
        self._try_stamp()

    def _on_stamp_physical(self):
        self._try_stamp()

    def _try_stamp(self):
        if self._busy:
            return
        if self._isRunning.get():
            if not self._isReady:
                return
        else:
            now = time.time()
            if now - self._last_dev_stamp < DEV_STAMP_COOLDOWN:
                rem = DEV_STAMP_COOLDOWN - (now - self._last_dev_stamp)
                self._status_text.set(f"⏳ Cooldown {rem:.1f}s")
                return
            self._last_dev_stamp = now
        self._busy = True
        threading.Thread(target=self._stamp_sequence, daemon=True).start()

    def _stamp_sequence(self):
        if self._isRunning.get() and self._isFinalMatch.get():
            with self._lock:
                frame = self._frame.copy() if self._frame is not None else None
            if frame is not None:
                passed = do_template_match(frame, self._tmpl_gray[3],
                                           self._roi, self._thresh_vars[3].get())
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
            time.sleep(RELAY_MS / 1000.0)
            GPIO.output(17, GPIO.HIGH)
        else:
            time.sleep(RELAY_MS / 1000.0)

        if self._isRunning.get():
            self.after(0, lambda: self._status_text.set(
                f"⏳ Waiting {POST_STAMP_WAIT:.0f}s..."))
            time.sleep(POST_STAMP_WAIT)
            self._step    = 0
            self._isReady = False
            self._busy    = False
            self.after(0, self._reset_cycle)
        else:
            self._busy = False
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
        self._draw_start = None
        if self._draw_rect_id:
            self._canvas.delete(self._draw_rect_id)
            self._draw_rect_id = None

    # ── cleanup ───────────────────────────────────────────────────────────────
    def _on_close(self):
        self._save_settings_now()
        self._running_capture = False
        if self._cap is not None:
            self._cap.release()
        if _HAS_GPIO:
            import RPi.GPIO as GPIO
            GPIO.cleanup()
        self.destroy()


if __name__ == "__main__":
    app = CheckApp()
    app.mainloop()
