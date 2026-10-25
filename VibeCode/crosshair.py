"""
Crosshair overlay with a selector menu (Windows).
Menu: tkinter.  Global hotkeys: pynput.
F8 = recalibrate crosshair to current mouse position
F9 = reset crosshair to true screen center
"""

import sys
import ctypes
import queue
import tkinter as tk
from tkinter import colorchooser
from pynput import keyboard

# ======================================================================
# EASY-TO-EDIT SETTINGS
# ======================================================================
CROSSHAIR_COLOR = "#00ff00"   # any hex color
CROSSHAIR_SIZE = 20           # "radius" in pixels (menu slider: 6-80)
LINE_THICKNESS = 2            # pixels (menu slider: 1-8)
DEFAULT_DESIGN = 0            # index into DESIGNS below

KEY_RECALIBRATE = keyboard.Key.f8
KEY_RESET = keyboard.Key.f9

# Color used as the "see-through" key. Avoid using this exact color for the crosshair.
TRANSPARENT_KEY = "#010203"

COLOR_PRESETS = ["#00ff00", "#ff0000", "#00ffff", "#ffff00", "#ffffff", "#ff00ff"]

# ======================================================================
# DPI AWARENESS (Windows): makes tkinter and cursor coords use real pixels,
# so "screen center" matches the actual resolution on scaled displays.
# ======================================================================
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# ======================================================================
# CROSSHAIR DRAWING (shared by the menu previews and the overlay)
# ======================================================================
DESIGNS = [
    ("Classic cross", "cross"),
    ("Cross with gap", "gap"),
    ("Dot", "dot"),
    ("Circle", "circle"),
    ("Circle + dot", "circle_dot"),
    ("T-shape", "t"),
    ("X-shape", "x"),
    ("Square brackets", "bracket"),
    ("Gap cross + dot", "gap_dot"),
]


def draw_crosshair(cv, kind, cx, cy, s, color, t):
    """Draw one crosshair design centered at (cx, cy) with 'radius' s."""
    gap = max(3, s // 4)

    def line(x1, y1, x2, y2):
        cv.create_line(x1, y1, x2, y2, fill=color, width=t, capstyle="butt")

    def dot(r):
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline=color)

    if kind == "cross":
        line(cx - s, cy, cx + s, cy)
        line(cx, cy - s, cx, cy + s)

    elif kind in ("gap", "gap_dot"):
        line(cx - s, cy, cx - gap, cy)
        line(cx + gap, cy, cx + s, cy)
        line(cx, cy - s, cx, cy - gap)
        line(cx, cy + gap, cx, cy + s)
        if kind == "gap_dot":
            dot(max(1.5, t / 2 + 0.5))

    elif kind == "dot":
        dot(max(2, t + 1))

    elif kind in ("circle", "circle_dot"):
        r = s * 0.75
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=t)
        if kind == "circle_dot":
            dot(max(1.5, t / 2 + 0.5))

    elif kind == "t":  # no top arm
        line(cx - s, cy, cx + s, cy)
        line(cx, cy, cx, cy + s)

    elif kind == "x":
        d = s * 0.7
        g = gap * 0.7
        for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            line(cx + sx * g, cy + sy * g, cx + sx * d, cy + sy * d)

    elif kind == "bracket":  # four corner brackets forming a square
        h, L = s * 0.7, s * 0.35
        for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            line(cx + sx * h, cy + sy * h, cx + sx * (h - L), cy + sy * h)
            line(cx + sx * h, cy + sy * h, cx + sx * h, cy + sy * (h - L))


# ======================================================================
# MAIN APPLICATION
# ======================================================================
class CrosshairApp:
    PAD = 6          # extra pixels around the crosshair inside the overlay window
    PREVIEW = 84     # preview tile size in the menu

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Crosshair Menu")
        self.root.resizable(False, False)

        # --- State ---
        self.color = CROSSHAIR_COLOR
        self.size = tk.IntVar(value=CROSSHAIR_SIZE)
        self.thick = tk.IntVar(value=LINE_THICKNESS)
        self.design = DEFAULT_DESIGN
        self.visible = True

        # True center of the PRIMARY monitor (real resolution thanks to DPI awareness)
        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        self.true_center = (self.screen_w // 2, self.screen_h // 2)
        self.center = self.true_center  # current (possibly recalibrated) center

        # Thread-safe queue: hotkey thread -> tkinter thread
        self.events = queue.Queue()

        self.build_overlay()
        self.build_menu()
        self.start_hotkeys()
        self.refresh_all()
        self.poll(0)

        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

    # ---------------- Overlay window ----------------
    def build_overlay(self):
        """Borderless, topmost, transparent, click-through window."""
        self.ov = tk.Toplevel(self.root)
        self.ov.overrideredirect(True)
        self.ov.attributes("-topmost", True)
        self.ov.config(bg=TRANSPARENT_KEY)
        try:
            self.ov.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            print("Warning: transparent color not supported on this platform.")
        self.ov_canvas = tk.Canvas(self.ov, bg=TRANSPARENT_KEY, highlightthickness=0)
        self.ov_canvas.pack()
        self.ov.update_idletasks()
        self.make_click_through()

    def make_click_through(self):
        """Windows: add WS_EX_TRANSPARENT so mouse clicks pass through the overlay."""
        if sys.platform != "win32":
            return
        GWL_EXSTYLE = -20
        WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x80000, 0x20, 0x80
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(self.ov.winfo_id()) or self.ov.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        )

    def refresh_overlay(self):
        """Resize/move the overlay so the crosshair is centered on self.center."""
        s, t = self.size.get(), self.thick.get()
        half = s + self.PAD
        w = 2 * half + 1  # odd size -> a true center pixel
        cx, cy = self.center
        self.ov.geometry(f"{w}x{w}+{cx - half}+{cy - half}")
        self.ov_canvas.config(width=w, height=w)
        self.ov_canvas.delete("all")
        draw_crosshair(self.ov_canvas, DESIGNS[self.design][1], half, half, s, self.color, t)
        if self.visible:
            self.ov.deiconify()
        else:
            self.ov.withdraw()

    # ---------------- Menu window ----------------
    def build_menu(self):
        tk.Label(self.root, text="Pick a crosshair", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, pady=(8, 2))

        # Grid of clickable previews (3 columns)
        self.tiles = []
        for i, (name, _) in enumerate(DESIGNS):
            frame = tk.Frame(self.root)
            frame.grid(row=1 + i // 3, column=i % 3, padx=5, pady=5)
            cv = tk.Canvas(frame, width=self.PREVIEW, height=self.PREVIEW, bg="#222222",
                           highlightthickness=3, highlightbackground="#222222", cursor="hand2")
            cv.pack()
            cv.bind("<Button-1>", lambda e, idx=i: self.select(idx))
            tk.Label(frame, text=name, font=("Segoe UI", 8)).pack()
            self.tiles.append(cv)

        r = 1 + (len(DESIGNS) + 2) // 3

        # Sliders
        tk.Scale(self.root, from_=6, to=80, orient="horizontal", label="Size",
                 variable=self.size, command=lambda v: self.refresh_all()
                 ).grid(row=r, column=0, columnspan=3, sticky="ew", padx=10)
        tk.Scale(self.root, from_=1, to=8, orient="horizontal", label="Thickness",
                 variable=self.thick, command=lambda v: self.refresh_all()
                 ).grid(row=r + 1, column=0, columnspan=3, sticky="ew", padx=10)

        # Color presets + custom picker
        cf = tk.Frame(self.root)
        cf.grid(row=r + 2, column=0, columnspan=3, pady=4)
        tk.Label(cf, text="Color:").pack(side="left")
        for c in COLOR_PRESETS:
            tk.Button(cf, bg=c, width=2, command=lambda c=c: self.set_color(c)).pack(side="left", padx=2)
        tk.Button(cf, text="Custom…", command=self.pick_color).pack(side="left", padx=4)

        # Action buttons
        bf = tk.Frame(self.root)
        bf.grid(row=r + 3, column=0, columnspan=3, pady=6)
        self.toggle_btn = tk.Button(bf, text="Hide", width=8, command=self.toggle_visible)
        self.toggle_btn.pack(side="left", padx=3)
        tk.Button(bf, text="Reset (F9)", width=10, command=self.reset_center).pack(side="left", padx=3)
        tk.Button(bf, text="Exit", width=8, command=self.exit_app).pack(side="left", padx=3)

        self.status = tk.Label(self.root, fg="#555555", font=("Segoe UI", 8))
        self.status.grid(row=r + 4, column=0, columnspan=3, pady=(0, 8))

    def refresh_previews(self):
        t = min(self.thick.get(), 4)
        for i, cv in enumerate(self.tiles):
            cv.delete("all")
            m = self.PREVIEW // 2
            draw_crosshair(cv, DESIGNS[i][1], m, m, 24, self.color, t)
            cv.config(highlightbackground="#ffffff" if i == self.design else "#222222")

    def refresh_all(self):
        self.refresh_previews()
        self.refresh_overlay()
        self.toggle_btn.config(text="Hide" if self.visible else "Show")
        self.status.config(text=f"Center: {self.center[0]}, {self.center[1]}   "
                                f"(screen {self.screen_w}x{self.screen_h})   F8 = recalibrate")

    # ---------------- Actions ----------------
    def select(self, idx):
        self.design = idx
        self.visible = True
        self.refresh_all()

    def set_color(self, c):
        self.color = c
        self.refresh_all()

    def pick_color(self):
        c = colorchooser.askcolor(color=self.color, title="Crosshair color")[1]
        if c:
            self.set_color(c)

    def toggle_visible(self):
        self.visible = not self.visible
        self.refresh_all()

    def recalibrate(self):
        """F8: make the current mouse position the new crosshair center."""
        self.center = self.root.winfo_pointerxy()
        self.refresh_all()

    def reset_center(self):
        """F9 / Reset button: back to the true screen center."""
        self.center = self.true_center
        self.refresh_all()

    # ---------------- Global hotkeys ----------------
    def start_hotkeys(self):
        """pynput listens system-wide; it only queues events (tkinter is not thread-safe)."""
        def on_press(key):
            if key == KEY_RECALIBRATE:
                self.events.put("recalibrate")
            elif key == KEY_RESET:
                self.events.put("reset")

        self.listener = keyboard.Listener(on_press=on_press)
        self.listener.daemon = True
        self.listener.start()

    def poll(self, tick):
        """Runs in the tkinter thread: handle hotkey events and keep overlay on top."""
        try:
            while True:
                ev = self.events.get_nowait()
                if ev == "recalibrate":
                    self.recalibrate()
                elif ev == "reset":
                    self.reset_center()
        except queue.Empty:
            pass
        if tick % 20 == 0 and self.visible:  # ~once per second: re-assert topmost
            self.ov.lift()
            self.ov.attributes("-topmost", True)
        self.root.after(50, self.poll, tick + 1)

    def exit_app(self):
        self.listener.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    CrosshairApp().run()