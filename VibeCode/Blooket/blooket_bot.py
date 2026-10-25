"""
blooket_bot.py - screen-reading quiz helper for Windows.

Reads the question + 4 answers from your screen with OCR, decides on an answer
(safe math parser first, Anthropic LLM as fallback) and clicks it.

Usage:
    python blooket_bot.py --calibrate   # one-time: define the screen regions
    python blooket_bot.py --test        # read the screen once, print decision, NO click
    python blooket_bot.py               # run; toggle with Left Ctrl+Left Shift+B
    python blooket_bot.py --debug       # also save cropped images to ./debug

Only use this where it is allowed (your own games / solo practice / with the
host's permission). Don't use it to undermine other players or a graded activity.
"""

# ============================================================================
# 0. IMPORTS + WINDOWS DPI SETUP
# ============================================================================
import argparse
import ast
import ctypes
import difflib
import hashlib
import json
import logging
import math
import operator
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Make Windows report real pixels so mss and pyautogui use the same coordinates
# (otherwise display scaling at 125%/150% shifts every click).
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import cv2
import keyboard
import mss
import numpy as np
import pyautogui
import pytesseract

try:
    import winsound
except ImportError:  # not on Windows
    winsound = None

# ============================================================================
# 1. SETTINGS  (everything here can be overridden under "settings" in config.json)
# ============================================================================
DEFAULT_SETTINGS = {
    # --- hotkeys ---
    "hotkey_toggle": "left ctrl+left shift+b",
    "hotkey_quit": "ctrl+shift+q",
    "calibrate_key": "f8",            # press while hovering to record a point

    # --- timing (seconds) ---
    "poll_interval": 0.5,             # how often the screen is checked
    "stable_delay": 0.4,              # question must read the same twice (animation guard)
    "click_delay_min": 0.4,           # random "thinking" pause before moving
    "click_delay_max": 1.2,
    "move_duration_min": 0.12,        # mouse travel time per movement leg
    "move_duration_max": 0.35,
    "click_jitter_frac": 0.12,        # random offset from box center (fraction of box size)

    # --- OCR ---
    "tesseract_cmd": r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    "ocr_scale": 2.0,                 # upscale crops before OCR (helps small text)
    "tesseract_config": "--psm 6",
    "min_question_chars": 2,          # shorter OCR results are treated as "no question"
    "same_question_ratio": 0.90,       # similarity above this = same question
    "monitor_index": 1,               # mss monitor (1 = primary) used when window is null

    # --- LLM ---
    "llm_enabled": True,
    "model": "claude-haiku-5-5",      # any Anthropic model string works here
    "llm_max_tokens": 5,
    "llm_timeout": 15.0,

    # --- misc ---
    "debug": False,                   # save cropped images to debug_dir
    "debug_dir": "debug",
    "beep": True,
    "log_file": "bot.log",
}

# Default regions as FRACTIONS (left, top, right, bottom) of the window/screen.
# Running --calibrate overwrites these in config.json.
DEFAULT_REGIONS = {
    "question": [0.10, 0.12, 0.90, 0.45],
    "answers": [
        [0.05, 0.55, 0.49, 0.75],   # 1: top-left  (orange)
        [0.51, 0.55, 0.95, 0.75],   # 2: top-right (blue)
        [0.05, 0.78, 0.49, 0.98],   # 3: bottom-left (green)
        [0.51, 0.78, 0.95, 0.98],   # 4: bottom-right (red)
    ],
}
ANSWER_NAMES = ["top-left", "top-right", "bottom-left", "bottom-right"]

CONFIG_PATH = Path(__file__).with_name("config.json")

# ============================================================================
# 2. LOGGING
# ============================================================================
log = logging.getLogger("blooket")


def setup_logging(log_file):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        fh = logging.FileHandler(Path(__file__).with_name(log_file), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
        log.addHandler(fh)
    except Exception as e:
        print(f"(could not open log file: {e})")


# ============================================================================
# 3. CONFIG LOAD / SAVE
# ============================================================================
def load_config():
    cfg = {"settings": dict(DEFAULT_SETTINGS), "window": None, "regions": DEFAULT_REGIONS}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg["settings"].update(data.get("settings", {}))
            cfg["window"] = data.get("window")
            cfg["regions"] = data.get("regions", DEFAULT_REGIONS)
        except Exception as e:
            print(f"Could not read config.json ({e}); using defaults.")
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ============================================================================
# 4. SCREEN GEOMETRY + CAPTURE
# ============================================================================
def get_window_box(cfg, sct):
    """Pixel rect (l, t, r, b) that the fractional regions are relative to."""
    if cfg["window"]:
        return tuple(int(v) for v in cfg["window"])
    m = sct.monitors[cfg["settings"]["monitor_index"]]
    return (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])


def frac_to_box(frac, win):
    wl, wt, wr, wb = win
    w, h = wr - wl, wb - wt
    return (int(wl + frac[0] * w), int(wt + frac[1] * h),
            int(wl + frac[2] * w), int(wt + frac[3] * h))


def grab_gray(sct, box):
    l, t, r, b = box
    shot = sct.grab({"left": l, "top": t, "width": max(1, r - l), "height": max(1, b - t)})
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2GRAY)


# ============================================================================
# 5. OCR  (grayscale -> upscale -> blur -> Otsu threshold -> auto-invert)
# ============================================================================
def preprocess(gray, scale):
    if scale and scale != 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Make the majority colour (the background) white and the text black.
    if (th == 255).mean() < 0.5:
        th = cv2.bitwise_not(th)
    return cv2.copyMakeBorder(th, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)


def ocr_region(sct, box, s, tag=None, save=False):
    """OCR one screen box. If save is True, store raw + processed crops for debugging."""
    gray = grab_gray(sct, box)
    proc = preprocess(gray, s["ocr_scale"])
    text = pytesseract.image_to_string(proc, config=s["tesseract_config"])
    text = " ".join(text.split())
    if save and tag:
        d = Path(__file__).with_name(s["debug_dir"])
        d.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S_%f")[:-3]
        cv2.imwrite(str(d / f"{stamp}_{tag}_raw.png"), gray)
        cv2.imwrite(str(d / f"{stamp}_{tag}_processed.png"), proc)
    return text


def read_all(sct, cfg, save=False):
    """Return (question_text, [4 answer texts])."""
    s = cfg["settings"]
    win = get_window_box(cfg, sct)
    q = ocr_region(sct, frac_to_box(cfg["regions"]["question"], win), s, "question", save)
    answers = [
        ocr_region(sct, frac_to_box(r, win), s, f"answer{i + 1}", save)
        for i, r in enumerate(cfg["regions"]["answers"])
    ]
    return q, answers


# ============================================================================
# 6. SAFE MATH PARSER  (ast-based, no eval)
# ============================================================================
_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
            raise ValueError("exponent too large")
        return _BINOPS[type(node.op)](left, right)
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def normalize_math(text):
    """Clean OCR'd text into something ast can parse, or return None if it isn't math."""
    t = text.replace("×", "*").replace("÷", "/").replace("−", "-").replace("–", "-")
    t = t.replace("^", "**").replace("=", " ").replace("?", " ")
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)                      # 1,000 -> 1000
    t = re.sub(r"(?<=[\d)\s])[xX](?=[\d(\s])", "*", t)           # 2 x 3 -> 2 * 3
    t = t.strip()
    if not t or len(t) > 100:
        return None
    if not re.fullmatch(r"[\d\s+\-*/().%]+", t):
        return None
    if not re.search(r"\d\s*[+\-*/%]", t):                       # needs an operator
        return None
    return t


def safe_eval_math(text):
    """Return a float if text is a pure math expression, else None."""
    t = normalize_math(text)
    if t is None:
        return None
    try:
        val = _eval_node(ast.parse(t, mode="eval"))
        return val if math.isfinite(val) else None
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, RecursionError):
        return None


def parse_number(text):
    """'2', '2.0', '2.00', '002', '1,000', '$5' -> float; anything else -> None."""
    t = re.sub(r"[\s,$]", "", text)
    if re.fullmatch(r"-?\d+(\.\d+)?|-?\.\d+", t):
        return float(t)
    return None


def pick_by_math(question, answers):
    value = safe_eval_math(question)
    if value is None:
        return None
    for i, a in enumerate(answers):               # first match wins
        n = parse_number(a)
        if n is not None and math.isclose(n, value, rel_tol=1e-9, abs_tol=1e-9):
            return i
    return None


# ============================================================================
# 7. LLM FALLBACK  (Anthropic API, key from ANTHROPIC_API_KEY)
# ============================================================================
_client = None


def ask_llm(question, answers, s):
    global _client
    if not s["llm_enabled"]:
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.info("LLM skipped: ANTHROPIC_API_KEY is not set.")
        return None
    try:
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic(timeout=s["llm_timeout"])
        options = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(answers))
        resp = _client.messages.create(
            model=s["model"],
            max_tokens=s["llm_max_tokens"],
            system=("You answer multiple-choice quiz questions. The text was OCR'd and may "
                    "contain small typos. Reply with ONLY one digit, 1-4, for the correct option."),
            messages=[{"role": "user", "content": f"Question: {question}\n\nOptions:\n{options}"}],
        )
        reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"[1-4]", reply)
        if m:
            return int(m.group()) - 1
        log.info(f"LLM reply had no 1-4: {reply!r}")
    except Exception as e:
        log.info(f"LLM error: {e}")
    return None


def decide(question, answers, s):
    """Return (answer_index or None, method)."""
    if sum(1 for a in answers if a) < 2:
        return None, "too few answers read"
    idx = pick_by_math(question, answers)
    if idx is not None:
        return idx, "math"
    idx = ask_llm(question, answers, s)
    if idx is not None and answers[idx]:
        return idx, "llm"
    return None, "no decision"


# ============================================================================
# 8. MOUSE CONTROL  (human-ish movement, fail-safe stays ON)
# ============================================================================
pyautogui.FAILSAFE = True   # slam the mouse into a screen corner to abort
pyautogui.PAUSE = 0


def human_click(box, s):
    l, t, r, b = box
    w, h = r - l, b - t
    jx, jy = w * s["click_jitter_frac"], h * s["click_jitter_frac"]
    tx = (l + r) / 2 + random.uniform(-jx, jx)
    ty = (t + b) / 2 + random.uniform(-jy, jy)

    time.sleep(random.uniform(s["click_delay_min"], s["click_delay_max"]))

    # Leg 1: curve toward a rough waypoint; Leg 2: settle on the target.
    cx, cy = pyautogui.position()
    wx = cx + (tx - cx) * random.uniform(0.5, 0.8) + random.uniform(-30, 30)
    wy = cy + (ty - cy) * random.uniform(0.5, 0.8) + random.uniform(-30, 30)
    pyautogui.moveTo(wx, wy, random.uniform(s["move_duration_min"], s["move_duration_max"]),
                     pyautogui.easeOutQuad)
    pyautogui.moveTo(tx, ty, random.uniform(s["move_duration_min"], s["move_duration_max"]),
                     pyautogui.easeInOutQuad)
    time.sleep(random.uniform(0.05, 0.15))
    pyautogui.click()


# ============================================================================
# 9. BOT STATE + BACKGROUND WORKER
# ============================================================================
class Bot:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.s = cfg["settings"]
        self.enabled = threading.Event()
        self.quit_event = threading.Event()
        self.reset_flag = threading.Event()   # forget the last question (set on toggle ON)
        self.dry_run = dry_run

    def beep(self, freq):
        if self.s["beep"] and winsound:
            try:
                winsound.Beep(freq, 150)
            except Exception:
                pass

    def toggle(self):
        if self.enabled.is_set():
            self.enabled.clear()
            state, freq = "OFF", 500
        else:
            self.reset_flag.set()
            self.enabled.set()
            state, freq = "ON", 1100
        print(f"\n>>> BOT {state} <<<")
        log.info(f"Bot toggled {state}")
        self.beep(freq)

    def quit(self):
        print("\nQuitting...")
        self.quit_event.set()
        self.beep(400)

    def same(self, a, b):
        if not a or not b:
            return False
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= self.s["same_question_ratio"]

    def run(self):
        s = self.s
        last_text = None      # question we already answered
        pending = None        # candidate question waiting for a stable second read
        try:
            sct = mss.mss()   # create inside this thread (mss handles are thread-bound)
        except Exception as e:
            log.info(f"Screen capture init failed: {e}")
            self.quit_event.set()
            return

        while not self.quit_event.is_set():
            if not self.enabled.is_set():
                time.sleep(0.1)
                continue
            if self.reset_flag.is_set():
                self.reset_flag.clear()
                last_text, pending = None, None

            try:
                win = get_window_box(self.cfg, sct)
                q = ocr_region(sct, frac_to_box(self.cfg["regions"]["question"], win), s)

                if len(q) < s["min_question_chars"]:
                    last_text, pending = None, None        # between questions
                    time.sleep(s["poll_interval"])
                    continue
                if self.same(q, last_text):                 # already answered this one
                    time.sleep(s["poll_interval"])
                    continue
                if not self.same(q, pending):               # wait for a stable read
                    pending = q
                    time.sleep(s["stable_delay"])
                    continue

                # New, stable question -> re-read everything (saving debug crops if enabled)
                q, answers = read_all(sct, self.cfg, save=s["debug"])
                qhash = hashlib.md5(q.lower().encode()).hexdigest()[:8]
                last_text, pending = q, None
                log.info(f"[{qhash}] Q: {q}")
                for name, a in zip(ANSWER_NAMES, answers):
                    log.info(f"        {name:>12}: {a!r}")

                idx, method = decide(q, answers, s)
                if idx is None:
                    log.info(f"        -> no action ({method})")
                    continue
                log.info(f"        -> choice {idx + 1} ({ANSWER_NAMES[idx]}) via {method}: {answers[idx]!r}")

                if not self.dry_run:
                    box = frac_to_box(self.cfg["regions"]["answers"][idx], get_window_box(self.cfg, sct))
                    human_click(box, s)

            except pyautogui.FailSafeException:
                log.info("Fail-safe triggered (mouse in corner). Stopping.")
                self.enabled.clear()
                self.quit_event.set()
            except Exception as e:
                log.info(f"Loop error: {e}")
                time.sleep(1.0)


# ============================================================================
# 10. CALIBRATION MODE
# ============================================================================
def calibrate(cfg):
    s = cfg["settings"]
    key = s["calibrate_key"]

    def grab_point(prompt):
        print(f"  {prompt}  -> hover, then press [{key.upper()}]")
        keyboard.wait(key)
        time.sleep(0.3)
        p = pyautogui.position()
        print(f"     recorded ({p.x}, {p.y})")
        return p.x, p.y

    print("\n=== CALIBRATION ===")
    print("Open your Blooket question screen first. Press Ctrl+C to cancel.\n")

    fullscreen = input("Is Blooket fullscreen on your primary monitor? [y/N]: ").strip().lower() == "y"
    if fullscreen:
        window = None
        with mss.mss() as sct:
            m = sct.monitors[s["monitor_index"]]
            win = (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])
    else:
        print("\nWindow bounds (the browser's page area; ignore the purple bar by choosing regions below it):")
        x1, y1 = grab_point("TOP-LEFT corner of the Blooket window/page")
        x2, y2 = grab_point("BOTTOM-RIGHT corner of the Blooket window/page")
        window = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        win = tuple(window)

    w, h = win[2] - win[0], win[3] - win[1]

    def region(label):
        print(f"\n{label}")
        a = grab_point("TOP-LEFT corner")
        b = grab_point("BOTTOM-RIGHT corner")
        return [round((min(a[0], b[0]) - win[0]) / w, 4), round((min(a[1], b[1]) - win[1]) / h, 4),
                round((max(a[0], b[0]) - win[0]) / w, 4), round((max(a[1], b[1]) - win[1]) / h, 4)]

    regions = {"question": region("QUESTION text area"), "answers": []}
    for name in ANSWER_NAMES:
        regions["answers"].append(region(f"ANSWER box: {name}"))

    cfg["window"], cfg["regions"] = window, regions
    save_config(cfg)
    print(f"\nSaved to {CONFIG_PATH}. Run with --test --debug to check the OCR crops.")


# ============================================================================
# 11. MAIN
# ============================================================================
def main():
    ap = argparse.ArgumentParser(description="Blooket-style quiz helper")
    ap.add_argument("--calibrate", action="store_true", help="define screen regions and save them")
    ap.add_argument("--test", action="store_true", help="read the screen once; print decision; no click")
    ap.add_argument("--debug", action="store_true", help="save cropped region images to the debug folder")
    args = ap.parse_args()

    cfg = load_config()
    s = cfg["settings"]
    if args.debug:
        s["debug"] = True
    setup_logging(s["log_file"])

    if os.path.exists(s["tesseract_cmd"]):
        pytesseract.pytesseract.tesseract_cmd = s["tesseract_cmd"]
    else:
        log.info(f"Tesseract not found at {s['tesseract_cmd']} - relying on PATH. "
                 f"Set 'tesseract_cmd' in config.json if OCR fails.")

    if args.calibrate:
        try:
            calibrate(cfg)
        except KeyboardInterrupt:
            print("\nCalibration cancelled.")
        return

    if args.test:
        try:
            with mss.mss() as sct:
                q, answers = read_all(sct, cfg, save=True)
            print(f"\nQuestion: {q!r}")
            for name, a in zip(ANSWER_NAMES, answers):
                print(f"  {name:>12}: {a!r}")
            idx, method = decide(q, answers, s)
            print("\nDecision:", "none" if idx is None else f"{idx + 1} ({ANSWER_NAMES[idx]}) via {method}")
            print(f"Crops saved in ./{s['debug_dir']}")
        except Exception as e:
            print(f"Test failed: {e}")
        return

    bot = Bot(cfg)
    try:
        keyboard.add_hotkey(s["hotkey_toggle"], bot.toggle)
        keyboard.add_hotkey(s["hotkey_quit"], bot.quit)
    except Exception as e:
        print(f"Could not register hotkeys ({e}). Try running the terminal as Administrator.")
        return

    threading.Thread(target=bot.run, daemon=True).start()

    print("=" * 60)
    print(f" Toggle ON/OFF : {s['hotkey_toggle']}")
    print(f" Quit          : {s['hotkey_quit']}")
    print(" Fail-safe     : slam the mouse into a screen corner")
    print(f" LLM fallback  : {'on, model ' + s['model'] if s['llm_enabled'] else 'off'}")
    print(" Status        : OFF (waiting for hotkey)")
    print("=" * 60)

    try:
        while not bot.quit_event.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        bot.quit_event.set()
        keyboard.unhook_all()
        print("Stopped.")


if __name__ == "__main__":
    main()
