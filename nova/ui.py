"""Overlay pill + settings window (pywebview) and a tray icon (pystray)."""

import ctypes
import json
import logging
import os
import threading
import time
from pathlib import Path

import keyboard
import pystray
import webview
from PIL import Image, ImageDraw

from . import brain, config
from . import router as router_mod
from .i18n import DEFAULT_NAMES, LANGUAGES, t

log = logging.getLogger(__name__)
HERE = Path(__file__).parent / "web"

# Pointer-sized arguments: without these, HWND_TOPMOST (-1) is passed as a 32-bit int
# on 64-bit Windows and SetWindowPos silently does nothing.
_user32 = ctypes.windll.user32
_user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
ctypes.windll.gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
_user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]


ICON = HERE / "nova.ico"  # drawn by tools/make_icon.py


def _tray_image() -> Image.Image:
    if HERE.joinpath("nova.png").exists():
        return Image.open(HERE / "nova.png").convert("RGBA").resize((64, 64), Image.LANCZOS)
    return _drawn_tray_image()


def _drawn_tray_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for r in range(30, 0, -1):  # mint glass orb: deep teal edge to a bright mint centre
        k = 1 - r / 30
        d.ellipse((32 - r, 32 - r, 32 + r, 32 + r),
                  fill=(int(11 + 111 * k), int(59 + 188 * k), int(72 + 133 * k), 255))
    d.ellipse((18, 14, 30, 24), fill=(255, 255, 255, 200))  # highlight
    return img


class SettingsApi:
    """Methods callable from settings.html as window.pywebview.api.*"""

    def __init__(self, nova):
        self._nova = nova

    def get_state(self):
        r = self._nova.router
        return {
            "config": self._nova.cfg,
            "languages": LANGUAGES,
            "default_names": DEFAULT_NAMES,
            "macros": dict(r.macros),
            "learned": {k: [a.get("skill") for a in v["actions"]] for k, v in list(r.learned.items())},
            "memory": dict(self._nova.memory.stats(), rules=len(r.learned), macros=len(r.macros)),
        }

    # --- AI connection -----------------------------------------------------

    def ai_status(self, provider):
        return brain.provider_status(provider)

    def ai_install(self, provider):
        return brain.install_provider(provider)

    def ai_login(self, provider):
        brain._WARM.close()  # spares started before signing in would still be signed out
        brain.login_provider(provider)
        return True

    def ai_test(self, provider):
        """Send one tiny request through the chosen subscription and time it."""
        cfg = dict(self._nova.cfg, brain=provider)
        brain._WARM.close()  # test what is installed and signed in now, not an old spare
        started = time.time()
        try:
            test = brain.Brain(cfg)
            test.one_off = True
            answer = test.ask("ping", [])
        except Exception as e:  # noqa: BLE001 - the button must always get an answer
            return {"ok": False, "error": str(e) or type(e).__name__}
        return {"ok": True, "seconds": round(time.time() - started, 1), "say": answer.get("say", "")}

    def save(self, cfg):
        """Returns "" when all good, or a message to show (bad shortcut)."""
        return self._nova.apply_settings(config.clean(cfg))

    def record_hotkey(self):
        """The next key combination, or "" if none within 8 s or the window was closed:
        a forgotten recording must not grab the user's Ctrl+C an hour later."""
        names, done = [], threading.Event()
        self._nova.recording_cancel, self._nova.recording_cancelled = done, False

        def on_key(event):
            if event.event_type == "down" and event.name and event.name not in names:
                names.append(event.name)
            elif event.event_type == "up" and names:
                done.set()

        hook = keyboard.hook(on_key)
        try:
            done.wait(8)
        finally:
            keyboard.unhook(hook)
            self._nova.recording_cancel = None
        if not names or not done.is_set() or getattr(self._nova, "recording_cancelled", False):
            self._nova.recording_cancelled = False
            return ""
        return keyboard.get_hotkey_name(names)

    def clear_memory(self):
        """Forget everything learned (not the user's own "запомни" commands)."""
        self._nova.memory.clear()
        self._nova.router.learned.clear()
        self._nova.router._save_learned()
        # Contacts Nova picked up by itself go too; facts the user dictated stay.
        self._nova.knowledge.contacts.clear()
        self._nova.knowledge.save()
        # ...and their copies: backups, old AI prompts that listed them, apps found on disk.
        data = config.DATA_DIR
        for leftover in [data / "learned.json.bak", data / "knowledge.json.bak", data / "found_apps.json",
                         data / "found_apps.json.bak", *(data / "brain").glob("system_prompt_*.txt")]:
            leftover.unlink(missing_ok=True)
        return self.get_state()

    def delete_command(self, name):
        r = self._nova.router
        r.macros.pop(name, None)
        r.learned.pop(name, None)
        router_mod._save(router_mod.MACROS_PATH, r.macros)
        router_mod._save(router_mod.LEARNED_PATH, r.learned)
        return self.get_state()


class OverlayApi:
    """What the overlay page can call: the close button."""

    def __init__(self, nova):
        self._nova = nova

    def close(self):
        self._nova.cancel()
        self._nova.ui.hide()
        return True


class UI:
    # Window = the panel itself, in logical px (scaled by DPI below). Windows 11 rounds
    # its corners and draws the shadow, so there's nothing around the panel to fake.
    SIZES = {"full": (540, 74), "orb": (74, 74), "tall": (540, 150)}
    SETTINGS_W, SETTINGS_H = 505, 645  # logical px: a tall panel, centred, scrolls inside
    EDGE = 14  # gap between the panel and the screen edge
    GLASS_FPS = 2  # live glass refreshes while visible (5 cost ~3% of a core for little)

    def __init__(self, nova):
        self.nova = nova
        nova.ui = self
        self._hide_timer: threading.Timer | None = None
        self._visible = False
        self._hwnd = 0
        # The process works in physical pixels (see __main__), so scale for 125%/150% screens.
        self.k = ctypes.windll.user32.GetDpiForSystem() / 96
        self._hide_pending: threading.Timer | None = None
        self._generation = 0
        self._show_lock = threading.RLock()
        self._tall = False
        self._hide_token = None
        self._resize()
        # Not transparent=True: WebView2 transparency doesn't render here. The glass is a
        # blurred snapshot of what's behind the overlay instead (see _backdrop).
        self.overlay = webview.create_window(
            "Nova", str(HERE / "overlay.html"), width=self.w, height=self.h, x=-4000, y=-4000,
            frameless=True, on_top=True, focus=False, resizable=False, easy_drag=False,
            background_color="#0a1a33", hidden=False, js_api=OverlayApi(nova),
        )
        # Created hidden at start (pywebview can't reliably create windows later from the
        # tray's thread); while hidden its browser engine is put into low-memory mode.
        self.settings = webview.create_window(
            t("settings_title"), str(HERE / "settings.html"), width=round(self.SETTINGS_W * self.k),
            height=round(self.SETTINGS_H * self.k), hidden=True, js_api=SettingsApi(nova),
            background_color="#071327", min_size=(440, 480),
        )
        self.settings.events.closing += self._on_settings_closing
        self.settings.events.shown += self._style_settings
        self.tray = pystray.Icon("Nova", _tray_image(), "Nova", menu=pystray.Menu(
            pystray.MenuItem(lambda item: t("tray_settings"), self.open_settings, default=True),
            pystray.MenuItem(lambda item: t("tray_quit"), self.quit),
        ))

    def open_settings(self, *_):
        try:
            self._memory(low=False, window=self.settings)
            self.settings.show()
            self.settings.restore()
            self._center_settings()
            run = getattr(self.settings, "run_js", None) or self.settings.evaluate_js
            run("window.unselect && unselect()")
        except Exception:  # noqa: BLE001 - pystray swallows errors silently
            log.exception("Opening settings failed")

    def _center_settings(self) -> None:
        """Open in the middle of the screen the user is on, fully visible."""
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        hwnd = int(self.settings.native.Handle.ToInt64())
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        info = (ctypes.c_long * 10)()
        info[0] = 40
        point = wt.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
        user32.GetMonitorInfoW(user32.MonitorFromPoint(point, 2), info)  # where the user is looking
        left, top, right, bottom = info[5], info[6], info[7], info[8]  # work area
        w = min(round(self.SETTINGS_W * self.k), right - left - 40)
        h = min(round(self.SETTINGS_H * self.k), bottom - top - 40)
        SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10
        user32.SetWindowPos(hwnd, 0, left + (right - left - w) // 2, top + (bottom - top - h) // 2, w, h,
                            SWP_NOZORDER)

    def _on_settings_closing(self):
        if not self.nova.cfg.get("onboarded"):
            # Closed the welcome without choosing: keep the Windows language, don't ask again.
            threading.Thread(target=self.nova.apply_settings, args=({"onboarded": True},), daemon=True).start()
        if getattr(self.nova, "recording_cancel", None):  # a shortcut recording is still waiting
            self.nova.recording_cancelled = True
            self.nova.recording_cancel.set()
        self.settings.hide()
        self._memory(low=True, window=self.settings)
        return False  # keep the window (hidden, trimmed) for the next time

    def _style_settings(self):
        """Dark title bar so the window matches the navy design."""
        try:
            hwnd = int(self.settings.native.Handle.ToInt64())
            dark = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)
            caption = ctypes.c_int(0x00271307)  # COLORREF of #071327
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass

    # --- overlay -----------------------------------------------------------

    def _js(self, code: str) -> None:
        try:
            self.overlay.evaluate_js(code)
        except Exception:  # noqa: BLE001 - window not ready yet
            pass

    # Game mode: set while the user is in a fullscreen app, so the overlay never
    # pops up over the game (Nova keeps listening and acting).
    quiet = False

    def _place(self) -> tuple[int, int]:
        """Bottom centre of the work area of the monitor the user is working on."""
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = ctypes.c_void_p  # HMONITOR doesn't fit a C int
        user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        info = (ctypes.c_long * 10)()
        info[0] = 40  # MONITORINFO.cbSize
        monitor = user32.MonitorFromWindow(user32.GetForegroundWindow(), 2)  # MONITOR_DEFAULTTONEAREST
        user32.GetMonitorInfoW(monitor, info)
        # This monitor's own scaling (one at 100 %, another at 150 %): size the panel for it.
        dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
        try:
            ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(monitor), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
            if dpi_x.value and abs(dpi_x.value / 96 - self.k) > 0.01:
                self.k = dpi_x.value / 96
                self._resize()
        except (AttributeError, OSError):
            pass
        left, top, right, bottom = info[5], info[6], info[7], info[8]  # rcWork
        x = left + (right - left - self.w) // 2
        if self.nova.cfg.get("overlay_position", "top") == "bottom":
            return x, bottom - self.h - round(self.EDGE * self.k)
        return x, top + round(self.EDGE * self.k)

    def _resize(self) -> None:
        w, h = self.SIZES["tall"] if self._tall else self.SIZES.get(
            self.nova.cfg.get("overlay_style", "full"), self.SIZES["full"])
        self.w, self.h = round(w * self.k), round(h * self.k)

    def _backdrop(self, x: int, y: int) -> None:
        """Frosted glass: a blurred view of what's behind the panel. The window is
        excluded from screen capture, so this sees the desktop, never Nova itself."""
        try:
            import base64

            import cv2

            from .screen import _grab

            img = _grab(x, y, self.w, self.h)
            small = cv2.resize(img, (max(8, self.w // 5), max(8, self.h // 5)), interpolation=cv2.INTER_AREA)
            small = cv2.GaussianBlur(small, (0, 0), 3.5)
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
            self._js(f"setBackdrop('data:image/jpeg;base64,{base64.b64encode(buf.tobytes()).decode()}')")
        except Exception:  # noqa: BLE001 - no glass: plain navy
            pass

    def _live_glass(self, x: int, y: int, generation: int) -> None:
        """Keep the glass alive while the panel is visible: video behind it keeps moving."""
        while self._visible and self._generation == generation and not self._hide_pending:
            time.sleep(1 / self.GLASS_FPS)
            if self._visible and self._generation == generation:
                self._backdrop(x, y)

    def _show(self) -> None:
        with self._show_lock:
            self._show_locked()

    def _show_locked(self) -> None:
        if self._hide_pending:  # asked to show again while fading out: just stay
            self._hide_pending.cancel()
            self._hide_pending = None
            self._hide_token = None  # a fade already running must not hide it now
            if self._visible:
                self._generation += 1
                x, y = self._place()
                if not os.environ.get("NOVA_OVERLAY_IN_CAPTURE"):  # the glass stopped with the fade
                    threading.Thread(target=self._live_glass, args=(x, y, self._generation), daemon=True).start()
                self._js("enter()")
                return
        if not self._hwnd and not self.quiet:
            try:
                self._style_overlay()  # the first try at startup timed out
            except Exception:  # noqa: BLE001
                return
        if self._visible or self.quiet or not self._hwnd:
            return
        try:
            self._memory(low=False)
            self._resize()
            x, y = self._place()
            cfg = self.nova.cfg
            self._js(f"setMode({json.dumps(cfg.get('overlay_style', 'full'))}, "
                     f"{json.dumps(cfg.get('overlay_position', 'top'))})")
            self._backdrop(x, y)
            SWP_NOACTIVATE, SWP_SHOWWINDOW, HWND_TOPMOST = 0x10, 0x40, -1
            ctypes.windll.user32.SetWindowPos(self._hwnd, HWND_TOPMOST, x, y, self.w, self.h,
                                              SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self._visible = True
            self._esc(True)
            self._generation += 1
            if not os.environ.get("NOVA_OVERLAY_IN_CAPTURE"):  # (debug mode would sample itself)
                threading.Thread(target=self._live_glass, args=(x, y, self._generation), daemon=True).start()
            self._js("enter()")
        except Exception:  # noqa: BLE001 - the overlay must never break a request
            log.exception("Showing the overlay failed")

    def state(self, state: str, text: str = "", question: bool = False) -> None:
        if self._hide_timer:
            self._hide_timer.cancel()
        # What must be read in full gets a taller panel: a question the user answers
        # (the whole message to approve), an error, a long answer.
        tall = question or (state in ("error", "speaking") and len(text) > 70)
        with self._show_lock:
            self._set_tall(tall)
        self._show()
        self._js(f"setState({json.dumps(state)}, {json.dumps(text)}, {json.dumps(tall)})")

    def _set_tall(self, tall: bool) -> None:
        if tall == self._tall:
            return
        self._tall = tall
        self._resize()
        if self._visible and self._hwnd:
            x, y = self._place()
            SWP_NOACTIVATE, SWP_NOZORDER = 0x10, 0x4
            ctypes.windll.user32.SetWindowPos(self._hwnd, 0, x, y, self.w, self.h, SWP_NOACTIVATE | SWP_NOZORDER)
            self._backdrop(x, y)

    def set_language(self, code: str) -> None:
        self._js(f"setLang({json.dumps(code)})")
        try:
            self.tray.update_menu()  # pystray builds the menu once: rebuild it in the new language
        except Exception:  # noqa: BLE001
            pass
        try:
            self.settings.set_title(t("settings_title"))
        except Exception:  # noqa: BLE001
            pass

    def level(self, value: float) -> None:
        self._js(f"setLevel({value:.4f})")

    def hide(self, animate: bool = True) -> None:
        """Fade out, then really hide the window."""
        with self._show_lock:
            self._hide_locked(animate)

    def _hide_locked(self, animate: bool) -> None:
        if not self._visible:
            return
        self._js("leave()")
        token = self._hide_token = object()

        def gone():
            with self._show_lock:
                if self._hide_token is not token:
                    return  # shown again meanwhile
                self._gone()

        if not animate:
            self._gone()
            return
        self._hide_pending = threading.Timer(0.3, gone)
        self._hide_pending.daemon = True
        self._hide_pending.start()

    def _esc(self, enabled: bool) -> None:
        hotkeys = getattr(self.nova, "hotkeys", None)
        if hotkeys is not None:
            hotkeys.esc(enabled, wait=False)  # queued in order; never blocks the panel

    def _gone(self) -> None:
        self._esc(False)
        self._hide_pending = None
        self._js("setState('idle', ''); sleep(true)")
        self._memory(low=True)
        if self._hwnd:
            SWP_NOACTIVATE, SWP_HIDEWINDOW, SWP_NOMOVE, SWP_NOSIZE = 0x10, 0x80, 0x2, 0x1
            ctypes.windll.user32.SetWindowPos(self._hwnd, 0, 0, 0, 0, 0,
                                              SWP_NOACTIVATE | SWP_HIDEWINDOW | SWP_NOMOVE | SWP_NOSIZE)
        self._visible = False

    def hide_later(self, seconds: float) -> None:
        if self._hide_timer:
            self._hide_timer.cancel()
        self._hide_timer = threading.Timer(seconds, self.hide)
        self._hide_timer.daemon = True
        self._hide_timer.start()

    def _memory(self, low: bool, window=None) -> None:
        """While hidden, ask WebView2 to trim its memory; back to normal before showing."""
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level
            from System import Action

            form = (window or self.overlay).native
            target = Level.Low if low else Level.Normal

            def apply():
                # WebView2 may only be touched on its own UI thread: reading CoreWebView2 from
                # any other thread can block forever (it froze Nova's startup once).
                try:
                    core = form.browser.webview.CoreWebView2
                    if core is not None:
                        core.MemoryUsageTargetLevel = target
                except Exception as e:  # noqa: BLE001 - older WebView2 runtime: just no trimming
                    log.debug("WebView2 memory level not set: %s", e)

            form.BeginInvoke(Action(apply))  # queued, never waits
        except Exception as e:  # noqa: BLE001
            log.debug("WebView2 memory level not set: %s", e)

    # --- lifecycle ---------------------------------------------------------

    def _style_overlay(self) -> None:
        """Pill shape, no taskbar button, never takes keyboard focus."""
        if not self.overlay.events.shown.wait(10):
            raise RuntimeError("overlay window not shown yet")  # retried on the first _show
        hwnd = int(self.overlay.native.Handle.ToInt64())
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_APPWINDOW = -20, 0x80, 0x08000000, 0x40000
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW)
        # Smooth rounded corners and a soft shadow from Windows 11 itself.
        DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND = 33, ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                                   ctypes.byref(DWMWCP_ROUND), 4)
        # Invisible to screen capture: the live glass can sample what's behind the panel
        # without seeing itself (and Nova's screenshots for the AI never include it).
        if not os.environ.get("NOVA_OVERLAY_IN_CAPTURE"):
            _user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
        self._hwnd = hwnd
        self._visible = True
        self.hide(animate=False)

    def _start(self) -> None:
        try:
            self._style_overlay()
        except Exception:  # noqa: BLE001 - cosmetic only
            log.exception("Overlay styling failed")
        self.set_language(self.nova.cfg["language"])
        self.nova.boot()

    def quit(self, *_):
        self.tray.stop()
        brain._WARM.close()  # os._exit skips atexit: don't leave claude processes behind
        os._exit(0)

    def run(self) -> None:
        self.tray.run_detached()
        webview.start(self._start, icon=str(ICON) if ICON.exists() else None)