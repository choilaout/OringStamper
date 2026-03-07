import cv2
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
import os

class AppGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Camera Monitor System")
        
        # Cấu hình không cho resize và đặt vị trí top-left (0,0)
        self.root.geometry("1280x720+0+0")
        self.root.resizable(False, False)
        self.root.configure(bg="#393939")

        # Chia tỷ lệ cột
        self.root.columnconfigure(0, weight=3) # Cột camera rộng hơn
        self.root.columnconfigure(1, weight=1) # Cột chứa template ảnh
        self.root.rowconfigure(0, weight=1)

        # --- CỘT 0 ---
        self.col0_frame = tk.Frame(self.root, bg="#393939")
        self.col0_frame.grid(row=0, column=0, sticky="nsew")
        
        # Row 0: Camera Display
        self.lbl_cam = tk.Label(self.col0_frame, bg="gray", fg="white", font=("Arial", 20))
        self.lbl_cam.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Row 1: NOT-IMPLEMENT
        self.lbl_not_imp = tk.Label(self.col0_frame, text="NOT-IMPLEMENT", 
                                    bg="#2b2b2b", fg="#888888", height=5)
        self.lbl_not_imp.pack(fill="x", padx=5, pady=5)

        # --- CỘT 1 ---
        self.col1_frame = tk.Frame(self.root, bg="#2b2b2b", width=300)
        self.col1_frame.grid(row=0, column=1, sticky="nsew")
        
        # Load 3 ảnh template
        self.template_labels = []
        self.load_templates(["template/s1.png", "template/s2.png", "template/s3.png"])

        # Khởi tạo Camera
        self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened():
            self.lbl_cam.config(text="NO CAMERA DETECTED")
        else:
            self.update_camera()

    def load_templates(self, paths):
        for i, path in enumerate(paths):
            lbl = tk.Label(self.col1_frame, bg="#393939", fg="white", text="NO IMAGE DETECT", 
                           relief="sunken", bd=1)
            lbl.pack(fill="both", expand=True, padx=5, pady=5)
            
            if os.path.exists(path):
                try:
                    img = Image.open(path)
                    # Resize ảnh để fit vào cột bên phải (giữ tỉ lệ hoặc cố định)
                    img = img.resize((250, 150), Image.Resampling.LANCZOS)
                    img_tk = ImageTk.PhotoImage(img)
                    lbl.config(image=img_tk, text="")
                    lbl.image = img_tk # Giữ reference để tránh bị garbage collected
                except Exception as e:
                    print(f"Error loading {path}: {e}")
            self.template_labels.append(lbl)

    def update_camera(self):
        ret, frame = self.cap.read()
        if ret:
            # Chuyển màu từ BGR sang RGB
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            # Resize frame để vừa vặn vùng hiển thị (khoảng 900x500)
            img = Image.fromarray(frame)
            img = img.resize((960, 540), Image.Resampling.LANCZOS)
            img_tk = ImageTk.PhotoImage(image=img)
            
            self.lbl_cam.imgtk = img_tk
            self.lbl_cam.configure(image=img_tk)
            
        self.root.after(30, self.update_camera) # ~30 FPS display

    def __del__(self):
        if hasattr(self, 'cap') and self.cap.isOpened():
            self.cap.release()

if __name__ == "__main__":
    root = tk.Tk()
    app = AppGUI(root)
    root.mainloop()