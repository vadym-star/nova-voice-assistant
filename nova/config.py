"""User settings, stored as JSON in %APPDATA%\\Nova."""

import json
import math
import os
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Nova"
CONFIG_PATH = DATA_DIR / "config.json"
MODELS_DIR = DATA_DIR / "models"
LOG_PATH = DATA_DIR / "nova.log"

DEFAULTS = {
    # Name the assistant answers to (wake word in always-listen mode).
    "name": "Нова",
    # Interface and reply language: ru, en, zh (see nova/i18n.py).
    "language": "ru",
    # Speech recognition language: "auto" understands any language.
    "stt_language": "auto",
    # Push-to-talk shortcut, in `keyboard` library syntax.
    "hotkey": "ctrl+alt+space",
    # "hold": talk while the shortcut is held. "toggle": press to start, press again to stop.
    "hotkey_mode": "hold",
    # Experimental: listen all the time and react to the assistant's name.
    "wake_word_enabled": True,
    # Brain for anything the fast path can't handle: "claude", "codex" or "off".
    "brain": "claude",
    "claude_model": "haiku",
    "codex_model": "",
    # faster-whisper model size: tiny, base, small, medium, large-v3-turbo.
    "stt_model": "small",
    # "auto" tries the GPU first and falls back to the CPU.
    "stt_device": "auto",
    "confirm_dangerous": True,
    # Before sending a message: "unsure" (only when Nova had to guess the chat), "always", "never".
    "confirm_messages": "unsure",
    # No overlay over fullscreen apps (games, videos).
    "game_mode": True,
    # No synthetic keys or clicks at all while a game with anti-cheat runs (ban risk).
    "anticheat_guard": True,
    # After the AI found a button by sight, let it map the rest of that screen in the background.
    "app_mapping": True,
    # Shown the first-start welcome (language choice) already.
    "onboarded": False,
    # Start through the UAC prompt, to control administrator windows too.
    "run_as_admin": False,
    # Soft chimes when Nova starts/stops listening.
    "sounds": True,
    # "windows" (Windows' speech sounds, quieter) or "ticks" (tiny synthesized ticks).
    "sound_style": "windows",
    # Nova hides if nothing is said for this long after being called (seconds).
    "silence_timeout": 5,
    # Where the overlay appears ("top" / "bottom") and how ("full": orb + text, "orb": just the orb).
    "overlay_position": "top",
    "overlay_style": "full",
}


_WRITE_LOCK = threading.Lock()


def write_json(path: Path, data) -> None:
    """Save without ever leaving a half-written file: temp file, keep a .bak, then swap."""
    with _WRITE_LOCK:
        _write_json(path, data)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Own temp name per writer: two threads saving at once must not share one.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except BaseException:  # disk full, read-only folder: no half-written leftovers
        tmp.unlink(missing_ok=True)
        raise
    # Antivirus and the search indexer briefly lock fresh files on Windows: retry.
    for attempt in range(10):
        try:
            if path.exists():
                os.replace(path, path.with_name(path.name + ".bak"))
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


def read_json(path: Path, default):
    """Read a JSON file, falling back to its .bak copy if the main one is damaged."""
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            return json.loads(candidate.read_text(encoding="utf-8-sig"))  # -sig: saved by Notepad
        except (OSError, ValueError):  # ValueError covers bad JSON and broken UTF-8
            continue
    return default


def clean(values) -> dict:
    """Only known settings with the right types: a hand-edited config.json or the
    settings page can't put a string where a number belongs and crash Nova."""
    out = {}
    for key, value in (values if isinstance(values, dict) else {}).items():
        if key not in DEFAULTS:
            continue  # unknown or from an older version
        default = DEFAULTS[key]
        if isinstance(default, bool):
            if isinstance(value, bool):
                out[key] = value
        elif isinstance(default, (int, float)):
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                try:
                    number = float(value)
                except ValueError:
                    continue
                if math.isfinite(number) and 0 <= number <= 3600:
                    out[key] = number
        elif isinstance(value, str) and len(value) <= 200:
            if key in CHOICES and value not in CHOICES[key]:
                continue  # "stt_model": "../../x" and the like
            out[key] = value
    return out


# Settings that may only take one of these values.
CHOICES = {
    "language": ("ru", "en", "zh"), "stt_language": ("auto", "ru", "uk", "en", "zh", "de", "fr", "es", "it", "pt",
                                                     "pl", "tr", "ja", "ko"),
    "hotkey_mode": ("hold", "toggle"), "brain": ("claude", "codex", "off"),
    "stt_model": ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"),
    "stt_device": ("auto", "cuda", "cpu"), "confirm_messages": ("unsure", "always", "never"),
    "sound_style": ("windows", "ticks"), "overlay_position": ("top", "bottom"), "overlay_style": ("full", "orb"),
    "claude_model": ("haiku", "sonnet", "opus"),
}


def _windows_language() -> str:
    """The Windows display language as one of Nova's: ru (also uk, be, kk), zh, or en."""
    try:
        import ctypes

        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
    except (AttributeError, OSError):
        return "en"
    return {0x19: "ru", 0x22: "ru", 0x23: "ru", 0x3F: "ru", 0x04: "zh"}.get(primary, "en")


_FIRST_NAMES = {"ru": "Нова", "en": "Nova", "zh": "诺娃"}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if not CONFIG_PATH.exists() and not CONFIG_PATH.with_name(CONFIG_PATH.name + ".bak").exists():
        # First start: speak the user's language from the first second, not Russian.
        cfg["language"] = _windows_language()
        cfg["name"] = _FIRST_NAMES[cfg["language"]]
    saved = read_json(CONFIG_PATH, {})
    if isinstance(saved, dict) and saved and "onboarded" not in saved:
        saved["onboarded"] = True  # from a version without the welcome: not a new user
    cfg.update(clean(saved))
    return cfg


def save(cfg: dict) -> None:
    write_json(CONFIG_PATH, cfg)
