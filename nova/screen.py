"""Screen capture of the active window / the area around the cursor, and clicks.

All coordinates are physical pixels (the process is per-monitor DPI aware,
see __main__.py). Screenshots live only in memory: nothing here writes to disk.
"""

import base64
import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass

import cv2
import numpy as np

from . import guard

user32 = ctypes.windll.user32


@dataclass
class Shot:
    image: np.ndarray  # BGR
    left: int  # screen position of image pixel (0, 0)
    top: int

    @property
    def width(self) -> int:
        return self.image.shape[1]

    @property
    def height(self) -> int:
        return self.image.shape[0]

    def to_jpeg_b64(self, max_side: int = 1280) -> tuple[str, float]:
        """Downscaled JPEG for the AI, plus the factor to map its pixels back."""
        scale = min(1.0, max_side / max(self.width, self.height))
        img = self.image if scale == 1.0 else cv2.resize(
            self.image, (round(self.width * scale), round(self.height * scale)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return base64.b64encode(buf.tobytes()).decode(), scale


def _grab(left: int, top: int, width: int, height: int) -> np.ndarray:
    import mss

    with getattr(mss, "MSS", mss.mss)() as sct:
        raw = np.asarray(sct.grab({"left": left, "top": top, "width": width, "height": height}))
    return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)


def _virtual_screen() -> tuple[int, int, int, int]:
    # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
    x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    return x, y, x + user32.GetSystemMetrics(78), y + user32.GetSystemMetrics(79)


def _clip(l, t, r, b):
    vl, vt, vr, vb = _virtual_screen()
    return max(l, vl), max(t, vt), min(r, vr), min(b, vb)


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Visible bounds of a window (without the invisible resize border)."""
    if not hwnd or not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    rect = wt.RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    l, t, r, b = _clip(rect.left, rect.top, rect.right, rect.bottom)
    return (l, t, r, b) if r - l > 50 and b - t > 50 else None


def cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _refuse_private(*hwnds: int) -> None:
    """Password managers and Windows credential prompts are never captured: not for
    the AI, not for visual memory."""
    if any(h and guard.is_private(h) for h in hwnds):
        from .i18n import t
        from .skills import SkillError

        raise SkillError(t("private_window"))


def capture_window(hwnd: int) -> Shot:
    """The active window; falls back to the monitor under the cursor."""
    _refuse_private(hwnd, user32.GetForegroundWindow())
    rect = window_rect(hwnd)
    if rect is None:
        return capture_monitor()
    l, t, r, b = rect
    return Shot(_grab(l, t, r - l, b - t), l, t)


def capture_monitor() -> Shot:
    import mss

    x, y = cursor_pos()
    _refuse_private(user32.GetForegroundWindow(), user32.WindowFromPoint(wt.POINT(x, y)))
    with getattr(mss, "MSS", mss.mss)() as sct:
        mon = next((m for m in sct.monitors[1:] if m["left"] <= x < m["left"] + m["width"]
                    and m["top"] <= y < m["top"] + m["height"]), sct.monitors[1])
    return Shot(_grab(mon["left"], mon["top"], mon["width"], mon["height"]), mon["left"], mon["top"])


def capture_around_cursor(width: int = 1000, height: int = 700) -> Shot:
    """"Посмотри сюда": the area around the mouse pointer."""
    x, y = cursor_pos()
    _refuse_private(user32.GetForegroundWindow(), user32.WindowFromPoint(wt.POINT(x, y)))
    l, t, r, b = _clip(x - width // 2, y - height // 2, x + width // 2, y + height // 2)
    return Shot(_grab(l, t, r - l, b - t), l, t)


def app_name(hwnd: int) -> str:
    """'dota2.exe' for a window — visual memory is kept per app."""
    import psutil

    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name().lower()
    except (psutil.Error, ValueError):
        return "unknown"


def bring_to_front(hwnd: int) -> bool:
    """Really make `hwnd` the active window; returns False if Windows refused.

    Windows only lets the foreground process hand the focus over, and Nova runs in
    the background. A synthetic Alt tap counts as user input and lifts that lock.
    Callers must not type unless this returned True — the keys would land in
    whatever window is active instead (a game, someone else's chat).
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    for _ in range(3):
        if user32.GetForegroundWindow() == hwnd:
            return True
        if not guard.games.blocking:  # no synthetic keys at all next to an anti-cheat
            user32.keybd_event(0x12, 0, 0, 0)  # Alt down
            user32.keybd_event(0x12, 0, 2, 0)  # Alt up
        user32.SetForegroundWindow(hwnd)
        user32.SwitchToThisWindow(hwnd, True)
        time.sleep(0.15)
    return user32.GetForegroundWindow() == hwnd


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def is_fullscreen(hwnd: int) -> bool:
    """A game, video or presentation in fullscreen — Nova then stays out of sight.

    Asks Windows the same question it uses to silence notifications
    (SHQueryUserNotificationState); a maximized app like Telegram doesn't count.
    """
    state = ctypes.c_int(0)
    try:
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            # 2 = fullscreen app, 3 = Direct3D exclusive fullscreen, 4 = presentation mode
            return state.value in (2, 3, 4)
    except (AttributeError, OSError):
        pass
    return _covers_monitor(hwnd)


def _covers_monitor(hwnd: int) -> bool:
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):  # the desktop itself
        return False
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
    MONITOR_DEFAULTTONEAREST = 2
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(info))
    m = info.rcMonitor
    return rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom


def top_windows() -> list[tuple[int, str, str]]:
    """Visible titled windows as (hwnd, title, 'process.exe'), front to back."""
    import psutil

    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if user32.IsWindowVisible(hwnd) and length > 0 and is_app_window(hwnd):
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                out.append((hwnd, buf.value, psutil.Process(pid.value).name().lower()))
            except psutil.Error:
                pass
        return True

    user32.EnumWindows(visit, 0)
    return out


def default_browser() -> tuple[str, str]:
    """(path to the user's default browser, 'opera.exe') from the https handler; ('', '') if unknown."""
    import re
    import winreg

    try:
        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as k:
            command = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return "", ""
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', command)
    path = (m.group(1) or m.group(2)) if m else ""
    return path, path.replace("/", "\\").rsplit("\\", 1)[-1].lower()


_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
_dwm = ctypes.windll.dwmapi


def is_app_window(hwnd: int) -> bool:
    """A window the user means by "the Telegram window": not the desktop or taskbar,
    not a hidden (cloaked) suspended app, and not Nova's own panel or settings."""
    import os

    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in _SHELL_CLASSES:
        return False
    cloaked = ctypes.c_int(0)
    _dwm.DwmGetWindowAttribute(wt.HWND(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))  # DWMWA_CLOAKED
    if cloaked.value:
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value != os.getpid()


def windows_with_paths() -> list[tuple[int, str, str]]:
    """Visible titled windows as (hwnd, title, full exe path in lower case), front to back."""
    import psutil

    out, paths = [], {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if user32.IsWindowVisible(hwnd) and length > 0 and is_app_window(hwnd):
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in paths:
                try:
                    paths[pid.value] = (psutil.Process(pid.value).exe() or "").lower()
                except psutil.Error:
                    paths[pid.value] = ""
            if paths[pid.value]:
                out.append((hwnd, buf.value, paths[pid.value]))
        return True

    user32.EnumWindows(visit, 0)
    return out


def main_window_of(exe: str) -> int:
    """The visible, titled top-level window of a process ('telegram.exe'), or 0."""
    import psutil

    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0 and is_app_window(hwnd):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                if psutil.Process(pid.value).name().lower() == exe:
                    found.append(hwnd)
                    return False
            except psutil.Error:
                pass
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else 0


def still_there(hwnd: int, shot: "Shot") -> bool:
    """The window a screenshot was taken of is still in front, at the same place."""
    if not hwnd:
        return True  # the desktop itself
    if not user32.IsWindow(hwnd) or user32.GetForegroundWindow() != hwnd:
        return False
    rect = window_rect(hwnd)
    return rect is not None and (rect[0], rect[1]) == (shot.left, shot.top)


def click_at(x: int, y: int) -> None:
    import uiautomation as auto

    auto.Click(int(x), int(y), waitTime=0.05)
    time.sleep(0.05)
