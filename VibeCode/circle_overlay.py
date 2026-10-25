import ctypes
import math            # NEW
import queue
import threading       # NEW
import time            # NEW
import tkinter as tk

import cv2             # NEW  (pip install opencv-python)
import keyboard        # pip install keyboard
import mss             # NEW  (pip install mss)
import numpy as np     # NEW  (pip install numpy)

# ======================= EASY-TO-EDIT SETTINGS =======================
CIRCLE_DIAMETER = 200        # starting circle size in pixels (the slider changes it live)
MIN_DIAMETER = 20            # smallest size the slider allows
CIRCLE_COLOR = "#00FF00"     # outline color (any hex color except #010101)
LINE_THICKNESS = 3           # outline width in pixels
TOGGLE_HOTKEY = "alt+b"      # show/hide the circle
MENU_HOTKEY = "alt+m"        # show/hide the control panel
QUIT_HOTKEY = "alt+q"        # exit the script

# --- "Move mouse to rectangle" settings ---
RECT_HOTKEY = "alt+r"        # toggle the "Move mouse to rectangle" feature
RECT_MIN_SIZE = 30           # smallest rectangle (width AND height, in pixels) to accept
RECT_MAX_SIZE = 600          # largest rectangle (width AND height, in pixels) to accept
RECT_COLOR_FILTER = None     # None = detect by edges (any color)
                             # or a hex color like "#FF0000", or a BGR tuple like (0, 0, 255)
RECT_COLOR_TOLERANCE = 40    # how far each color channel may differ (only used with a color filter)
MOUSE_MOVE_SPEED = 1500      # STARTING cursor speed in pixels per second (the slider changes it live)
MOUSE_SPEED_MIN = 100        # NEW: slowest speed the slider allows
MOUSE_SPEED_MAX = 5000       # NEW: fastest speed the slider allows
SCANS_PER_SECOND = 20        # how often the screen is scanned while the feature is ON
# =====================================================================

TRANSPARENT_KEY = "#010101"  # background color made invisible (don't use it for the circle)

# --- internal tuning for the rectangle feature ---
CORNER_MARGIN = 3            # failsafe: cursor within this many px of a screen corner = stop + exit
MOVE_STEP_DELAY = 0.008      # seconds between cursor steps during smooth movement
ARRIVED_DISTANCE = 4         # don't re-move if the cursor is already this close to the target
EXCLUDE_PAD = 6              # extra pixels around our own windows that are ignored

# --- Windows API constants for click-through ---
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080  # keeps the overlay out of the Alt+Tab list
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # hides a window from screen capture (Win10 2004+)
GA_ROOT = 2

user32 = ctypes.windll.user32


# --- cursor helpers (ctypes, works with the DPI-aware setup) ---
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def set_cursor_pos(x, y):
    user32.SetCursorPos(int(x), int(y))


def make_click_through(window):
    """Add Windows extended styles so mouse clicks pass through the window."""
    hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd, GWL_EXSTYLE,
        style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW,
    )


def hide_from_capture(window):
    """Ask Windows to leave this window out of screen captures (best effort)."""
    try:
        hwnd = user32.GetAncestor(window.winfo_id(), GA_ROOT) or window.winfo_id()
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        pass  # older Windows: the geometric exclusion below still protects us


def parse_color_filter(color):
    """Turn RECT_COLOR_FILTER into (lower_bgr, upper_bgr) arrays, or None."""
    if color is None:
        return None
    if isinstance(color, str):
        h = color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        bgr = np.array([b, g, r], dtype=np.int32)
    else:
        bgr = np.array(color, dtype=np.int32)  # tuple is already BGR
    lo = np.clip(bgr - RECT_COLOR_TOLERANCE, 0, 255).astype(np.uint8)
    hi = np.clip(bgr + RECT_COLOR_TOLERANCE, 0, 255).astype(np.uint8)
    return lo, hi


def rects_intersect(a, b):
    """a and b are (left, top, right, bottom)."""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def main():
    # Make the process DPI-aware so we get the screen's true pixel resolution.
    user32.SetProcessDPIAware()

    # Primary monitor resolution (the primary monitor's origin is always 0,0).
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    max_diameter = min(screen_w, screen_h) - 40  # slider upper limit

    # Hidden master window. Both the overlay and the menu are children of it,
    # so hiding the overlay doesn't hide the menu.
    root = tk.Tk()
    root.withdraw()

    # --- Circle overlay window ---
    overlay = tk.Toplevel(root)
    overlay.overrideredirect(True)                            # no title bar or borders
    overlay.attributes("-topmost", True)                      # always on top
    overlay.attributes("-transparentcolor", TRANSPARENT_KEY)  # background becomes invisible
    overlay.configure(bg=TRANSPARENT_KEY)

    canvas = tk.Canvas(overlay, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
    canvas.pack()

    def draw_circle(diameter):
        """Resize the overlay, re-center it on the primary screen, and redraw the circle."""
        diameter = int(diameter)
        pad = LINE_THICKNESS + 2
        size = diameter + 2 * pad
        x = (screen_w - size) // 2
        y = (screen_h - size) // 2
        overlay.geometry(f"{size}x{size}+{x}+{y}")
        canvas.config(width=size, height=size)
        canvas.delete("all")
        canvas.create_oval(pad, pad, pad + diameter, pad + diameter,
                           outline=CIRCLE_COLOR, width=LINE_THICKNESS)

    draw_circle(CIRCLE_DIAMETER)
    overlay.update()
    make_click_through(overlay)
    hide_from_capture(overlay)  # keep the circle out of the screenshots
    overlay.withdraw()  # circle starts hidden

    # --- Draggable control panel (normal window, drag it by its title bar) ---
    menu = tk.Toplevel(root)
    menu.title("Circle Settings")
    menu.attributes("-topmost", True)
    menu.resizable(False, False)
    menu.geometry("+60+60")
    menu.protocol("WM_DELETE_WINDOW", menu.withdraw)  # the X button hides it (Alt+M brings it back)

    tk.Label(menu, text="Circle size (px)").pack(padx=12, pady=(10, 0))
    slider = tk.Scale(menu, from_=MIN_DIAMETER, to=max_diameter, orient="horizontal",
                      length=260, command=draw_circle)
    slider.set(CIRCLE_DIAMETER)
    slider.pack(padx=12, pady=(0, 6))

    # ======================= rectangle feature state =======================
    rect_enabled = threading.Event()   # set = feature ON (thread-safe flag)
    stop_event = threading.Event()     # set = everything must stop (quit / failsafe)
    exclude_lock = threading.Lock()
    exclude = {"menu": None, "circle": None}  # screen rects of our own windows (updated by poll)

    # NEW (speed slider): shared speed value. The slider (main thread) writes it,
    # the detection thread only reads it. The thread never touches tkinter.
    speed_start = min(max(MOUSE_MOVE_SPEED, MOUSE_SPEED_MIN), MOUSE_SPEED_MAX)
    move_speed = {"value": float(speed_start)}

    rect_var = tk.BooleanVar(value=False)  # checkbox state (OFF by default, main thread only)

    def apply_rect_state():
        """Copy the checkbox state into the thread-safe flag."""
        if rect_var.get():
            rect_enabled.set()
        else:
            rect_enabled.clear()

    tk.Checkbutton(menu, text="Move mouse to rectangle", variable=rect_var,
                   command=apply_rect_state).pack(padx=12, pady=(0, 6), anchor="w")

    # NEW (speed slider): controls how fast the cursor glides to the rectangle
    def on_speed_change(value):
        move_speed["value"] = max(float(MOUSE_SPEED_MIN), float(value))

    tk.Label(menu, text="Mouse speed (px/sec)").pack(padx=12, pady=(0, 0))
    speed_slider = tk.Scale(menu, from_=MOUSE_SPEED_MIN, to=MOUSE_SPEED_MAX, resolution=50,
                            orient="horizontal", length=260, command=on_speed_change)
    speed_slider.set(speed_start)
    speed_slider.pack(padx=12, pady=(0, 6))
    # =======================================================================

    tk.Label(menu, fg="gray30",
             text=f"{TOGGLE_HOTKEY.upper()} circle  |  {MENU_HOTKEY.upper()} menu  |  "
                  f"{RECT_HOTKEY.upper()} rectangle  |  {QUIT_HOTKEY.upper()} quit"
             ).pack(padx=12, pady=(0, 10))
    menu.update()
    hide_from_capture(menu)  # keep the menu out of the screenshots

    # --- Thread-safe hotkey handling ---
    # The keyboard library calls back from another thread, but Tk must only be
    # touched from the main thread, so callbacks just drop messages in a queue.
    commands = queue.Queue()

    def on_quit_hotkey():
        # Stop the mouse feature instantly (don't wait for the main thread), then quit.
        rect_enabled.clear()
        stop_event.set()
        commands.put("quit")

    keyboard.add_hotkey(TOGGLE_HOTKEY, lambda: commands.put("toggle"))
    keyboard.add_hotkey(MENU_HOTKEY, lambda: commands.put("menu"))
    keyboard.add_hotkey(RECT_HOTKEY, lambda: commands.put("rect"))
    keyboard.add_hotkey(QUIT_HOTKEY, on_quit_hotkey)

    # ======================= detection / mouse thread =======================
    color_bounds = parse_color_filter(RECT_COLOR_FILTER)
    kernel = np.ones((3, 3), np.uint8)
    screen_center = (screen_w / 2, screen_h / 2)

    def in_corner():
        x, y = get_cursor_pos()
        near_x = x <= CORNER_MARGIN or x >= screen_w - 1 - CORNER_MARGIN
        near_y = y <= CORNER_MARGIN or y >= screen_h - 1 - CORNER_MARGIN
        return near_x and near_y

    def trigger_failsafe():
        rect_enabled.clear()
        stop_event.set()
        commands.put("quit")

    def find_rectangles(frame):
        """Return a list of (center_x, center_y) for 4-sided shapes on the screen."""
        if color_bounds is not None:
            mask = cv2.inRange(frame, color_bounds[0], color_bounds[1])
            edges = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        else:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            edges = cv2.dilate(cv2.Canny(gray, 50, 150), kernel)

        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        with exclude_lock:
            menu_rect = exclude["menu"]
            circle_rect = exclude["circle"]

        found = []
        for c in contours:
            approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue
            x, y, w, h = cv2.boundingRect(approx)
            if not (RECT_MIN_SIZE <= w <= RECT_MAX_SIZE and RECT_MIN_SIZE <= h <= RECT_MAX_SIZE):
                continue
            if cv2.contourArea(approx) / float(w * h) < 0.7:  # must really fill its box
                continue
            box = (x, y, x + w, y + h)
            # Never detect our own menu window.
            if menu_rect is not None and rects_intersect(box, menu_rect):
                continue
            # Never detect our own circle overlay (fallback if capture-exclusion isn't available).
            if circle_rect is not None:
                cw = circle_rect[2] - circle_rect[0]
                inside = (box[0] >= circle_rect[0] - EXCLUDE_PAD and box[1] >= circle_rect[1] - EXCLUDE_PAD
                          and box[2] <= circle_rect[2] + EXCLUDE_PAD and box[3] <= circle_rect[3] + EXCLUDE_PAD)
                if inside and w >= 0.9 * cw and h >= 0.9 * cw:
                    continue
            cx, cy = x + w // 2, y + h // 2
            # Don't target spots in a screen corner (that would trip the failsafe).
            if (cx <= CORNER_MARGIN + 5 or cx >= screen_w - 6 - CORNER_MARGIN) and \
               (cy <= CORNER_MARGIN + 5 or cy >= screen_h - 6 - CORNER_MARGIN):
                continue
            found.append((cx, cy))
        return found

    def move_cursor_smooth(tx, ty):
        """Glide the cursor to (tx, ty). Aborts instantly if the feature is turned off."""
        sx, sy = get_cursor_pos()
        dist = math.hypot(tx - sx, ty - sy)
        if dist <= ARRIVED_DISTANCE:
            return
        speed = max(1.0, move_speed["value"])  # NEW: read the live slider value
        steps = max(1, int(dist / speed / MOVE_STEP_DELAY))
        for i in range(1, steps + 1):
            if stop_event.is_set() or not rect_enabled.is_set():
                return
            if in_corner():          # failsafe: user threw the mouse into a corner
                trigger_failsafe()
                return
            t = i / steps
            t = t * t * (3 - 2 * t)  # smoothstep easing
            set_cursor_pos(sx + (tx - sx) * t, sy + (ty - sy) * t)
            time.sleep(MOVE_STEP_DELAY)

    def detection_worker():
        """Background thread. Never touches tkinter."""
        interval = 1.0 / max(1, SCANS_PER_SECOND)
        region = {"left": 0, "top": 0, "width": screen_w, "height": screen_h}
        with mss.mss() as sct:  # create mss inside the thread that uses it
            while not stop_event.is_set():
                if not rect_enabled.wait(timeout=0.2):  # sleeps while the feature is OFF
                    continue
                started = time.perf_counter()
                if in_corner():  # failsafe check on every scan
                    trigger_failsafe()
                    break
                try:
                    frame = np.ascontiguousarray(np.array(sct.grab(region))[:, :, :3])  # BGRA -> BGR
                    targets = find_rectangles(frame)
                    if targets and rect_enabled.is_set() and not stop_event.is_set():
                        tx, ty = min(targets, key=lambda p: math.hypot(p[0] - screen_center[0],
                                                                      p[1] - screen_center[1]))
                        move_cursor_smooth(tx, ty)
                except Exception as e:  # keep the thread alive no matter what
                    print(f"Rectangle scan error: {e}")
                    time.sleep(0.5)
                spare = interval - (time.perf_counter() - started)
                if spare > 0:
                    time.sleep(spare)

    threading.Thread(target=detection_worker, daemon=True).start()
    # =======================================================================

    def update_exclusions():
        """Runs on the main thread. Publishes our windows' screen areas for the worker."""
        def area(win):
            if win.state() == "withdrawn":
                return None
            x, y = win.winfo_rootx(), win.winfo_rooty()
            return (x - EXCLUDE_PAD, y - EXCLUDE_PAD,
                    x + win.winfo_width() + EXCLUDE_PAD, y + win.winfo_height() + EXCLUDE_PAD)
        with exclude_lock:
            exclude["menu"] = area(menu)
            exclude["circle"] = area(overlay)

    def poll():
        """Runs every 50 ms on the main thread to process hotkey commands."""
        while not commands.empty():
            cmd = commands.get()
            if cmd == "toggle":
                if overlay.state() == "withdrawn":
                    overlay.deiconify()
                    overlay.attributes("-topmost", True)
                    make_click_through(overlay)
                else:
                    overlay.withdraw()
            elif cmd == "menu":
                if menu.state() == "withdrawn":
                    menu.deiconify()
                    menu.attributes("-topmost", True)
                else:
                    menu.withdraw()
            elif cmd == "rect":  # hotkey flips the checkbox, flag stays in sync
                rect_var.set(not rect_var.get())
                apply_rect_state()
            elif cmd == "quit":
                rect_enabled.clear()
                stop_event.set()
                root.destroy()
                return
        update_exclusions()
        root.after(50, poll)

    root.after(50, poll)
    print(f"Running. {TOGGLE_HOTKEY.upper()} = toggle circle, {MENU_HOTKEY.upper()} = toggle menu, "
          f"{RECT_HOTKEY.upper()} = toggle rectangle mouse, "
          f"{QUIT_HOTKEY.upper()} or Ctrl+C = quit. Mouse in a screen corner = emergency stop.")

    # --- Run until quit ---
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        rect_enabled.clear()
        stop_event.set()
        keyboard.unhook_all()


if __name__ == "__main__":
    main()