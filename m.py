import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import cv2
import json
import os
import threading

# ── paths ──────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "template")
ROI_FILE    = os.path.join(BASE_DIR, "roi.json")

os.makedirs(TEMPLATE_DIR, exist_ok=True)

# ── default ROI ─────────────────────────────────────────────────────────────
DEFAULT_ROI = (0, 0, 100, 100)   # x, y, w, h


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


def load_template(name: str):
    """Return a PIL Image or None."""
    path = os.path.join(TEMPLATE_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        return Image.open(path)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
class CheckApp(tk.Tk):
    DISP_W, DISP_H = 640, 480

    def __init__(self):
        super().__init__()
        self.title("check app")
        self.geometry("1280x720")
        self.resizable(False, False)

        # ── state ───────────────────────────────────────────────────────────
        self._isRunning    = tk.BooleanVar(value=False)
        self._isDisplayROI = tk.BooleanVar(value=False)
        self._roi          = list(load_roi())   # [x, y, w, h]

        # ROI drawing state
        self._draw_start   = None
        self._draw_rect_id = None

        # ── camera ──────────────────────────────────────────────────────────
        self._cap   = cv2.VideoCapture(0)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self.DISP_W)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.DISP_H)
        self._frame = None
        self._lock  = threading.Lock()
        self._running_capture = True
        self._capture_thread  = threading.Thread(target=self._capture_loop,
                                                  daemon=True)
        self._capture_thread.start()

        # ── build UI ─────────────────────────────────────────────────────────
        self._build_ui()

        # ── start display loop ──────────────────────────────────────────────
        self._update_display()

        # ── cleanup on close ────────────────────────────────────────────────
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ────────────────────────────────────────────────────────────────────────
    #  UI construction
    # ────────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # root grid: 2 columns
        self.columnconfigure(0, weight=2)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ── COL 0 ────────────────────────────────────────────────────────────
        col0 = tk.Frame(self)
        col0.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        col0.rowconfigure(0, weight=0)
        col0.rowconfigure(1, weight=1)

        # col0 row0 – controls frame
        ctrl_frame = tk.Frame(col0, bd=1, relief=tk.RIDGE)
        ctrl_frame.grid(row=0, column=0, sticky="ew", padx=2, pady=2)

        tk.Checkbutton(ctrl_frame, text="Running",
                       variable=self._isRunning).pack(side=tk.LEFT, padx=6, pady=4)

        tk.Checkbutton(ctrl_frame, text="Display ROI",
                       variable=self._isDisplayROI).pack(side=tk.LEFT, padx=6, pady=4)

        self._roi_label = tk.Label(ctrl_frame,
                                   text=self._roi_text(),
                                   fg="#cc8800", font=("Courier", 9))
        self._roi_label.pack(side=tk.LEFT, padx=6, pady=4)

        # col0 row1 – camera canvas
        self._canvas = tk.Canvas(col0,
                                 width=self.DISP_W, height=self.DISP_H,
                                 bg="black", cursor="crosshair")
        self._canvas.grid(row=1, column=0, padx=2, pady=2)

        # ROI drawing bindings (active only when not Running)
        self._canvas.bind("<ButtonPress-1>",   self._roi_mouse_press)
        self._canvas.bind("<B1-Motion>",       self._roi_mouse_drag)
        self._canvas.bind("<ButtonRelease-1>", self._roi_mouse_release)

        # ── COL 1 ────────────────────────────────────────────────────────────
        col1 = tk.Frame(self)
        col1.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        for r in range(3):
            col1.rowconfigure(r, weight=1)
        col1.columnconfigure(0, weight=1)

        template_names = ["s1.png", "s2.png", "s3.png"]
        labels         = ["S1", "S2", "S3"]
        self._tmpl_imgs = []   # keep references alive

        for idx, (label, fname) in enumerate(zip(labels, template_names)):
            frame = tk.LabelFrame(col1, text=label, bd=1, relief=tk.RIDGE)
            frame.grid(row=idx, column=0, sticky="nsew", padx=4, pady=4)
            frame.rowconfigure(0, weight=0)
            frame.rowconfigure(1, weight=1)
            frame.columnconfigure(0, weight=1)

            # row 0 – button + placeholder checkbox
            ctrl_row = tk.Frame(frame)
            ctrl_row.grid(row=0, column=0, sticky="ew", padx=4, pady=2)
            tk.Button(ctrl_row, text=f"Load {label}",
                      command=lambda f=fname, i=idx: self._reload_template(f, i)
                      ).pack(side=tk.LEFT, padx=4)
            tk.Checkbutton(ctrl_row, text="not-imple").pack(side=tk.LEFT, padx=4)

            # row 1 – image or "NO IMAGE"
            img_frame = tk.Frame(frame, bg="black")
            img_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

            pil_img = load_template(fname)
            if pil_img:
                # fit inside available area (approx 200×160)
                pil_img.thumbnail((200, 160), Image.LANCZOS)
                photo = ImageTk.PhotoImage(pil_img)
                self._tmpl_imgs.append(photo)
                lbl = tk.Label(img_frame, image=photo, bg="black")
                lbl.pack(expand=True, fill=tk.BOTH)
            else:
                self._tmpl_imgs.append(None)
                lbl = tk.Label(img_frame, text="NO IMAGE",
                               fg="red", bg="black",
                               font=("Arial", 14, "bold"))
                lbl.pack(expand=True)

            # store label widget for reload
            setattr(self, f"_s{idx+1}_label_widget", lbl)
            setattr(self, f"_s{idx+1}_img_frame",    img_frame)

    # ────────────────────────────────────────────────────────────────────────
    #  Helper: ROI text
    # ────────────────────────────────────────────────────────────────────────
    def _roi_text(self):
        x, y, w, h = self._roi
        return f"ROI: ({x},{y},{w},{h})"

    # ────────────────────────────────────────────────────────────────────────
    #  Template reload
    # ────────────────────────────────────────────────────────────────────────
    def _reload_template(self, fname: str, idx: int):
        lbl       = getattr(self, f"_s{idx+1}_label_widget")
        img_frame = getattr(self, f"_s{idx+1}_img_frame")
        pil_img   = load_template(fname)
        if pil_img:
            pil_img.thumbnail((200, 160), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pil_img)
            self._tmpl_imgs[idx] = photo
            lbl.config(image=photo, text="", bg="black")
        else:
            self._tmpl_imgs[idx] = None
            lbl.config(image="", text="NO IMAGE",
                       fg="red", bg="black",
                       font=("Arial", 14, "bold"))

    # ────────────────────────────────────────────────────────────────────────
    #  Camera capture thread
    # ────────────────────────────────────────────────────────────────────────
    def _capture_loop(self):
        while self._running_capture:
            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._frame = frame

    # ────────────────────────────────────────────────────────────────────────
    #  Display loop (Tk main thread)
    # ────────────────────────────────────────────────────────────────────────
    def _update_display(self):
        with self._lock:
            frame = self._frame.copy() if self._frame is not None else None

        if frame is not None:
            # draw ROI overlay
            if self._isDisplayROI.get():
                x, y, w, h = self._roi
                cv2.rectangle(frame, (x, y), (x + w, y + h),
                              (0, 255, 255), 1)   # yellow-ish in BGR

            # BGR → RGB → PIL → ImageTk
            rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil   = Image.fromarray(rgb)
            photo = ImageTk.PhotoImage(pil)

            self._canvas.create_image(0, 0, anchor=tk.NW, image=photo)
            self._canvas._photo = photo   # prevent GC

        self.after(30, self._update_display)   # ~33 fps

    # ────────────────────────────────────────────────────────────────────────
    #  ROI mouse drawing (only when not Running)
    # ────────────────────────────────────────────────────────────────────────
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
            x0, y0, event.x, event.y,
            outline="yellow", width=1)

    def _roi_mouse_release(self, event):
        if self._isRunning.get() or self._draw_start is None:
            return
        x0, y0 = self._draw_start
        x1, y1 = event.x, event.y
        # normalise
        rx = min(x0, x1)
        ry = min(y0, y1)
        rw = abs(x1 - x0)
        rh = abs(y1 - y0)

        if rw > 2 and rh > 2:          # ignore accidental clicks
            self._roi = [rx, ry, rw, rh]
            save_roi(tuple(self._roi))
            self._roi_label.config(text=self._roi_text())

        self._draw_start = None
        if self._draw_rect_id:
            self._canvas.delete(self._draw_rect_id)
            self._draw_rect_id = None

    # ────────────────────────────────────────────────────────────────────────
    #  Cleanup
    # ────────────────────────────────────────────────────────────────────────
    def _on_close(self):
        self._running_capture = False
        self._cap.release()
        self.destroy()


# ── entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = CheckApp()
    app.mainloop()