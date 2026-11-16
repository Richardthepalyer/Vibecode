"""
Auto Chat - repeatedly pastes your messages into whatever text box is active
and presses Enter.  Windows only, Python 3.10+.

Install:   pip install keyboard pyperclip
Run:       python auto_chat.py
           (run as Administrator if the target app is itself running as admin;
            Windows blocks simulated keystrokes into higher-privilege windows)

Architecture
------------
* ``AutoChatApp``  - tkinter UI (main thread only). Send tab uses the clean,
                     simple layout of the original Auto Chat; every option of
                     the newer versions is tucked into that layout.
* ``SendWorker``   - background thread that owns the clipboard and sends keys.
* ``ui_queue``     - the ONLY channel from the worker / hotkey thread to the UI.
* ``Settings``     - immutable snapshot of every option; replaced atomically
                     so the worker can read it without locks.
* ``HistoryStore`` - per-message send history (use count + last used), kept in
                     its own JSON file (auto_chat_history.json) next to the script.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace
from typing import Any, Callable, Sequence

try:
    import keyboard
    import pyperclip
except ImportError as _exc:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "Auto Chat - missing package",
        f"Could not import '{_exc.name}'.\n\nInstall the requirements with:\n"
        "    pip install keyboard pyperclip",
    )
    sys.exit(1)

# ============================== BLACKLIST ===================================
# Words that are NOT allowed in the message box.
#
# HOW TO USE: put each word or phrase in quotes, separated by commas, one per
# line. Remove the "#" in front of an example to use it, or add your own.
# Matching ignores UPPER/lower case, and it also catches the word inside longer
# words (so "ass" would also be removed from "class") - avoid short words that
# are commonly part of normal words.
#
# When someone types, pastes or loads one of these, it is erased from the box
# and the app shows: 'The word "..." is blacklisted.'
BLACKLISTED_WORDS = [
    "Nigger",
    "Faggot",
    "Adolf Hitler",
    "Nigga",
    "jeffrey epstein",
    "Osyreo is gay",
    "Benjamin Netanyahu",
    "Big yahu",
    "Benjamin Netanyahu is my king",
    "jews",
    "jew",
    "yahu"
]
# ============================================================================


# ============================== SETTINGS ====================================
APP_NAME = "Auto Chat"
SETTINGS_FILE = Path(__file__).with_name("auto_chat_settings.json")  # auto-loaded
SETTINGS_VERSION = 1
HISTORY_FILE = Path(__file__).with_name("auto_chat_history.json")  # auto-loaded
HISTORY_SAVE_INTERVAL = 5.0    # max seconds between history writes while sending

# --- Hotkeys (defaults; changeable from the UI) ---
HOTKEY_NAMES = ("toggle", "pause", "quit")
DEFAULT_HOTKEYS = {"toggle": "f8", "pause": "f9", "quit": "alt+q"}
HOTKEY_LABELS = {"toggle": "Start / Stop", "pause": "Pause / Resume",
                 "quit": "Quit (failsafe)"}
HOTKEY_HINT_WORDS = {"toggle": "start/stop", "pause": "pause", "quit": "quit"}
HOTKEY_DEBOUNCE = 0.3          # ignore repeat triggers of one hotkey faster than this
STALE_HOTKEY_SECONDS = 1.0     # drop queued hotkey events older than this
CAPTURE_TIMEOUT_MS = 8000      # "Record" gives up after this long

# --- Sending ---
DEFAULT_DELAY_MIN = 1.0        # seconds between messages (fixed delay / range low)
DEFAULT_DELAY_MAX = 2.0        # range high (used when "random range" is on)
MIN_DELAY = 0.2               # lowest delay allowed
MAX_DELAY = 3600.0             # highest delay allowed
MAX_SEND_LIMIT = 1_000_000
MAX_TIMER_MINUTES = 1440.0
START_DELAY = 1                # countdown seconds so you can click your chat box
PASTE_TO_ENTER_GAP = 0.05      # tiny pause between Ctrl+V and Enter
CLIPBOARD_SETTLE = 0.15        # let the target app finish reading the clipboard
                               # before we restore it after the final message
WAIT_POLL = 0.25               # re-check interval while our own window has focus
SLEEP_SLICE = 0.05             # pause/stop responsiveness while waiting
KEY_RELEASE_TIMEOUT = 1.0      # wait this long for held modifier keys to be released
MODIFIER_KEYS = ("ctrl", "alt", "shift", "windows")

# --- Clipboard ---
CLIPBOARD_RETRIES = 5
CLIPBOARD_RETRY_WAIT = 0.03
MAX_CLIPBOARD_FAILURES = 3     # consecutive failed copies before giving up

# --- UI / threading ---
POLL_MS = 50                   # how often the UI drains the queue
WORKER_JOIN_TIMEOUT = 2.0      # max wait for the worker (clipboard restore) on exit
NOTICE_MS = 5000               # how long transient notices stay visible

FONT = "Segoe UI"


@dataclass(frozen=True)
class Theme:
    """Colour palette. Every widget colour comes from one of these."""
    bg: str
    card: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    stop: str
    stop_hover: str
    disabled: str
    disabled_text: str
    green: str
    amber: str
    grey_dot: str
    error: str


DARK = Theme(
    bg="#14161a", card="#1c1f26", border="#2b303b", text="#e8eaed",
    muted="#8b93a1", accent="#ff4d5e", accent_hover="#ff6675",
    stop="#2b303b", stop_hover="#363c49", disabled="#232730",
    disabled_text="#5b6270", green="#34d399", amber="#fbbf24",
    grey_dot="#5b6270", error="#f87171",
)
LIGHT = Theme(
    bg="#f4f5f7", card="#ffffff", border="#d5d9e0", text="#1b1f27",
    muted="#667085", accent="#e63950", accent_hover="#f0566a",
    stop="#dfe3ea", stop_hover="#d0d5de", disabled="#e6e9ee",
    disabled_text="#a0a7b4", green="#12a36b", amber="#c77f00",
    grey_dot="#a0a7b4", error="#d92d20",
)
# ============================================================================


# ------------------------------ Win32 helpers -------------------------------
class Win32:
    """Small ctypes wrappers. Loaded lazily so importing never fails elsewhere."""

    ERROR_ACCESS_DENIED = 5
    _api: SimpleNamespace | None = None

    @classmethod
    def _load(cls) -> SimpleNamespace:
        if cls._api is None:
            from ctypes import wintypes as wt

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)

            user32.GetForegroundWindow.argtypes = []
            user32.GetForegroundWindow.restype = wt.HWND
            user32.GetParent.argtypes = [wt.HWND]
            user32.GetParent.restype = wt.HWND
            user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
            user32.GetWindowThreadProcessId.restype = wt.DWORD
            kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            kernel32.OpenProcess.restype = wt.HANDLE
            kernel32.CloseHandle.argtypes = [wt.HANDLE]
            kernel32.CloseHandle.restype = wt.BOOL
            advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
            advapi32.OpenProcessToken.restype = wt.BOOL
            advapi32.GetTokenInformation.argtypes = [
                wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
            advapi32.GetTokenInformation.restype = wt.BOOL
            dwmapi.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
            dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
            shell32.IsUserAnAdmin.argtypes = []
            shell32.IsUserAnAdmin.restype = wt.BOOL

            cls._api = SimpleNamespace(
                wt=wt, user32=user32, kernel32=kernel32, advapi32=advapi32,
                dwmapi=dwmapi, shell32=shell32)
        return cls._api

    @classmethod
    def is_admin(cls) -> bool:
        """True if this process is running elevated."""
        try:
            return bool(cls._load().shell32.IsUserAnAdmin())
        except Exception:
            return False

    @classmethod
    def foreground_pid(cls) -> int | None:
        """Process id owning the foreground window, or None if there is none
        (lock screen, UAC prompt, desktop switch...)."""
        api = cls._load()
        hwnd = api.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = api.wt.DWORD()
        api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None

    @classmethod
    def is_elevated(cls, pid: int) -> bool:
        """True if *pid* is a higher-privilege (admin) process we can't type into.

        Conservative: if the process can't even be inspected we say False, so a
        false alarm never blocks sending.
        """
        try:
            api = cls._load()
            wt = api.wt
            handle = api.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
            if not handle:
                return False
            try:
                token = wt.HANDLE()
                if not api.advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
                    return ctypes.get_last_error() == cls.ERROR_ACCESS_DENIED
                try:
                    elevation, size = wt.DWORD(), wt.DWORD()
                    ok = api.advapi32.GetTokenInformation(
                        token, 20, ctypes.byref(elevation),  # 20 = TokenElevation
                        ctypes.sizeof(elevation), ctypes.byref(size))
                    return bool(ok and elevation.value)
                finally:
                    api.kernel32.CloseHandle(token)
            finally:
                api.kernel32.CloseHandle(handle)
        except Exception:
            return False

    @classmethod
    def set_dark_titlebar(cls, hwnd: int, dark: bool) -> None:
        """Ask Windows 10/11 for a dark/light title bar (purely cosmetic)."""
        try:
            api = cls._load()
            value = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):  # 20 = Win11/new Win10, 19 = older Win10
                api.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass

    @classmethod
    def toplevel_hwnd(cls, widget: tk.Misc) -> int:
        """Real top-level window handle of a tk root/Toplevel."""
        try:
            return int(widget.frame(), 16)
        except Exception:
            wid = widget.winfo_id()
            try:
                return cls._load().user32.GetParent(wid) or wid
            except Exception:
                return wid


# -------------------------------- settings ----------------------------------
class SettingsError(Exception):
    """Raised for unreadable / invalid / unwritable settings files."""


def validate_hotkeys(combos: dict[str, Any]) -> str | None:
    """Return an error message, or None if all three hotkeys are usable."""
    seen: dict[str, str] = {}
    for name in HOTKEY_NAMES:
        combo = combos.get(name)
        label = HOTKEY_LABELS[name]
        if not isinstance(combo, str) or not combo.strip():
            return f"{label}: enter a hotkey."
        try:
            key = repr(keyboard.parse_hotkey(combo.strip()))
        except Exception:
            return f"{label}: '{combo}' is not a valid hotkey."
        if key in seen:
            return f"{label} and {seen[key]} use the same hotkey."
        seen[key] = label
    return None


def _number(data: dict[str, Any], key: str, default: float,
            lo: float, hi: float) -> float:
    """Read a finite number from *data*, clamped to [lo, hi]."""
    try:
        value = float(data.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, lo), hi)


@dataclass(frozen=True)
class Settings:
    """Everything the user can configure. Immutable: replace, never mutate."""
    messages: tuple[str, ...] = ()
    random_order: bool = False
    random_delay: bool = False
    delay_min: float = DEFAULT_DELAY_MIN
    delay_max: float = DEFAULT_DELAY_MAX
    send_limit: int = 0                 # 0 = unlimited
    timer_minutes: float = 0.0          # 0 = no timer
    hotkeys: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_HOTKEYS))
    dark_mode: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "version": SETTINGS_VERSION,
            "messages": list(self.messages),
            "random_order": self.random_order,
            "random_delay": self.random_delay,
            "delay_min": self.delay_min,
            "delay_max": self.delay_max,
            "send_limit": self.send_limit,
            "timer_minutes": self.timer_minutes,
            "hotkeys": dict(self.hotkeys),
            "dark_mode": self.dark_mode,
        }

    @classmethod
    def from_json(cls, data: Any) -> "Settings":
        """Build Settings from untrusted JSON; bad fields fall back to defaults."""
        if not isinstance(data, dict):
            raise SettingsError("The file must contain a JSON object.")

        raw_msgs = data.get("messages", [])
        if isinstance(raw_msgs, str):
            raw_msgs = raw_msgs.splitlines()
        messages: tuple[str, ...] = ()
        if isinstance(raw_msgs, list):
            messages = tuple(m.strip() for m in raw_msgs
                             if isinstance(m, str) and m.strip())

        hotkeys = dict(DEFAULT_HOTKEYS)
        raw_keys = data.get("hotkeys")
        if isinstance(raw_keys, dict):
            candidate = {n: raw_keys.get(n, DEFAULT_HOTKEYS[n]) for n in HOTKEY_NAMES}
            if validate_hotkeys(candidate) is None:
                hotkeys = {n: candidate[n].strip().lower() for n in HOTKEY_NAMES}

        return cls(
            messages=messages,
            random_order=bool(data.get("random_order", False)),
            random_delay=bool(data.get("random_delay", False)),
            delay_min=_number(data, "delay_min", DEFAULT_DELAY_MIN, MIN_DELAY, MAX_DELAY),
            delay_max=_number(data, "delay_max", DEFAULT_DELAY_MAX, MIN_DELAY, MAX_DELAY),
            send_limit=int(_number(data, "send_limit", 0, 0, MAX_SEND_LIMIT)),
            timer_minutes=_number(data, "timer_minutes", 0.0, 0.0, MAX_TIMER_MINUTES),
            hotkeys=hotkeys,
            dark_mode=bool(data.get("dark_mode", True)),
        )


def load_settings(path: Path) -> Settings:
    """Read and validate a settings file."""
    try:
        # utf-8-sig: tolerate the BOM that Windows Notepad adds to "UTF-8" files
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:     # ValueError covers bad JSON/encoding
        raise SettingsError(f"Could not read {path.name}: {exc}") from exc
    return Settings.from_json(data)


def save_settings(path: Path, settings: Settings) -> None:
    """Write settings atomically so a crash can't leave a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(settings.to_json(), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise SettingsError(f"Could not save {path.name}: {exc}") from exc


# --------------------------------- history ----------------------------------
class HistoryStore:
    """Per-message send history: ``{message: {"count": int, "last": unix_time}}``.

    Kept in its own JSON file (same format as the old Auto Chat history, so an
    existing ``auto_chat_history.json`` keeps working). Main thread only.
    Writes are atomic and throttled, so sending fast never hammers the disk.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._last_save = 0.0

    def load(self) -> str | None:
        """Read the file. Returns a warning if it had to be ignored, else None."""
        self.entries = {}
        self._dirty = False
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
        except (OSError, ValueError) as exc:
            self._set_aside()
            return f"Could not read {self.path.name} ({exc}). Starting with empty history."
        for raw_text, info in data.items():
            text = str(raw_text).strip()
            if not text or not isinstance(info, dict):
                continue                                  # skip junk, keep the rest
            count = int(_number(info, "count", 0, 0, 10**9))
            last = _number(info, "last", 0.0, 0.0, 4e9)
            entry = self.entries.setdefault(text, {"count": 0, "last": 0.0})
            entry["count"] += count                       # merges keys that differ
            entry["last"] = max(entry["last"], last)      # only by whitespace
        return None

    def _set_aside(self) -> None:
        """Keep an unreadable file as *.bad instead of overwriting it later."""
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".bad"))
        except OSError:
            pass

    def record(self, text: str, stamp: float) -> None:
        entry = self.entries.setdefault(text, {"count": 0, "last": 0.0})
        entry["count"] += 1
        entry["last"] = stamp
        self._dirty = True

    def clear(self) -> None:
        self.entries.clear()
        self._dirty = True

    def sorted_items(self) -> list[tuple[str, dict[str, Any]]]:
        """Most used first; ties broken by most recently used."""
        return sorted(self.entries.items(),
                      key=lambda kv: (-kv[1]["count"], -kv[1]["last"]))

    def total_sends(self) -> int:
        return sum(int(v["count"]) for v in self.entries.values())

    def flush(self, force: bool = False) -> str | None:
        """Write to disk if there are changes. Returns an error message or None."""
        if not self._dirty:
            return None
        now = time.monotonic()
        if not force and now - self._last_save < HISTORY_SAVE_INTERVAL:
            return None
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            self._last_save = now                          # don't retry on every send
            return f"Could not save {self.path.name}: {exc}"
        self._dirty = False
        self._last_save = now
        return None


# ------------------------ clipboard / picking / timing ----------------------
class ClipboardError(Exception):
    """The clipboard could not be read or written (e.g. locked by another app)."""


def _retry(func: Callable[..., Any], *args: Any) -> Any:
    """Call a pyperclip function, retrying briefly if the clipboard is locked."""
    last: Exception | None = None
    for _ in range(CLIPBOARD_RETRIES):
        try:
            return func(*args)
        except (pyperclip.PyperclipException, OSError) as exc:
            last = exc
            time.sleep(CLIPBOARD_RETRY_WAIT)
    raise ClipboardError(str(last) if last else "clipboard unavailable")


class ClipboardGuard:
    """Owns the clipboard during a run: snapshot, set + verify, restore.

    Only text can be restored (pyperclip limitation); images/files that were on
    the clipboard before the run can't be recovered.
    """

    def __init__(self) -> None:
        self._original: str | None = None
        self._last_set: str | None = None

    def snapshot(self) -> None:
        """Remember what the user had on the clipboard."""
        self._original = _retry(pyperclip.paste)
        self._last_set = None

    def set(self, text: str) -> None:
        """Put *text* on the clipboard and confirm it really got there."""
        _retry(pyperclip.copy, text)
        for _ in range(CLIPBOARD_RETRIES):
            if _retry(pyperclip.paste) == text:
                self._last_set = text
                return
            time.sleep(CLIPBOARD_RETRY_WAIT)
        raise ClipboardError("another program keeps changing the clipboard")

    def restore(self) -> bool:
        """Put the original text back. Returns False only if that failed.

        If the clipboard no longer holds our text, the user copied something
        new during the run - we leave that alone.
        """
        if self._original is None or self._last_set is None:
            self._original = self._last_set = None
            return True
        original, last_set = self._original, self._last_set
        self._original = self._last_set = None
        try:
            if _retry(pyperclip.paste) == last_set:
                _retry(pyperclip.copy, original)
        except ClipboardError:
            return False
        return True


class MessagePicker:
    """Chooses the next message: in order, or shuffled without immediate repeats."""

    def __init__(self) -> None:
        self._index = 0
        self._bag: list[int] = []
        self._bag_size = 0
        self._last = -1

    def next(self, messages: Sequence[str], randomize: bool) -> str:
        count = len(messages)
        if randomize:
            if not self._bag or self._bag_size != count:
                self._refill(count)
            idx = self._bag.pop()
        else:
            idx = self._index % count
            self._index = idx + 1
        self._last = idx
        return messages[idx]

    def _refill(self, count: int) -> None:
        bag = list(range(count))
        random.shuffle(bag)
        if count > 1 and bag[-1] == self._last:   # don't repeat across reshuffles
            bag[0], bag[-1] = bag[-1], bag[0]
        self._bag, self._bag_size = bag, count


class ActiveClock:
    """Measures time spent actively sending (pauses and countdown excluded)."""

    def __init__(self) -> None:
        self._accumulated = 0.0
        self._since: float | None = None

    def resume(self) -> None:
        if self._since is None:
            self._since = time.monotonic()

    def pause(self) -> None:
        if self._since is not None:
            self._accumulated += time.monotonic() - self._since
            self._since = None

    def elapsed(self) -> float:
        running = time.monotonic() - self._since if self._since is not None else 0.0
        return self._accumulated + running


# ------------------------------ worker thread -------------------------------
class SendWorker(threading.Thread):
    """Background sender. Never touches tkinter; reports via *post*.

    Messages posted (all tuples, tagged with run_id so stale ones are ignored):
        ("phase", run_id, name, detail)   name: countdown|running|waiting|blocked|paused
        ("sent", run_id, count, message, unix_time)
        ("finished", run_id, reason, is_error)    always the last message
    """

    def __init__(self, run_id: int, get_settings: Callable[[], Settings],
                 post: Callable[[tuple[Any, ...]], None],
                 own_pid: int, we_are_admin: bool) -> None:
        super().__init__(name=f"AutoChatWorker-{run_id}", daemon=True)
        self.run_id = run_id
        self.stop_event = threading.Event()   # set = stop NOW (wakes every wait)
        self._gate = threading.Event()        # set = running, clear = paused
        self._gate.set()
        self._get_settings = get_settings
        self._post = post
        self._own_pid = own_pid
        self._we_are_admin = we_are_admin
        self._shown: tuple[str, str] | None = None

    # ---- controls (safe to call from any thread) ----
    @property
    def paused(self) -> bool:
        return not self._gate.is_set()

    def pause(self) -> None:
        if not self.stop_event.is_set():
            self._gate.clear()

    def resume(self) -> None:
        self._gate.set()

    def request_stop(self) -> None:
        self.stop_event.set()
        self._gate.set()                      # also wakes a paused worker

    # ---- thread body ----
    def run(self) -> None:
        guard, clock, picker = ClipboardGuard(), ActiveClock(), MessagePicker()
        reason, is_error = "Stopped", False
        try:
            try:
                reason = self._loop(guard, clock, picker)
            except Exception as exc:          # keyboard / pyperclip / anything else
                reason, is_error = self._describe_error(exc), True
            clock.pause()
            if not guard.restore():
                reason += " (clipboard could not be restored)"
        finally:
            self._post(("finished", self.run_id, reason, is_error))

    def _loop(self, guard: ClipboardGuard, clock: ActiveClock,
              picker: MessagePicker) -> str:
        stop = self.stop_event
        for n in range(START_DELAY, 0, -1):          # countdown: click your chat box
            self._phase("countdown", str(n))
            if stop.wait(1.0):
                return "Stopped"

        self._phase("running")
        clock.resume()
        self._wait_keys_released()
        guard.snapshot()

        sent = failures = 0
        pending: str | None = None        # picked but not pasted yet; retried, not skipped
        while not stop.is_set():
            if not self._gate.is_set():
                if not self._hold_paused(guard, clock):
                    break
                continue

            cfg = self._get_settings()
            if not cfg.messages:
                return "No messages to send"
            if cfg.send_limit and sent >= cfg.send_limit:
                return f"Send limit reached ({sent})"
            left = self._time_left(cfg, clock)
            if left is not None and left <= 0:
                return "Timer finished"

            state = self._target_state()
            if state != "ok":                         # never paste into ourselves etc.
                self._phase("blocked" if state == "elevated" else "waiting", state)
                self._sleep(WAIT_POLL)
                continue
            self._phase("running")

            try:
                if pending is None:
                    pending = picker.next(cfg.messages, cfg.random_order)
                guard.set(pending)
            except ClipboardError:
                failures += 1
                if failures >= MAX_CLIPBOARD_FAILURES:
                    raise
                self._sleep(WAIT_POLL)
                continue
            failures = 0

            if stop.is_set() or self._target_state() != "ok":   # focus moved: retry later
                self._sleep(WAIT_POLL)
                continue
            message, pending = pending, None
            keyboard.send("ctrl+v")
            if stop.wait(PASTE_TO_ENTER_GAP):
                break
            if self._target_state() != "ok":                    # focus moved: no Enter
                continue
            keyboard.send("enter")
            sent += 1
            self._post(("sent", self.run_id, sent, message, time.time()))

            if cfg.send_limit and sent >= cfg.send_limit:
                # Done: finish now instead of idling through one more (possibly
                # very long) delay with the clipboard still taken over.
                self._sleep(CLIPBOARD_SETTLE)
                return f"Send limit reached ({sent})"

            delay = self._pick_delay(cfg)
            left = self._time_left(cfg, clock)
            if left is not None:
                delay = min(delay, max(left, 0.0))
            self._sleep(delay)
        return "Stopped"

    # ---- helpers ----
    def _phase(self, name: str, detail: str = "") -> None:
        """Post a phase change (de-duplicated so the queue isn't flooded)."""
        if (name, detail) != self._shown:
            self._shown = (name, detail)
            self._post(("phase", self.run_id, name, detail))

    def _sleep(self, seconds: float) -> bool:
        """Wait up to *seconds*. True if cut short by stop or pause."""
        deadline = time.monotonic() + seconds
        while True:
            if self.stop_event.is_set() or not self._gate.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.stop_event.wait(min(remaining, SLEEP_SLICE))

    def _hold_paused(self, guard: ClipboardGuard, clock: ActiveClock) -> bool:
        """Block while paused (clipboard handed back). False if stopped meanwhile."""
        clock.pause()
        guard.restore()
        self._phase("paused")
        while not self._gate.wait(0.1):
            if self.stop_event.is_set():
                return False
        if self.stop_event.is_set():
            return False
        self._phase("running")
        clock.resume()
        self._wait_keys_released()
        guard.snapshot()
        return True

    def _wait_keys_released(self) -> None:
        """Let go of held modifiers first, or Ctrl+V could become Alt+Ctrl+V etc."""
        deadline = time.monotonic() + KEY_RELEASE_TIMEOUT
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                if not any(keyboard.is_pressed(k) for k in MODIFIER_KEYS):
                    return
            except Exception:
                return
            self.stop_event.wait(0.02)

    def _target_state(self) -> str:
        """'ok' | 'own' (our window) | 'none' (no window) | 'elevated' (admin app)."""
        pid = Win32.foreground_pid()
        if pid is None:
            return "none"
        if pid == self._own_pid:
            return "own"
        if not self._we_are_admin and Win32.is_elevated(pid):
            return "elevated"
        return "ok"

    @staticmethod
    def _pick_delay(cfg: Settings) -> float:
        if cfg.random_delay:
            lo, hi = sorted((cfg.delay_min, cfg.delay_max))
            return random.uniform(lo, hi)
        return cfg.delay_min

    @staticmethod
    def _time_left(cfg: Settings, clock: ActiveClock) -> float | None:
        if cfg.timer_minutes <= 0:
            return None
        return cfg.timer_minutes * 60.0 - clock.elapsed()

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        if isinstance(exc, (ClipboardError, pyperclip.PyperclipException)):
            return f"Clipboard error: {exc}"
        if isinstance(exc, PermissionError) or \
                getattr(exc, "winerror", None) == Win32.ERROR_ACCESS_DENIED:
            return "Access denied - try running Auto Chat as Administrator"
        return f"{type(exc).__name__}: {exc}"


# --------------------------------- widgets ----------------------------------
class RoundButton(tk.Canvas):
    """Flat rounded button on a canvas. Roles: primary | secondary | disabled."""

    def __init__(self, parent: tk.Misc, command: Callable[[], None],
                 width: int = 300, height: int = 46, radius: int = 12) -> None:
        super().__init__(parent, width=width, height=height,
                         highlightthickness=0, bd=0)
        self.command = command
        self.w, self.h, self.r = width, height, radius
        self.text, self.role = "Start", "primary"
        self.theme = DARK
        self.hovering = False
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonRelease-1>", self._click)

    @property
    def enabled(self) -> bool:
        return self.role != "disabled"

    def apply_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.configure(bg=theme.bg)
        self._draw()

    def set_state(self, text: str, role: str) -> None:
        self.text, self.role = text, role
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._draw()

    def _hover(self, value: bool) -> None:
        self.hovering = value
        self._draw()

    def _click(self, event: tk.Event) -> None:
        if self.enabled and 0 <= event.x <= self.w and 0 <= event.y <= self.h:
            self.command()

    def _draw(self) -> None:
        t = self.theme
        fill, hover, fg = {
            "primary": (t.accent, t.accent_hover, "white"),
            "secondary": (t.stop, t.stop_hover, t.text),
            "disabled": (t.disabled, t.disabled, t.disabled_text),
        }[self.role]
        w, h, r = self.w, self.h, self.r
        pts = [r, 0, w - r, 0, w, 0, w, r, w, h - r, w, h,
               w - r, h, r, h, 0, h, 0, h - r, 0, r, 0, 0]
        color = hover if (self.hovering and self.enabled) else fill
        self.delete("all")
        self.create_polygon(pts, smooth=True, fill=color, outline=color)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg,
                         font=(FONT, 12, "bold"))


class Stepper(tk.Frame):
    """  [-] [ value ] [+]  with key-by-key validation and clamping.

    Only digits (and one decimal point) can be typed, so NaN/inf/negative/text
    never get in. ``value`` is always a valid in-range number.
    """

    def __init__(self, parent: tk.Misc, *, value: float, minimum: float,
                 maximum: float, step: float, decimals: int,
                 on_change: Callable[[], None] | None = None,
                 entry_width: int = 5, font_size: int = 11) -> None:
        super().__init__(parent, padx=1, pady=1)
        self._min, self._max, self._step = minimum, maximum, step
        self._font_size = font_size
        self._decimals = decimals
        self._on_change = on_change
        self._theme = DARK
        digits = r"\d{0,7}"
        self._pattern = re.compile(
            rf"^{digits}$" if decimals == 0 else rf"^{digits}(\.\d{{0,{decimals}}})?$")
        self._value: float = self._clamp(value)

        self.inner = tk.Frame(self)
        self.inner.pack()
        self.minus = self._button("\u2212", -step)
        self.minus.pack(side="left")
        self.var = tk.StringVar(value=self._format(self._value))
        self.entry = tk.Entry(
            self.inner, textvariable=self.var, width=entry_width, justify="center",
            relief="flat", font=(FONT, font_size), highlightthickness=0, validate="key",
            validatecommand=(self.register(self._validate), "%P"))
        self.entry.pack(side="left", ipady=4)
        self.plus = self._button("+", step)
        self.plus.pack(side="left")
        self.var.trace_add("write", self._on_edit)
        self.entry.bind("<FocusOut>", lambda e: self._normalize())
        self.entry.bind("<Return>", lambda e: self._normalize())

    @property
    def value(self) -> float:
        return int(self._value) if self._decimals == 0 else self._value

    def set(self, value: float) -> None:
        self._value = self._clamp(value)
        self.var.set(self._format(self._value))

    def apply_theme(self, t: Theme) -> None:
        self._theme = t
        self.configure(bg=t.border)
        self.inner.configure(bg=t.card)
        self.entry.configure(bg=t.card, fg=t.text, insertbackground=t.text)
        for lbl in (self.minus, self.plus):
            lbl.configure(bg=t.card, fg=t.text)

    def _button(self, symbol: str, delta: float) -> tk.Label:
        lbl = tk.Label(self.inner, text=symbol, width=2, font=(FONT, self._font_size + 1),
                       cursor="hand2")
        lbl.bind("<Enter>", lambda e: lbl.configure(bg=self._theme.border))
        lbl.bind("<Leave>", lambda e: lbl.configure(bg=self._theme.card))
        lbl.bind("<Button-1>", lambda e: self.set(round(self._value + delta, 2)))
        return lbl

    def _clamp(self, value: float) -> float:
        value = min(max(value, self._min), self._max)
        return float(int(round(value))) if self._decimals == 0 \
            else round(value, self._decimals)

    def _format(self, value: float) -> str:
        return str(int(value)) if self._decimals == 0 else str(round(value, self._decimals))

    def _validate(self, proposed: str) -> bool:
        return bool(self._pattern.match(proposed))

    def _on_edit(self, *_: Any) -> None:
        try:
            parsed = float(self.var.get())
        except ValueError:
            return                       # empty / "." while typing: keep last value
        self._value = self._clamp(parsed)
        if self._on_change:
            self._on_change()

    def _normalize(self) -> None:
        """Tidy the text once the user finishes editing."""
        self.var.set(self._format(self._value))


class Toggle(tk.Label):
    """Checkbox drawn as a label (native checkboxes can't be themed on Windows)."""

    def __init__(self, parent: tk.Misc, text: str, variable: tk.BooleanVar,
                 command: Callable[[], None] | None = None) -> None:
        super().__init__(parent, font=(FONT, 9), cursor="hand2", anchor="w")
        self._text, self.var, self.command = text, variable, command
        self._theme = DARK
        self.bind("<Button-1>", self._click)
        variable.trace_add("write", lambda *_: self._render())
        self._render()

    def apply_theme(self, t: Theme) -> None:
        self._theme = t
        self.configure(bg=t.bg)
        self._render()

    def _click(self, _event: tk.Event) -> None:
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _render(self) -> None:
        on = self.var.get()
        self.configure(text=("\u2611  " if on else "\u2610  ") + self._text,
                       fg=self._theme.accent if on else self._theme.muted)


class LinkButton(tk.Label):
    """Small text button that highlights on hover."""

    def __init__(self, parent: tk.Misc, text: str,
                 command: Callable[[], None]) -> None:
        super().__init__(parent, text=text, font=(FONT, 9), cursor="hand2")
        self._theme = DARK
        self._enabled = True
        self.bind("<Button-1>", lambda e: command() if self._enabled else None)
        self.bind("<Enter>", lambda e: self.configure(
            fg=self._theme.accent if self._enabled else self._theme.disabled_text))
        self.bind("<Leave>", lambda e: self._paint_idle())

    def apply_theme(self, t: Theme) -> None:
        self._theme = t
        self.configure(bg=t.bg)
        self._paint_idle()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._paint_idle()

    def _paint_idle(self) -> None:
        self.configure(fg=self._theme.text if self._enabled else self._theme.disabled_text)


class TabBar(tk.Frame):
    """Underlined text tabs (e.g. Send | History)."""

    def __init__(self, parent: tk.Misc, names: Sequence[str],
                 on_select: Callable[[str], None]) -> None:
        super().__init__(parent)
        self._theme = DARK
        self._active = names[0]
        self._cols: list[tk.Frame] = []
        self._labels: dict[str, tk.Label] = {}
        self._lines: dict[str, tk.Frame] = {}
        for name in names:
            col = tk.Frame(self)
            col.pack(side="left", padx=(0, 20))
            label = tk.Label(col, text=name, font=(FONT, 10, "bold"), cursor="hand2")
            label.pack()
            line = tk.Frame(col, height=2)
            line.pack(fill="x", pady=(5, 0))
            label.bind("<Button-1>", lambda e, n=name: on_select(n))
            self._cols.append(col)
            self._labels[name], self._lines[name] = label, line

    def select(self, name: str) -> None:
        self._active = name
        self._render()

    def apply_theme(self, t: Theme) -> None:
        self._theme = t
        self.configure(bg=t.bg)
        for col in self._cols:
            col.configure(bg=t.bg)
        self._render()

    def _render(self) -> None:
        t = self._theme
        for name, label in self._labels.items():
            active = name == self._active
            label.configure(bg=t.bg, fg=t.text if active else t.muted)
            self._lines[name].configure(bg=t.accent if active else t.bg)


# ------------------------------ hotkey dialog -------------------------------
class HotkeyDialog(tk.Toplevel):
    """Edit the three global hotkeys: type them or press 'Record'."""

    def __init__(self, app: "AutoChatApp") -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Hotkeys")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.transient(app.root)
        app.style(self, bg="bg")

        body = tk.Frame(self)
        app.style(body, bg="bg")
        body.pack(padx=18, pady=16)

        self.vars: dict[str, tk.StringVar] = {}
        self.record_buttons: dict[str, LinkButton] = {}
        for row, name in enumerate(HOTKEY_NAMES):
            label = tk.Label(body, text=HOTKEY_LABELS[name], font=(FONT, 9), anchor="w")
            app.style(label, bg="bg", fg="text")
            label.grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=app.hotkeys.get(name, DEFAULT_HOTKEYS[name]))
            self.vars[name] = var
            entry = tk.Entry(body, textvariable=var, width=16, relief="flat",
                             font=(FONT, 10), highlightthickness=1)
            app.style(entry, bg="card", fg="text", insertbackground="text",
                      highlightbackground="border", highlightcolor="accent")
            entry.grid(row=row, column=1, padx=10, ipady=4)
            button = LinkButton(body, "Record", lambda n=name: self.record(n))
            app.register_custom(button)
            button.grid(row=row, column=2)
            self.record_buttons[name] = button

        self.message = tk.Label(body, text="Press Record, then the key combination "
                                "(Esc cancels).", font=(FONT, 8), wraplength=300,
                                justify="left", anchor="w")
        app.style(self.message, bg="bg", fg="muted")
        self.message.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 8))

        buttons = tk.Frame(body)
        app.style(buttons, bg="bg")
        buttons.grid(row=4, column=0, columnspan=3, sticky="e")
        for text, command in (("Reset defaults", self.reset), ("Apply", self.apply),
                              ("Close", self.close)):
            link = LinkButton(buttons, text, command)
            app.register_custom(link)
            link.pack(side="left", padx=(12, 0))

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.update()
        Win32.set_dark_titlebar(Win32.toplevel_hwnd(self), app.theme is DARK)

    def say(self, text: str, error: bool = False) -> None:
        self.message.configure(text=text,
                               fg=self.app.theme.error if error else self.app.theme.muted)

    def record(self, name: str) -> None:
        self.record_buttons[name].configure(text="Press keys...")
        self.say("Press the key combination now...")
        self.app.begin_capture(lambda combo, err, n=name: self._captured(n, combo, err))

    def _captured(self, name: str, combo: str | None, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.record_buttons[name].configure(text="Record")
        if combo:
            self.vars[name].set(combo)
            self.say(f"{combo} recorded - press Apply to use it.")
        else:
            self.say(error or "Recording cancelled.", error=bool(error))

    def reset(self) -> None:
        for name in HOTKEY_NAMES:
            self.vars[name].set(DEFAULT_HOTKEYS[name])
        self.say("Defaults filled in - press Apply.")

    def apply(self) -> None:
        error = self.app.apply_hotkeys({n: v.get() for n, v in self.vars.items()})
        if error:
            self.say(error, error=True)
        else:
            self.say("Hotkeys updated.")

    def close(self) -> None:
        self.app.cancel_capture()
        self.app.dialog = None
        self.destroy()


# ---------------------------------- main app --------------------------------
PAUSABLE_PHASES = ("running", "waiting", "blocked", "paused")


class AutoChatApp:
    """The tkinter front end. Everything here runs on the main thread, except the
    three ``_on_*_key`` callbacks which run on the keyboard thread and only
    touch the queue / thread-safe worker controls."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.theme: Theme = DARK
        self._styled: list[tuple[tk.Misc, dict[str, str]]] = []
        self._custom: list[Any] = []

        # --- run state (main thread; ``running`` is also read by the key thread) ---
        self.ui_queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.worker: SendWorker | None = None
        self.run_id = 0
        self.running = False
        self.phase = "stopped"
        self.detail = ""
        self.sent = 0
        self.closing = False
        self.stop_note: tuple[str, bool] | None = None   # (text, is_error)
        self.settings = Settings()
        self._loading = False
        self._notice_job: str | None = None

        # --- tabs / history (main thread only) ---
        self.history = HistoryStore(HISTORY_FILE)
        self.current_tab = "Send"
        self.confirm_clear = False
        self._clear_job: str | None = None
        self.row_texts: dict[str, str] = {}               # tree row id -> message

        # --- hotkeys ---
        self.hotkeys: dict[str, str] = {}
        self._hotkey_handles: dict[str, Any] = {}
        self._last_hotkey: dict[str, float] = {}      # key thread only
        self.capturing = False
        self._capture_hook: Any = None
        self._capture_done: Callable[[str | None, str | None], None] | None = None
        self._capture_job: str | None = None
        self.dialog: HotkeyDialog | None = None

        self.own_pid = os.getpid()
        self.is_admin = Win32.is_admin()

        self._build_ui()
        self.root.update()                              # create the real window
        self.own_hwnd = Win32.toplevel_hwnd(self.root)

        initial, problem = self._read_startup_settings()
        self._apply_settings(initial)
        history_problem = self.history.load()
        problems = " ".join(p for p in (problem, history_problem) if p)
        if problems:
            self.notice(problems, error=True)

        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        self.root.after(POLL_MS, self.process_queue)

    # ------------------------------ styling ---------------------------------
    def style(self, widget: Any, **mapping: str) -> Any:
        """Register *widget* so each option follows a theme colour, and paint it."""
        self._styled.append((widget, mapping))
        self._paint(widget, mapping)
        return widget

    def register_custom(self, widget: Any) -> Any:
        """Register a widget that knows how to theme itself (apply_theme)."""
        self._custom.append(widget)
        widget.apply_theme(self.theme)
        return widget

    def _paint(self, widget: Any, mapping: dict[str, str]) -> None:
        widget.configure(**{opt: getattr(self.theme, key) for opt, key in mapping.items()})

    def _set_theme(self, theme: Theme) -> None:
        self.theme = theme
        alive_styled = []
        for widget, mapping in self._styled:
            try:
                self._paint(widget, mapping)
                alive_styled.append((widget, mapping))
            except tk.TclError:
                pass                                   # widget was destroyed
        self._styled = alive_styled
        alive_custom = []
        for widget in self._custom:
            try:
                widget.apply_theme(theme)
                alive_custom.append(widget)
            except tk.TclError:
                pass
        self._custom = alive_custom
        Win32.set_dark_titlebar(self.own_hwnd, theme is DARK)
        if self.dialog is not None:
            Win32.set_dark_titlebar(Win32.toplevel_hwnd(self.dialog), theme is DARK)
        self._style_tree(theme)
        self.refresh_buttons()
        self.render_status()

    # --------------------------------- UI -----------------------------------
    def _label(self, parent: tk.Misc, text: str, color: str = "text",
               size: int = 9, bold: bool = False) -> tk.Label:
        label = tk.Label(parent, text=text, font=(FONT, size, "bold" if bold else "normal"))
        return self.style(label, bg="bg", fg=color)

    def _frame(self, parent: tk.Misc) -> tk.Frame:
        return self.style(tk.Frame(parent), bg="bg")

    def _caption(self, parent: tk.Misc, text: str) -> tk.Label:
        return self._label(parent, text, "muted", 8, True)

    def _row(self, parent: tk.Misc, caption: str, pady: tuple[int, int] = (8, 0)
             ) -> tuple[tk.Frame, tk.Frame]:
        """A 'CAPTION ........ controls' row. Returns (row, right-hand frame)."""
        row = self._frame(parent)
        row.pack(fill="x", pady=pady)
        self._caption(row, caption).pack(side="left")
        right = self._frame(row)
        right.pack(side="right")
        return row, right

    def _toggle(self, parent: tk.Misc, text: str, var: tk.BooleanVar,
                command: Callable[[], None]) -> Toggle:
        return self.register_custom(Toggle(parent, text, var, command))

    def _build_ui(self) -> None:
        root = self.root
        root.title(APP_NAME)
        root.attributes("-topmost", True)               # always on top
        root.resizable(False, False)
        self.style(root, bg="bg")

        outer = self._frame(root)
        outer.pack(padx=22, pady=20)

        self._label(outer, APP_NAME, "text", 16, True).pack(anchor="w")
        self._label(outer, "Paste & send messages on repeat", "muted", 9
                    ).pack(anchor="w", pady=(0, 12))

        # Tab bar
        self.tabs = self.register_custom(TabBar(outer, ("Send", "History"), self.show_tab))
        self.tabs.pack(fill="x")
        rule = tk.Frame(outer, height=1)
        self.style(rule, bg="border")
        rule.pack(fill="x", pady=(0, 14))

        # Both pages share one grid cell so the window keeps a steady size
        pages = self._frame(outer)
        pages.pack(fill="x")
        pages.grid_columnconfigure(0, weight=1)
        self.send_page = self._frame(pages)
        self.history_page = self._frame(pages)
        for page in (self.send_page, self.history_page):
            page.grid(row=0, column=0, sticky="nsew")
        self._build_send_page(self.send_page)
        self._build_history_page(self.history_page)

        # Shown on every tab
        self.notice_label = tk.Label(outer, text="", font=(FONT, 8), anchor="w",
                                     justify="left", wraplength=300)
        self.style(self.notice_label, bg="bg", fg="muted")
        self.notice_label.pack(fill="x", pady=(4, 0))

        # Footer
        sep = tk.Frame(outer, height=1)
        self.style(sep, bg="border")
        sep.pack(fill="x", pady=(14, 8))
        self.hint = self._label(outer, "", "muted", 8)
        self.hint.pack()
        footer = self._frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        for text, command in (("Hotkeys", self.open_hotkeys), ("Save", self.save_dialog),
                              ("Load", self.load_dialog)):
            link = self.register_custom(LinkButton(footer, text, command))
            link.pack(side="left", padx=(0, 14))
        self.dark_var = tk.BooleanVar(value=True)
        self._toggle(footer, "Dark mode", self.dark_var,
                     self._on_dark_toggled).pack(side="right")

        self._update_delay_visibility()
        self.show_tab("Send")

    def _build_send_page(self, page: tk.Frame) -> None:
        # Messages (the original's single entry, now one message per line)
        self._caption(page, "MESSAGES  \u00b7  ONE PER LINE").pack(anchor="w", pady=(0, 4))
        self.msg_text = tk.Text(
            page, width=36, height=3, wrap="word", undo=True, relief="flat",
            font=(FONT, 11), highlightthickness=1, padx=10, pady=8,
            selectforeground="white")
        self.style(self.msg_text, bg="card", fg="text", insertbackground="text",
                   highlightbackground="border", highlightcolor="accent",
                   selectbackground="accent")
        self.msg_text.pack(fill="x")
        self.msg_text.bind("<<Modified>>", self._on_text_modified)

        # Delay (second box appears when "random range" is on)
        _, right = self._row(page, "DELAY", pady=(16, 0))
        self.delay_min = self.register_custom(Stepper(
            right, value=DEFAULT_DELAY_MIN, minimum=MIN_DELAY, maximum=MAX_DELAY,
            step=0.1, decimals=2, on_change=self._sync_settings))
        self.delay_to = self._label(right, "to", "muted")
        self.delay_max = self.register_custom(Stepper(
            right, value=DEFAULT_DELAY_MAX, minimum=MIN_DELAY, maximum=MAX_DELAY,
            step=0.1, decimals=2, on_change=self._sync_settings))
        unit = self._label(right, "sec", "muted")
        self.delay_min.grid(row=0, column=0)
        self.delay_to.grid(row=0, column=1, padx=6)
        self.delay_max.grid(row=0, column=2)
        unit.grid(row=0, column=3, padx=(6, 0))

        # Compact options: two toggles side by side, then the two limits
        self.random_order_var = tk.BooleanVar(value=False)
        self.random_delay_var = tk.BooleanVar(value=False)
        toggles = self._frame(page)
        toggles.pack(fill="x", pady=(12, 0))
        self._toggle(toggles, "Random order", self.random_order_var,
                     self._sync_settings).pack(side="left")
        self._toggle(toggles, "Random delay range", self.random_delay_var,
                     self._on_random_delay_toggled).pack(side="right")

        _, right = self._row(page, "STOP AFTER", pady=(12, 0))
        self.limit_stepper = self.register_custom(Stepper(
            right, value=0, minimum=0, maximum=MAX_SEND_LIMIT, step=1, decimals=0,
            on_change=self._sync_settings))
        self.limit_stepper.pack(side="left")
        self._label(right, "msgs (0 = off)", "muted").pack(side="left", padx=(6, 0))
        _, right = self._row(page, "TIMER", pady=(8, 0))
        self.timer_stepper = self.register_custom(Stepper(
            right, value=0, minimum=0, maximum=MAX_TIMER_MINUTES, step=1, decimals=1,
            on_change=self._sync_settings))
        self.timer_stepper.pack(side="left")
        self._label(right, "min (0 = off)", "muted").pack(side="left", padx=(6, 0))

        # Big Start/Stop button, with Pause/Resume tucked right under it
        self.button = self.register_custom(RoundButton(page, self.toggle, width=300))
        self.button.pack(pady=(20, 0))
        self.pause_button = self.register_custom(
            LinkButton(page, "Pause", self.toggle_pause))
        self.pause_button.pack(pady=(8, 0))

        # Status row
        status_row = self._frame(page)
        status_row.pack(fill="x", pady=(10, 0))
        self.dot = tk.Canvas(status_row, width=10, height=10, highlightthickness=0)
        self.style(self.dot, bg="bg")
        self.dot.pack(side="left", pady=(2, 0))
        self.dot_item = self.dot.create_oval(1, 1, 9, 9, fill=DARK.grey_dot, outline="")
        self.status = tk.Label(status_row, text="", font=(FONT, 9), anchor="w",
                               justify="left", wraplength=200)
        self.style(self.status, bg="bg", fg="text")
        self.status.pack(side="left", padx=(8, 0))
        self.sent_label = self._label(status_row, "", "muted")
        self.sent_label.pack(side="right")

    def _build_history_page(self, page: tk.Frame) -> None:
        self.summary = self._label(page, "", "muted", 9)
        self.summary.configure(anchor="w")
        self.summary.pack(fill="x", pady=(0, 8))

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.layout("Auto.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        self._style_tree(self.theme)

        frame = tk.Frame(page, padx=1, pady=1)
        self.style(frame, bg="border")
        frame.pack(fill="x")
        self.tree = ttk.Treeview(frame, style="Auto.Treeview", height=10,
                                 columns=("msg", "uses", "last"),
                                 show="headings", selectmode="browse")
        self.tree.heading("msg", text="MESSAGE", anchor="w")
        self.tree.heading("uses", text="USES", anchor="center")
        self.tree.heading("last", text="LAST USED", anchor="w")
        self.tree.column("msg", width=150, anchor="w", stretch=True)
        self.tree.column("uses", width=50, anchor="center", stretch=False)
        self.tree.column("last", width=100, anchor="w", stretch=False)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview,
                               style="Auto.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="x", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.reuse_selected)

        bottom = self._frame(page)
        bottom.pack(fill="x", pady=(8, 0))
        self._label(bottom, "Double-click a row to add it to your messages",
                    "muted", 8).pack(side="left")
        self.clear_btn = self.register_custom(
            LinkButton(bottom, "Clear history", self.on_clear_click))
        self.clear_btn.pack(side="right")

    def _style_tree(self, t: Theme) -> None:
        """(Re)colour the history table; ttk widgets don't follow tk colours."""
        style = ttk.Style(self.root)
        style.configure("Auto.Treeview", background=t.card, fieldbackground=t.card,
                        foreground=t.text, borderwidth=0, rowheight=26, font=(FONT, 9))
        style.map("Auto.Treeview",
                  background=[("selected", t.accent)],
                  foreground=[("selected", "white")])
        style.configure("Auto.Treeview.Heading", background=t.border,
                        foreground=t.muted, relief="flat", borderwidth=0,
                        font=(FONT, 8, "bold"))
        style.map("Auto.Treeview.Heading", background=[("active", t.border)])
        style.configure("Auto.Vertical.TScrollbar", background=t.border,
                        troughcolor=t.card, bordercolor=t.card, arrowcolor=t.muted,
                        lightcolor=t.border, darkcolor=t.border, relief="flat")
        style.map("Auto.Vertical.TScrollbar", background=[("active", t.stop_hover)])

    # ------------------------- settings <-> widgets --------------------------
    def _read_startup_settings(self) -> tuple[Settings, str | None]:
        if SETTINGS_FILE.exists():
            try:
                return load_settings(SETTINGS_FILE), None
            except SettingsError as exc:
                return Settings(), f"Ignored saved settings. {exc}"
        return Settings(), None

    def _apply_settings(self, s: Settings) -> None:
        """Push a Settings object into the widgets, theme and hotkeys."""
        self._loading = True
        try:
            self.msg_text.delete("1.0", "end")
            self.msg_text.insert("1.0", "\n".join(s.messages))
            self.msg_text.edit_reset()        # Ctrl+Z must not undo the load itself
            self.msg_text.edit_modified(False)
            self.random_order_var.set(s.random_order)
            self.random_delay_var.set(s.random_delay)
            self.delay_min.set(s.delay_min)
            self.delay_max.set(s.delay_max)
            self.limit_stepper.set(s.send_limit)
            self.timer_stepper.set(s.timer_minutes)
            self.dark_var.set(s.dark_mode)
        finally:
            self._loading = False
        self._enforce_blacklist()                      # also covers loaded files
        self.msg_text.edit_modified(False)
        self._update_delay_visibility()
        self._set_theme(DARK if s.dark_mode else LIGHT)

        error = self.apply_hotkeys(s.hotkeys)
        if error and not self.hotkeys:                 # nothing registered: use defaults
            fallback = self.apply_hotkeys(DEFAULT_HOTKEYS)
            if fallback:
                error = f"{error} Hotkeys are unavailable ({fallback}); close the window to quit."
            else:
                error = f"{error} Default hotkeys are being used."
        if error:
            self.notice(f"Hotkey problem: {error}", error=True)
        self._refresh_hint()
        self._sync_settings()

    def _on_text_modified(self, _event: tk.Event) -> None:
        if self.msg_text.edit_modified():
            self.msg_text.edit_modified(False)
            self._enforce_blacklist()
            self.msg_text.edit_modified(False)
            self._sync_settings()

    def _enforce_blacklist(self) -> None:
        """Erase every blacklisted word from the message box and tell the user.

        Uses Tk's own text search so positions always match the widget (and the
        cursor stays where it was), even with emoji in the text.
        """
        words = sorted({w.strip() for w in BLACKLISTED_WORDS
                        if isinstance(w, str) and w.strip()},
                       key=len, reverse=True)          # longest first
        if not words:
            return
        removed: list[str] = []
        changed = True
        while changed:                                  # removing one word can join the
            changed = False                             # text around it into another one
            for word in words:
                while True:
                    count = tk.IntVar(self.root)
                    index = self.msg_text.search(word, "1.0", stopindex="end",
                                                 nocase=True, count=count)
                    if not index:
                        break
                    length = count.get() or len(word)
                    end = f"{index}+{length}c"
                    found = self.msg_text.get(index, end)
                    self.msg_text.delete(index, end)
                    changed = True
                    if found.lower() not in (r.lower() for r in removed):
                        removed.append(found)
        if removed:
            if len(removed) == 1:
                text = f'The word "{removed[0]}" is blacklisted.'
            else:
                text = "These words are blacklisted: " + ", ".join(
                    f'"{w}"' for w in removed)
            self.notice(text, error=True)

    def _sync_settings(self) -> None:
        """Rebuild the immutable Settings snapshot from the widgets."""
        if self._loading:
            return
        raw = self.msg_text.get("1.0", "end-1c")
        messages = tuple(line.strip() for line in raw.splitlines() if line.strip())
        self.settings = Settings(
            messages=messages,
            random_order=self.random_order_var.get(),
            random_delay=self.random_delay_var.get(),
            delay_min=self.delay_min.value,
            delay_max=self.delay_max.value,
            send_limit=int(self.limit_stepper.value),
            timer_minutes=float(self.timer_stepper.value),
            hotkeys=dict(self.hotkeys),
            dark_mode=self.dark_var.get(),
        )
        if self.running and not messages:
            self.stop("Message list is empty")
        self.refresh_buttons()
        self.render_status()

    def _on_random_delay_toggled(self) -> None:
        self._update_delay_visibility()
        self._sync_settings()

    def _update_delay_visibility(self) -> None:
        if self.random_delay_var.get():
            self.delay_to.grid()
            self.delay_max.grid()
        else:
            self.delay_to.grid_remove()
            self.delay_max.grid_remove()

    def _on_dark_toggled(self) -> None:
        self._set_theme(DARK if self.dark_var.get() else LIGHT)
        self._sync_settings()

    # ------------------------------ status UI -------------------------------
    def refresh_buttons(self) -> None:
        """Start is disabled with no messages; 'Stop' while running; Pause/Resume."""
        if self.running:
            self.button.set_state("Stop", "secondary")
        elif self.settings.messages:
            self.button.set_state("Start", "primary")
        else:
            self.button.set_state("Start", "disabled")
        can_pause = self.running and self.phase in PAUSABLE_PHASES
        self.pause_button.configure(text="Resume" if self.phase == "paused" else "Pause")
        self.pause_button.set_enabled(can_pause)

    def set_status(self, text: str, color: str, fg: str | None = None) -> None:
        self.dot.itemconfig(self.dot_item, fill=color)
        self.status.configure(text=text, fg=fg or self.theme.text)

    def render_status(self) -> None:
        t = self.theme
        phase, detail = self.phase, self.detail
        if phase == "stopped":
            if self.stop_note:
                text, is_error = self.stop_note
                self.set_status(text, t.error if is_error else t.grey_dot,
                                t.error if is_error else t.text)
            else:
                self.set_status("Stopped", t.grey_dot)
        elif phase == "countdown":
            self.set_status(f"Starting in {detail}... click your chat box", t.amber)
        elif phase == "paused":
            self.set_status("Paused", t.amber)
        elif phase == "waiting":
            text = ("Menu focused - click your chat box" if detail == "own"
                    else "No active window - waiting")
            self.set_status(text, t.amber)
        elif phase == "blocked":
            self.set_status("Target app runs as admin - restart Auto Chat as admin",
                            t.error, t.error)
        else:
            self.set_status("Running", t.green)
        limit = self.settings.send_limit
        self.sent_label.configure(
            text=f"Sent: {self.sent}" + (f" / {limit}" if limit else ""))

    def notice(self, text: str, error: bool = False) -> None:
        """Show a transient message under the status row."""
        if self._notice_job is not None:
            self.root.after_cancel(self._notice_job)
        self.notice_label.configure(
            text=text, fg=self.theme.error if error else self.theme.muted)
        self._notice_job = self.root.after(
            NOTICE_MS * (2 if error else 1), lambda: self.notice_label.configure(text=""))

    def _refresh_hint(self) -> None:
        if not self.hotkeys:
            self.hint.configure(text="Hotkeys unavailable")
            return
        self.hint.configure(text="   \u00b7   ".join(
            f"{self.hotkeys[n].upper()} {HOTKEY_HINT_WORDS[n]}" for n in HOTKEY_NAMES))

    # --------------------------- start / stop / pause ------------------------
    def toggle(self) -> None:
        """Used by the button and the hotkey so they always agree."""
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if not self.settings.messages:
            self.notice("Add at least one message first.", error=True)
            return
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=WORKER_JOIN_TIMEOUT)   # let it restore clipboard
        self.run_id += 1
        self.sent = 0
        self.stop_note = None
        self.worker = SendWorker(self.run_id, lambda: self.settings,
                                 self.ui_queue.put, self.own_pid, self.is_admin)
        self.running = True
        self.phase, self.detail = ("countdown", str(START_DELAY)) if START_DELAY > 0 \
            else ("running", "")
        self.worker.start()
        self.refresh_buttons()
        self.render_status()

    def stop(self, note: str | None = None, is_error: bool = False) -> None:
        if self.worker is not None:
            self.worker.request_stop()                      # wakes the worker instantly
        self.running = False
        self.phase = "stopped"
        self.stop_note = (note, is_error) if note else None
        self.refresh_buttons()
        self.render_status()

    def toggle_pause(self) -> None:
        worker = self.worker
        if not (self.running and worker and self.phase in PAUSABLE_PHASES):
            return
        if worker.paused:
            worker.resume()
        else:
            worker.pause()

    # ------------------------------ main-thread queue ------------------------
    def process_queue(self) -> None:
        """Drain the queue. One bad message must never kill the polling loop -
        if it did, hotkeys and status updates would silently stop working."""
        try:
            while not self.closing:
                try:
                    msg = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.handle(msg)
                except Exception:
                    traceback.print_exc()
        finally:
            if not self.closing:
                self.root.after(POLL_MS, self.process_queue)

    def handle(self, msg: tuple[Any, ...]) -> None:
        kind = msg[0]
        if kind == "hotkey":
            self._handle_hotkey(*msg[1:])
            return
        if kind == "captured":
            self._finish_capture(msg[1])
            return
        if kind == "sent":
            # A message that really went out always counts for the history, even
            # if its run was stopped or replaced a moment ago.
            self._record_history(msg[3], msg[4])
        elif kind == "finished":
            self.history.flush(force=True)                  # run over: save now
        if msg[1] != self.run_id:                           # stale: an older run
            return
        if kind == "finished":
            if self.running:                                # ended by itself
                self.running, self.phase = False, "stopped"
                self.stop_note = (msg[2], bool(msg[3]))
        elif not self.running:
            return
        elif kind == "phase":
            self.phase, self.detail = msg[2], msg[3]
        elif kind == "sent":
            self.sent = msg[2]
        self.refresh_buttons()
        self.render_status()

    def _handle_hotkey(self, name: str, was_active: bool, stamp: float) -> None:
        if name != "quit" and time.monotonic() - stamp > STALE_HOTKEY_SECONDS:
            return                                          # stale (UI was busy)
        if name == "quit":
            self.shutdown()
        elif name == "toggle":
            # Pressed while running -> intent was STOP; if the run already ended
            # on its own, don't turn that press into an accidental START.
            if was_active:
                if self.running:
                    self.stop()
            elif not self.running:
                self.start()
        elif name == "pause":
            self.toggle_pause()

    # ------------------------------ hotkeys ---------------------------------
    def apply_hotkeys(self, combos: dict[str, str]) -> str | None:
        """Validate and register hotkeys. Returns an error message or None."""
        cleaned = {n: str(combos.get(n, "")).strip().lower() for n in HOTKEY_NAMES}
        error = validate_hotkeys(cleaned)
        if error:
            return error
        previous = dict(self.hotkeys)
        self._unbind_hotkeys()
        try:
            self._bind_hotkeys(cleaned)
        except Exception as exc:                            # e.g. hook couldn't install
            self._unbind_hotkeys()
            if previous:
                try:
                    self._bind_hotkeys(previous)
                except Exception:
                    self._unbind_hotkeys()
            return f"Could not register hotkeys: {exc}"
        self._refresh_hint()
        self._sync_settings()
        return None

    def _bind_hotkeys(self, combos: dict[str, str]) -> None:
        callbacks = {"toggle": self._on_toggle_key, "pause": self._on_pause_key,
                     "quit": self._on_quit_key}
        for name in HOTKEY_NAMES:
            self._hotkey_handles[name] = keyboard.add_hotkey(combos[name], callbacks[name])
        self.hotkeys = dict(combos)

    def _unbind_hotkeys(self) -> None:
        for handle in self._hotkey_handles.values():
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass
        self._hotkey_handles.clear()
        self.hotkeys = {}

    def _debounced(self, name: str) -> bool:
        """True if this hotkey should be acted on (keyboard thread only)."""
        if self.capturing:                                  # recording a new hotkey
            return False
        now = time.monotonic()
        if now - self._last_hotkey.get(name, 0.0) < HOTKEY_DEBOUNCE:
            return False
        self._last_hotkey[name] = now
        return True

    # The three callbacks below run on the keyboard thread: no tkinter here!
    def _on_toggle_key(self) -> None:
        if not self._debounced("toggle"):
            return
        was_active, worker = self.running, self.worker
        if was_active and worker is not None:
            worker.request_stop()                           # stop pasting immediately
        self.ui_queue.put(("hotkey", "toggle", was_active, time.monotonic()))

    def _on_pause_key(self) -> None:
        if self._debounced("pause"):
            self.ui_queue.put(("hotkey", "pause", self.running, time.monotonic()))

    def _on_quit_key(self) -> None:
        if self.capturing:
            return
        worker = self.worker
        if worker is not None:
            worker.request_stop()                           # failsafe: stop right now
        self.ui_queue.put(("hotkey", "quit", self.running, time.monotonic()))

    # --- recording a hotkey for the dialog ---
    def begin_capture(self, on_done: Callable[[str | None, str | None], None]) -> None:
        """Record the next key combination. Calls on_done(combo, error)."""
        self.cancel_capture()
        self.capturing = True
        self._capture_done = on_done
        pressed: set[str] = set()

        def on_event(event: Any) -> None:                   # keyboard thread
            if event.event_type == keyboard.KEY_DOWN:
                if event.name:
                    pressed.add(event.name)
            elif pressed:                                   # first release ends it
                names = list(pressed)
                pressed.clear()
                self.ui_queue.put(("captured", names))

        try:
            self._capture_hook = keyboard.hook(on_event)
        except Exception as exc:
            self.capturing = False
            self._capture_done = None
            on_done(None, f"Could not listen for keys: {exc}")
            return
        self._capture_job = self.root.after(CAPTURE_TIMEOUT_MS, self._capture_timeout)

    def _capture_timeout(self) -> None:
        done = self._capture_done
        self.cancel_capture()
        if done:
            done(None, "Timed out - nothing recorded.")

    def cancel_capture(self) -> None:
        if self._capture_hook is not None:
            try:
                keyboard.unhook(self._capture_hook)
            except Exception:
                pass
        if self._capture_job is not None:
            self.root.after_cancel(self._capture_job)
        self._capture_hook = self._capture_job = self._capture_done = None
        self.capturing = False

    def _finish_capture(self, names: list[str]) -> None:
        if not self.capturing:                              # late duplicate event
            return
        done = self._capture_done
        self.cancel_capture()
        if not done:
            return
        try:
            combo = keyboard.get_hotkey_name(names)
        except Exception:
            done(None, "Could not read that key combination.")
            return
        if combo == "esc":                                  # Esc cancels recording
            done(None, None)
        else:
            done(combo, None)

    # ------------------------- dialogs / files -------------------------------
    def open_hotkeys(self) -> None:
        if self.dialog is not None:
            self.dialog.lift()
            return
        self.dialog = HotkeyDialog(self)

    def save_dialog(self) -> None:
        self._sync_settings()
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save settings", initialdir=str(SETTINGS_FILE.parent),
            initialfile=SETTINGS_FILE.name, defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            save_settings(Path(path), self.settings)
            self.notice(f"Saved {Path(path).name}")
        except SettingsError as exc:
            self.notice(str(exc), error=True)

    def load_dialog(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Load settings", initialdir=str(SETTINGS_FILE.parent),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            settings = load_settings(Path(path))
        except SettingsError as exc:
            self.notice(str(exc), error=True)
            return
        self.notice(f"Loaded {Path(path).name}")
        self._apply_settings(settings)                  # may replace it with a warning

    # ------------------------------ tabs / history ---------------------------
    def show_tab(self, name: str) -> None:
        self.current_tab = name
        page = self.send_page if name == "Send" else self.history_page
        page.tkraise()
        self.tabs.select(name)
        if name == "History":
            self.refresh_history()
        else:
            self._reset_clear_button()
            self.msg_text.focus_set()

    def _record_history(self, text: str, stamp: float) -> None:
        self.history.record(text, stamp)
        if self.current_tab == "History":
            self.refresh_history()
        error = self.history.flush()                        # throttled
        if error:                                           # don't fail silently
            self.notice(error, error=True)

    @staticmethod
    def _format_time(stamp: float) -> str:
        if stamp <= 0:
            return "-"
        try:
            return time.strftime("%b %d, %H:%M", time.localtime(stamp))
        except (OverflowError, OSError, ValueError):
            return "-"

    def refresh_history(self) -> None:
        """Rebuild the table, keeping the selection and scroll position."""
        tree = self.tree
        top = tree.yview()[0]
        sel = tree.selection()
        selected = self.row_texts.get(sel[0]) if sel else None
        tree.delete(*tree.get_children())
        self.row_texts.clear()
        items = self.history.sorted_items()
        for i, (text, info) in enumerate(items):
            rid = f"r{i}"
            tree.insert("", "end", iid=rid,
                        values=(text, info["count"], self._format_time(info["last"])))
            self.row_texts[rid] = text
            if text == selected:
                tree.selection_set(rid)
        tree.yview_moveto(top)
        if items:
            self.summary.configure(
                text=f"{len(items)} unique  \u00b7  {self.history.total_sends()} total sends")
        else:
            self.summary.configure(text="No history yet - send something first")

    def reuse_selected(self, event: Any = None) -> None:
        """Double-click: add that message to the message list (no duplicates).

        Uses the row under the pointer, so double-clicking a column header or
        empty space doesn't re-add whatever row happened to be selected.
        """
        if event is not None:
            rid = self.tree.identify_row(event.y)
        else:
            sel = self.tree.selection()
            rid = sel[0] if sel else ""
        text = self.row_texts.get(rid) if rid else None
        if text is None:
            return
        if text in self.settings.messages:
            note = "Already in your messages"
        else:
            current = self.msg_text.get("1.0", "end-1c")
            if current.strip() and not current.endswith("\n"):
                self.msg_text.insert("end", "\n")
            self.msg_text.insert("end", text)
            self.msg_text.see("end")
            note = "Added to your messages"
        self.show_tab("Send")
        self.notice(note)

    def on_clear_click(self) -> None:
        if not self.confirm_clear:
            self.confirm_clear = True
            self.clear_btn.configure(text="Click again to confirm")
            self._clear_job = self.root.after(3000, self._reset_clear_button)
            return
        self._reset_clear_button()
        self.history.clear()
        error = self.history.flush(force=True)
        self.refresh_history()
        self.notice(error or "History cleared", error=bool(error))

    def _reset_clear_button(self) -> None:
        if self._clear_job is not None:
            self.root.after_cancel(self._clear_job)
            self._clear_job = None
        self.confirm_clear = False
        self.clear_btn.configure(text="Clear history")

    def _save_pending_history(self) -> None:
        """Shutdown: count sends still waiting in the queue, then save."""
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                if msg[0] == "sent":
                    self.history.record(msg[3], msg[4])
        except queue.Empty:
            pass
        self.history.flush(force=True)

    # ------------------------------ shutdown ---------------------------------
    def shutdown(self) -> None:
        """Stop the loop, restore the clipboard, remove hotkeys, close the window."""
        if self.closing:
            return
        self.closing = True
        self.running = False
        self.cancel_capture()
        if self.worker is not None:
            self.worker.request_stop()
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=WORKER_JOIN_TIMEOUT)   # worker restores clipboard
        self._save_pending_history()
        self.root.destroy()


def main() -> None:
    if sys.platform != "win32":
        print("This script is written for Windows.")
        return
    root = tk.Tk()
    AutoChatApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()