"""The push-to-talk shortcut and Esc, through Windows' own RegisterHotKey.

A keyboard hook (the `keyboard` library) sees every key the user presses, needs
Python for each one, and has to hold Ctrl/Alt back to decide whether they start
the shortcut — a CS2 crouch on Ctrl arrives late, and Windows silently drops a
hook that answers slowly. RegisterHotKey asks Windows to tell Nova only about
its own combination: nothing else is delayed, nothing can be dropped, and it
works over administrator windows too.
"""

import ctypes
import ctypes.wintypes as wt
import logging
import queue
import threading

log = logging.getLogger(__name__)
user32 = ctypes.windll.user32
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetAsyncKeyState.restype = ctypes.c_short

_MODS = {"ctrl": (0x2, 0x11), "control": (0x2, 0x11), "left ctrl": (0x2, 0xA2), "right ctrl": (0x2, 0xA3),
         "alt": (0x1, 0x12), "left alt": (0x1, 0xA4), "right alt": (0x1, 0xA5), "alt gr": (0x3, 0xA5),
         "shift": (0x4, 0x10), "left shift": (0x4, 0xA0), "right shift": (0x4, 0xA1),
         "win": (0x8, 0x5B), "windows": (0x8, 0x5B), "left windows": (0x8, 0x5B), "right windows": (0x8, 0x5C)}
_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
         "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "page up": 0x21, "page down": 0x22,
         "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pause": 0x13, "scroll lock": 0x91,
         "caps lock": 0x14, "num lock": 0x90, "menu": 0x5D, "apps": 0x5D, "print screen": 0x2C,
         "play/pause media": 0xB3, **{f"f{i}": 0x6F + i for i in range(1, 25)}}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_APP = 0x0312, 0x8000
_SHORTCUT, _ESC = 1, 2


def parse(combo: str) -> tuple[int, int, list[int]] | None:
    """"ctrl+alt+space" -> (modifier flags, key code, every key code to check for "held")."""
    mods, held, key = 0, [], None
    for part in (p.strip().lower() for p in str(combo).split("+") if p.strip()):
        if part in _MODS:
            flag, vk = _MODS[part]
            mods |= flag
            held.append(vk)
        elif key is None:
            if part in _KEYS:
                key = _KEYS[part]
            elif len(part) == 1 and (part.isascii() and part.isalnum()):
                key = ord(part.upper())  # A-Z and 0-9 have their own key codes, whatever the layout
            elif len(part) == 1:
                scan = user32.VkKeyScanW(ord(part))
                if scan == -1:
                    return None
                key = scan & 0xFF
            else:
                return None
        else:
            return None  # two main keys: Windows hotkeys have one
    if key is None:
        return None
    return mods, key, held + [key]


class Hotkeys:
    """One thread owns the registrations and receives WM_HOTKEY."""

    def __init__(self, on_shortcut, on_esc):
        self._on = {_SHORTCUT: on_shortcut, _ESC: on_esc}
        self._requests: queue.Queue = queue.Queue()
        self._thread_id = 0
        self._ready = threading.Event()
        self.combo = ""
        self._held: list[int] = []
        threading.Thread(target=self._run, name="hotkeys", daemon=True).start()
        self._ready.wait(5)

    def _run(self) -> None:
        self._thread_id = threading.get_native_id()
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create this thread's message queue
        self._ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in self._on:
                try:
                    self._on[msg.wParam]()
                except Exception:  # noqa: BLE001 - a hotkey handler must never end the loop
                    log.exception("Hotkey handler failed")
            elif msg.message == WM_APP:
                while not self._requests.empty():
                    job, reply = self._requests.get()
                    try:
                        reply.put(job())
                    except Exception:  # noqa: BLE001 - one bad request must not end the shortcut
                        log.exception("Hotkey request failed")
                        reply.put(False)

    def _call(self, job, wait: bool = True):
        """Run `job` on the hotkey thread (Windows wants registrations made there)."""
        reply: queue.Queue = queue.Queue()
        self._requests.put((job, reply))
        user32.PostThreadMessageW(self._thread_id, WM_APP, 0, 0)
        if not wait:
            return None
        try:
            return reply.get(timeout=3)
        except queue.Empty:
            return False

    def set_shortcut(self, combo: str) -> bool:
        """Register the push-to-talk combination; the old one stays if this one is taken."""
        parsed = parse(combo)
        if parsed is None:
            return False
        mods, key, held = parsed

        def job():
            user32.UnregisterHotKey(None, _SHORTCUT)
            if user32.RegisterHotKey(None, _SHORTCUT, mods | MOD_NOREPEAT, key):
                return True
            old = parse(self.combo)  # taken by another program: put ours back
            if old:
                user32.RegisterHotKey(None, _SHORTCUT, old[0] | MOD_NOREPEAT, old[1])
            return False

        if self._call(job):
            self.combo, self._held = combo, held
            return True
        log.warning("Shortcut %r is taken by another program", combo)
        return False

    def esc(self, enabled: bool, wait: bool = True) -> None:
        """Esc cancels only while Nova is on screen; the rest of the time it's the user's."""
        self._call(lambda: user32.RegisterHotKey(None, _ESC, MOD_NOREPEAT, 0x1B) if enabled
                   else user32.UnregisterHotKey(None, _ESC), wait=wait)

    def shortcut_held(self) -> bool:
        """Are all keys of the shortcut still down? (hold-to-talk)"""
        return bool(self._held) and all(user32.GetAsyncKeyState(vk) & 0x8000 for vk in self._held)
