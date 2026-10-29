"""UI sounds: Windows' own speech sounds made lighter (default), or tiny quiet ticks.

What makes a sound pleasant rather than shrill: mid frequencies (500-800 Hz, not
1-2 kHz), a slow attack instead of a click, notes that glide into each other,
almost no overtones, a short room-like tail and a low level. Synthesized once,
played through Windows' own player.
"""

import io
import logging
import os
import threading
import wave
import winsound

import numpy as np
from pathlib import Path

RATE = 44100


def _blip(f0: float, f1: float, dur: float, level: float) -> bytes:
    """One tiny, soft, low "tick": a pure tone that fades in and out within `dur` seconds."""
    n = int(RATE * dur)
    t = np.arange(n) / RATE
    freq = np.linspace(f0, f1, n)  # a slight glide so it feels like a gesture, not a beep
    tone = np.sin(2 * np.pi * np.cumsum(freq) / RATE)
    env = np.sin(np.pi * t / dur) ** 2  # smooth in, smooth out: no click at either end
    audio = (tone * env * level * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


# name -> (from Hz, to Hz, seconds, level). Short, low and very quiet on purpose.
# Around 650-750 Hz: low enough to be soft, high enough that laptop speakers play it at all.
_RECIPES = {
    "listen": (640, 760, 0.09, 0.16),  # "I'm listening": a soft tick, slightly up
    "heard": (760, 640, 0.08, 0.13),   # "got it": the same, slightly down
    "error": (520, 440, 0.11, 0.15),   # something went wrong: lower
    "busy": (600, 600, 0.06, 0.10),    # cancelled / busy: barely there
}
VERSION = 7  # bump when a recipe changes, so the cached files are rebuilt
# The default: Windows' own speech sounds, made lighter (quieter, shorter). They are read
# from the user's Windows at runtime, never shipped with Nova.
WINDOWS_MEDIA = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Media"
_WINDOWS = {
    "listen": "Speech On.wav",
    "heard": "Speech Sleep.wav",
    "error": "Speech Misrecognition.wav",
    "busy": "Windows Navigation Start.wav",
}
LIGHTER_VOLUME, LIGHTER_MAX_S = 0.5, 0.55

_enabled = True
_style = "windows"  # "windows" | "ticks"
_lock = threading.Lock()


def _lighter(src: Path) -> bytes:
    """A quieter, shorter copy of a system sound (fades out instead of ringing on)."""
    with wave.open(str(src)) as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    if params.sampwidth != 2:
        raise ValueError("only 16-bit sounds")
    audio = np.frombuffer(frames, np.int16).reshape(-1, params.nchannels).astype(np.float32)
    audio = audio[: int(params.framerate * LIGHTER_MAX_S)] * LIGHTER_VOLUME
    fade_n = min(len(audio), int(params.framerate * 0.12))
    audio[-fade_n:] *= np.linspace(1, 0, fade_n)[:, None]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(2)
        w.setframerate(params.framerate)
        w.writeframes(audio.astype(np.int16).tobytes())
    return buf.getvalue()


def set_style(style: str) -> None:
    global _style
    _style = style if style in ("windows", "ticks") else "windows"


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = on


def _file(name: str) -> str:
    # winsound can't play from memory asynchronously, so each sound is a tiny file.
    from . import config

    folder = config.DATA_DIR / "sounds"
    src = WINDOWS_MEDIA / _WINDOWS[name]
    use_windows = _style == "windows" and src.exists()
    path = folder / (f"{name}_win_v{VERSION}.wav" if use_windows else f"{name}_v{VERSION}.wav")
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        try:
            data = _lighter(src) if use_windows else _blip(*_RECIPES[name])
        except (OSError, ValueError, wave.Error):  # unusual system sound: fall back to our tick
            data = _blip(*_RECIPES[name])
        path.write_bytes(data)
    return str(path)


def prepare() -> None:
    """Render any missing sound files (about a second, once per install)."""
    for name in _RECIPES:
        _file(name)


def play(name: str) -> None:
    """Never raises: a sound is a nicety, it must not break what Nova is doing."""
    if not _enabled or name not in _RECIPES:
        return
    try:
        with _lock:
            winsound.PlaySound(_file(name), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:  # noqa: BLE001 - no audio device, file locked, ...
        logging.getLogger(__name__).debug("Sound %s not played", name, exc_info=True)
