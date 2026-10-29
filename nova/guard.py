"""Things Windows and games can do to Nova behind its back, and what Nova does about them.

- Administrator windows: a normal process can't hook keys in them or press their
  buttons (UIPI). Nova can restart itself elevated, and says so when this bites.
- Anti-cheat games: synthetic keys and clicks while one runs can get an account
  flagged. Nova refuses to send any while such a game is running.
"""

import ctypes
import ctypes.wintypes as wt
import logging
import sys
import threading
import time
from pathlib import Path

import psutil

log = logging.getLogger(__name__)
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
shell32 = ctypes.windll.shell32

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
advapi32.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                         ctypes.POINTER(wt.DWORD)]
shell32.ShellExecuteW.restype = ctypes.c_void_p
shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]

# Processes of kernel anti-cheats (they run only while a protected game does) and of
# games whose anti-cheat has no process of its own. Lower-case.
ANTI_CHEAT = {
    "easyanticheat.exe", "easyanticheat_eos.exe",          # Easy Anti-Cheat
    "beservice.exe", "beservice_x64.exe",                  # BattlEye
    "eaanticheat.gameservice.exe",                         # EA Javelin
    # FACEIT: its client sits in the tray all day — see _PAIRS: only with CS2 running.
    "sguard64.exe", "ace-tray.exe",                        # Tencent ACE
    "gamemon.des", "gamemon.exe", "gamemon64.exe",         # nProtect GameGuard
    "xigncode.exe",                                        # XIGNCODE3
    "valorant-win64-shipping.exe", "league of legends.exe",  # Riot Vanguard
    "cod.exe", "modernwarfare.exe", "blackops6.exe",       # Ricochet
    "genshinimpact.exe", "yuanshen.exe", "starrail.exe", "zenlesszonezero.exe",  # mhyprot
}


# Anti-cheats that run all the time: they protect only while their game runs.
_PAIRS = {"faceitservice.exe": ("cs2.exe", "csgo.exe")}


# --- administrator rights ---------------------------------------------------

def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except OSError:
        return False


def is_elevated(hwnd: int) -> bool:
    """Is this window's process running as administrator (when Nova isn't)?"""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False
    process = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return False
    try:
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return not is_admin()  # a normal process may not read an elevated one's token
        try:
            elevated, size = wt.DWORD(), wt.DWORD()
            advapi32.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size))  # TokenElevation
            return bool(elevated.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


def relaunch(elevated: bool = True) -> bool:
    """Start a new Nova (elevated: through the UAC prompt). False if the user said no."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if windowless.exists():
        exe = windowless
    root = str(Path(__file__).resolve().parent.parent)
    verb = "runas" if elevated else "open"
    # An elevated process starts in System32 whatever directory is asked for, so the
    # command finds the package itself instead of relying on the working directory.
    code = "import os,runpy,sys; r=sys.argv[1]; os.chdir(r); sys.path.insert(0, r); runpy.run_module('nova', run_name='__main__')"
    # The folder travels as its own argument: a quote in it can't break the command.
    ok = (shell32.ShellExecuteW(None, verb, str(exe), f'-c "{code}" "{root}" --restarted', root, 1) or 0) > 32
    log.info("Restart %s: %s", "as administrator" if elevated else "normally", "started" if ok else "declined")
    return ok


# --- anti-cheat games -------------------------------------------------------

class GameGuard:
    """Knows whether an anti-cheat game is running; checked every few seconds."""

    def __init__(self, interval: float = 4.0):
        self.enabled = True
        self.game: str | None = None
        self._interval = interval
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            threading.Thread(target=self._watch, name="game-guard", daemon=True).start()

    def _watch(self) -> None:
        while True:
            try:
                running = {(p.info["name"] or "").lower() for p in psutil.process_iter(["name"])}
                found = next((n for n in running if n in ANTI_CHEAT), None) or next(
                    (n for n, games in _PAIRS.items() if n in running and running & set(games)), None)
                if found != self.game:
                    log.info("Anti-cheat game %s", f"running: {found}" if found else "closed")
                self.game = found
            except Exception:  # noqa: BLE001 - a watcher must never die
                log.debug("Process scan failed", exc_info=True)
            time.sleep(self._interval)

    @property
    def blocking(self) -> str | None:
        """The protected game's process name if synthetic input must not be sent now."""
        return self.game if self.enabled else None


games = GameGuard()
inputs_sent = 0  # how many synthetic key/click calls Nova made; read by the app


def check_input() -> None:
    """Every synthetic key press and click goes through here first."""
    global inputs_sent
    if games.blocking:
        from .i18n import t
        from .skills import SkillError

        raise SkillError(t("anticheat_blocked"))
    inputs_sent += 1


def install_input_guard() -> None:
    """Wrap the libraries' send-input functions, so no skill can get around the check."""
    import keyboard
    import uiautomation.uiautomation as uia

    def guarded(fn):
        def wrapper(*args, **kwargs):
            check_input()
            return fn(*args, **kwargs)
        wrapper.__wrapped__ = fn
        return wrapper

    if hasattr(keyboard.send, "__wrapped__"):
        return  # already installed
    for name in ("send", "write"):
        setattr(keyboard, name, guarded(getattr(keyboard, name)))
    for name in ("Click", "RightClick", "MiddleClick", "DragDrop", "WheelUp", "WheelDown"):
        if hasattr(uia, name):
            setattr(uia, name, guarded(getattr(uia, name)))
    import uiautomation
    for name in ("Click", "RightClick", "MiddleClick", "DragDrop", "WheelUp", "WheelDown"):
        if hasattr(uiautomation, name):
            setattr(uiautomation, name, getattr(uia, name))


def session_locked() -> bool:
    """Is the PC locked (or showing another user's / the secure desktop)?"""
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    try:
        return not user32.SwitchDesktop(desk)
    finally:
        user32.CloseDesktop(desk)


user32.OpenInputDesktop.restype = wt.HANDLE
user32.SwitchDesktop.argtypes = [wt.HANDLE]
user32.CloseDesktop.argtypes = [wt.HANDLE]


# --- what must never reach the AI, and when a command may not be the user's -----

# Screens that must never be sent to the AI or kept in visual memory.
PRIVATE_APPS = {
    "keepass.exe", "keepassxc.exe", "keepassx.exe", "1password.exe", "bitwarden.exe", "lastpass.exe",
    "dashlane.exe", "enpass.exe", "nordpass.exe", "roboform.exe", "keeperpasswordmanager.exe",
    "proton pass.exe", "credentialuibroker.exe", "consent.exe",
}


def is_private(hwnd: int) -> bool:
    from .screen import app_name

    root = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    return app_name(root) in PRIVATE_APPS


def speakers_playing() -> bool:
    """Is the PC itself making sound through speakers (not headphones) right now?

    Then a phrase heard in always-listen mode may have come from a video, a stream or
    a voice chat rather than from the user, and Nova treats it with more care.
    """
    global _meters, _meters_at
    if not _meters or time.time() - _meters_at > 30:  # devices change rarely: look them up twice a minute
        _meters, _meters_at = _speaker_meters(), time.time()
    peak = 0.0
    for i in range(2):  # speech has gaps: look twice
        for meter in _meters:
            try:
                peak = max(peak, meter.GetPeakValue())
            except Exception:  # noqa: BLE001 - device gone meanwhile
                _meters_at = 0.0
        if i == 0:
            time.sleep(0.03)
    return peak > 0.02


_meters: list = []
_meters_at = 0.0


def _speaker_meters() -> list:
    """Level meters of the outputs the microphone could hear: the default device and the
    default *communications* device (where Discord and Teams play), headphones left out."""
    try:
        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

        comtypes.CoInitialize()  # threads other than the main one need COM set up first
        enumerator = AudioUtilities.GetDeviceEnumerator()
        meters, seen = [], set()
        for role in (0, 2):  # eConsole, eCommunications
            try:
                device = AudioUtilities.CreateDevice(enumerator.GetDefaultAudioEndpoint(0, role))
            except Exception:  # noqa: BLE001 - no such device
                continue
            if device.id in seen:
                continue
            seen.add(device.id)
            # PKEY_AudioEndpoint_FormFactor: 3 headphones, 5 headset — the mic can't hear them.
            if device.properties.get("{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0") in (3, 5):
                continue
            meters.append(device._dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None).QueryInterface(
                IAudioMeterInformation))
        return meters
    except Exception:  # noqa: BLE001 - unknown means "the usual care"
        log.debug("Output meters unavailable", exc_info=True)
        return []
