"""Entry point: python -m nova"""

import ctypes
import logging
import logging.handlers
import sys
import time

from . import config


def main() -> None:
    # Physical pixels everywhere: screenshots, window bounds and clicks must agree
    # on screens with 125%/150% scaling. Must run before any window is created.
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    # Nova's own identity on the taskbar: its windows group under Nova's icon, not Python's.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Nova.VoiceAssistant")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        # Bounded: 3 x 1 MB. The log holds transcripts, so it must not grow forever.
        handlers=[logging.handlers.RotatingFileHandler(config.LOG_PATH, maxBytes=1_000_000, backupCount=2,
                                                       encoding="utf-8"), logging.StreamHandler()],
    )
    from . import guard

    restarted = "--restarted" in sys.argv
    if not restarted and config.load().get("run_as_admin") and not guard.is_admin() and guard.relaunch(True):
        return  # the elevated copy takes over; if UAC said no, carry on as is
    # One Nova at a time: two would fight over the shortcut and the microphone.
    # A restarted Nova waits for the old one to finish closing.
    kernel32 = ctypes.windll.kernel32
    for _ in range(40 if restarted else 1):
        mutex = kernel32.CreateMutexW(None, False, "NovaVoiceAssistant")
        error = kernel32.GetLastError()
        # 183: already exists. No handle at all (access denied): an elevated Nova owns it.
        if mutex and error != 183:
            break
        kernel32.CloseHandle(mutex)
        time.sleep(0.25)
    else:
        ctypes.windll.user32.MessageBoxW(None, "Nova is already running — look for it in the tray.\n"
                                         "Nova уже запущена — она в трее.", "Nova", 0x40)
        sys.exit(0)

    try:
        from .app import Nova
        from .ui import UI

        nova = Nova()
        UI(nova).run()
    except Exception:  # noqa: BLE001 - started without a console: the user must see something
        logging.exception("Nova failed to start")
        ctypes.windll.user32.MessageBoxW(
            None, f"Nova could not start. Details are in\n{config.LOG_PATH}\n\n"
                  "Common causes: Microsoft Edge WebView2 Runtime missing, or packages not installed "
                  "(pip install -r requirements.txt).", "Nova", 0x10)
        sys.exit(1)


if __name__ == "__main__":
    main()
