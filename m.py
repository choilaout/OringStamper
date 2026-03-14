import tkinter as tk
from PIL import Image, ImageTk
import cv2
import json
import os
import shutil
import threading
from datetime import datetime

# ── paths ───────────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR    = os.path.join(BASE_DIR, "template")
STALE_DIR       = os.path.join(BASE_DIR, "template_stale")
ROI_FILE        = os.path.join(BASE_DIR, "roi.json")
SETTINGS_FILE   = os.path.join(BASE_DIR, "appsetting.json")

os.makedirs(TEMPLATE_DIR, exist_ok=True)
os.makedirs(STALE_DIR,    exist_ok=True)

DEFAULT_ROI = (0, 0, 100, 100)

# ── default / schema for appsetting.json ────────────────────────────────────
DEFAULT_SETTINGS = {
    "isRunning":    False,
    "isDisplayROI": False,
    "s1": {"isRequire": False, "threshold": 75},
    "s2": {"isRequire": False, "threshold": 75},
    "s3": {"isRequire": False, "threshold": 75},
}


def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            # merge with defaults so missing keys are filled
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            for k in ("s1", "s2", "s3"):
                merged[k] = {**DEFAULT_SETTINGS[k], **data.get(k, {})}
            return merged
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(cfg: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def load_roi():
    if os.path.exists(ROI_FILE):
        try:
            with open(ROI_FILE) as f:
                d = json.load(f)
            return tuple(d["roi"])
        except Exception:
            pass
    return DEFAULT_ROI


def save_roi(roi: tuple):
    with open(ROI_FILE, "w") as f:
        json.dump({"roi": list(roi)}, f)


def load_template_pil(name: str):
    path = os.path.join(TEMPLATE_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        return Image.open(path).copy()
    except Exception:
        return None


def load_template_gray(name: str):
    path = os.path.join(TEMPLATE_DIR, name)
    if not os.path.exists(path):
        return None
    return cv2.imread(path, cv2.IMREAD_GRAYSCALE)


# ═══════════════════════════════════════════════════════════════════════════
class CheckApp(tk.Tk):
    DISP_W, DISP_H = 640, 480
    TMPL_NAMES     = ["s1.png", "s2.png", "s3.png"]
    TMPL_LABELS    = ["S1",     "S2",     "S3"]
    TMPL_KEYS      = ["s1",     "s2",     "s3"]
    THRESH_MIN     = 50
    THRESH_MAX     = 95

    def __init__(self):
        super().__init__()
        self.title("check app")
        self.geometry("1280x720")
        self.resizable(False, False)

        # ── load persisted settings ──────────────────────────────────────────
        cfg = load_settings()

        # ── tk variables ─────────────────────────────────────────────────────
        self._isRunning    = tk.BooleanVar(value=cfg["isRunning"])
        self._isDisplayROI = tk.BooleanVar(value=cfg["isDisplayROI"])
        self._roi          = list(load_roi())

        self._isS1Require  = tk.BooleanVar(value=cfg["s1"]["isRequire"])
        self._isS2Require  = tk.BooleanVar(value=cfg["s2"]["isRequire"])
        self._isS3Require  = tk.BooleanVar(value=cfg["s3"]["isRequire"])
        self._require_vars = [self._isS1Require,
                               self._isS2Require,
                               self._isS3Require]

        # threshold IntVars (50-95)
        self._thresh_vars = [
            tk.IntVar(value=cfg["s1"]["threshold"]),
            tk.IntVar(value=cfg["s2"]["threshold"]),
            tk.IntVar(value=cfg["s3"]["threshold"]),
        ]

        # register save-on-change traces
        for v in [self._isRunning, self._isDisplayROI,
                  *self._require_vars, *self._thresh_vars]:
            v.trace_add("write", lambda *_: self._save_settings_now())

        # grayscale arrays for matching
        self._tmpl_gray = [load_template_gray(n) for n in self.TMPL_NAMES]

        # ROI drawing
        self._draw_start   = None
        self._draw_rect_id = None

        # ── camera ──────────────────────────────────────────────────────────
        self._cap = cv2.VideoCapture(0)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self.DISP_W)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.DISP_H)
        self._frame           = None
        self._lock            = threading.Lock()
        self._running_capture = True
        threading.Thread(target=self._capture_loop, daemon=True).start()

        # UI refs
        self._tmpl_photo_refs  = [None, None, None]
        self._tmpl_lbl_widgets = []
        self._thresh_lbl_widgets = []   # value labels next to sliders

        self._build_ui()
        self._update_display()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ────────────────────────────────────────────────────────────────────────
    #  Persist settings
    # ────────────────────────────────────────────────────────────────────────
    def _save_settings_now(self):
        cfg = {
            "isRunning":    self._isRunning.get(),
            "isDisplayROI": self._isDisplayROI.get(),
            "s1": {"isRequire": self._isS1Require.get(),
                   "threshold": self._thresh_vars[0].get()},
            "s2": {"isRequire": self._isS2Require.get(),
                   "threshold": self._thresh_vars[1].get()},
            "s3": {"isRequire": self._isS3Require.get(),
                   "threshold": self._thresh_vars[2].get()},
        }
        save_settings(cfg)

    # ────────────────────────────────────────────────────────────────────────
    #  UI construction
    # ────────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ── COL 0 ────────────────────────────────────────────────────────────
        col0 = tk.Frame(self)
        col0.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        col0.rowconfigure(0, weight=0)
        col0.rowconfigure(1, weight=1)

        ctrl_frame = tk.Frame(col0, bd=1, relief=tk.RIDGE)
        ctrl_frame.grid(row=0, column=0, sticky="ew", padx=2, pady=2)

        tk.Checkbutton(ctrl_frame, text="Running",
                       variable=self._isRunning).pack(side=tk.LEFT, padx=6, pady=4)
        tk.Checkbutton(ctrl_frame, text="Display ROI",
                       variable=self._isDisplayROI).pack(side=tk.LEFT, padx=6, pady=4)
        self._roi_label = tk.Label(ctrl_frame, text=self._roi_text(),
                                   fg="#cc8800", font=("Courier", 9))
        self._roi_label.pack(side=tk.LEFT, padx=6, pady=4)

        self._canvas = tk.Canvas(col0, width=self.DISP_W, height=self.DISP_H,
                                 bg="black", cursor="crosshair")
        self._canvas.grid(row=1, column=0, padx=2, pady=2)
        self._canvas.bind("<ButtonPress-1>",   self._roi_mouse_press)
        self._canvas.bind("<B1-Motion>",       self._roi_mouse_drag)
        self._canvas.bind("<ButtonRelease-1>", self._roi_mouse_release)

        # ── COL 1 ────────────────────────────────────────────────────────────
        col1 = tk.Frame(self)
        col1.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        for r in range(3):
            col1.rowconfigure(r, weight=1)
        col1.columnconfigure(0, weight=1)

        for idx, (label, fname) in enumerate(
                zip(self.TMPL_LABELS, self.TMPL_NAMES)):

            s_frame = tk.LabelFrame(col1, text=label, bd=1, relief=tk.RIDGE)
            s_frame.grid(row=idx, column=0, sticky="nsew", padx=4, pady=4)
            s_frame.rowconfigure(0, weight=0)
            s_frame.rowconfigure(1, weight=1)
            s_frame.columnconfigure(0, weight=1)

            # ── row 0: controls ──────────────────────────────────────────────
            ctrl_row = tk.Frame(s_frame)
            ctrl_row.grid(row=0, column=0, sticky="ew", padx=4, pady=2)

            # Capture button
            tk.Button(ctrl_row,
                      text=f"Capture {label}",
                      command=lambda f=fname, i=idx: self._capture_template(f, i)
                      ).pack(side=tk.LEFT, padx=(4, 8))

            # Require checkbox
            tk.Checkbutton(ctrl_row,
                           text="Require",
                           variable=self._require_vars[idx]
                           ).pack(side=tk.LEFT, padx=(0, 4))

            # Threshold label (value readout)
            thresh_val_lbl = tk.Label(ctrl_row,
                                      text=f"{self._thresh_vars[idx].get()}%",
                                      font=("Courier", 9), fg="#005599", width=4)
            thresh_val_lbl.pack(side=tk.LEFT)
            self._thresh_lbl_widgets.append(thresh_val_lbl)

            # Threshold slider
            slider = tk.Scale(
                ctrl_row,
                variable=self._thresh_vars[idx],
                from_=self.THRESH_MIN,
                to=self.THRESH_MAX,
                orient=tk.HORIZONTAL,
                length=140,
                showvalue=False,
                resolution=1,
                command=lambda val, i=idx: self._on_thresh_change(val, i),
            )
            slider.pack(side=tk.LEFT, padx=(2, 6))

            # ── row 1: thumbnail ─────────────────────────────────────────────
            img_frame = tk.Frame(s_frame, bg="black")
            img_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

            pil_img = load_template_pil(fname)
            if pil_img:
                pil_img.thumbnail((200, 160), Image.LANCZOS)
                photo = ImageTk.PhotoImage(pil_img)
                self._tmpl_photo_refs[idx] = photo
                lbl = tk.Label(img_frame, image=photo, bg="black")
                lbl.pack(expand=True, fill=tk.BOTH)
            else:
                lbl = tk.Label(img_frame, text="NO IMAGE",
                               fg="red", bg="black",
                               font=("Arial", 14, "bold"))
                lbl.pack(expand=True)

            self._tmpl_lbl_widgets.append(lbl)

    # ────────────────────────────────────────────────────────────────────────
    #  Threshold slider callback
    # ────────────────────────────────────────────────────────────────────────
    def _on_thresh_change(self, val, idx: int):
        self._thresh_lbl_widgets[idx].config(text=f"{int(float(val))}%")
        # trace on IntVar already calls _save_settings_now

    # ────────────────────────────────────────────────────────────────────────
    #  Capture current frame → replace template file
    # ────────────────────────────────────────────────────────────────────────
    def _capture_template(self, fname: str, idx: int):
        if self._isRunning.get():
            return

        with self._lock:
            frame = self._frame.copy() if self._frame is not None else None
        if frame is None:
            return

        dest = os.path.join(TEMPLATE_DIR, fname)

        if os.path.exists(dest):
            stem, ext   = os.path.splitext(fname)
            ts          = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.move(dest, os.path.join(STALE_DIR, f"{stem}_{ts}{ext}"))

        cv2.imwrite(dest, frame)
        self._tmpl_gray[idx] = load_template_gray(fname)
        self._refresh_template_ui(fname, idx)

    # ────────────────────────────────────────────────────────────────────────
    def _refresh_template_ui(self, fname: str, idx: int):
        lbl     = self._tmpl_lbl_widgets[idx]
        pil_img = load_template_pil(fname)
        if pil_img:
            pil_img.thumbnail((200, 160), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pil_img)
            self._tmpl_photo_refs[idx] = photo
            lbl.config(image=photo, text="", bg="black")
        else:
            self._tmpl_photo_refs[idx] = None
            lbl.config(image="", text="NO IMAGE",
                       fg="red", bg="black",
                       font=("Arial", 14, "bold"))

    # ────────────────────────────────────────────────────────────────────────
    def _roi_text(self):
        x, y, w, h = self._roi
        return f"ROI: ({x},{y},{w},{h})"

    def _capture_loop(self):
        while self._running_capture:
            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._frame = frame

    def _update_display(self):
        with self._lock:
            frame = self._frame.copy() if self._frame is not None else None
        if frame is not None:
            if self._isDisplayROI.get():
                x, y, w, h = self._roi
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 255), 1)
            rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(Image.fromarray(rgb))
            self._canvas.create_image(0, 0, anchor=tk.NW, image=photo)
            self._canvas._photo = photo
        self.after(30, self._update_display)

    # ── ROI mouse ──────────────────────────────────────────────────────────
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

    def _on_close(self):
        self._save_settings_now()
        self._running_capture = False
        self._cap.release()
        self.destroy()


if __name__ == "__main__":
    app = CheckApp()
    app.mainloop()