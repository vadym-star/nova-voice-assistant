"""End-to-end voice check: synthesized speech -> wake word -> Whisper -> router.

Nothing is executed on the PC: skills and the AI are replaced by recorders.
Uses the Windows speech voices (System.Speech), so it runs offline.

    python tools/voice_bench.py            # all voices, clean + noisy
    python tools/voice_bench.py --quick    # one voice per language, clean only
"""

import argparse
import subprocess
import sys
import tempfile
import time
import wave
from types import SimpleNamespace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import config, i18n, skills  # noqa: E402
from nova.app import GATE_SURE, NAME_SURE, Nova  # noqa: E402
from nova.audio import UtteranceListener  # noqa: E402
from nova.router import Router  # noqa: E402
from nova.skills import Context  # noqa: E402
from nova.stt import NameGate, SpeechToText  # noqa: E402
from nova.text import normalize, split_name, strip_name_raw  # noqa: E402

VOICES = {"ru": ["Microsoft Irina Desktop"], "en": ["Microsoft Zira Desktop", "Microsoft David Desktop"]}
NAMES = {"ru": "Нова", "en": "Nova"}

# (phrase, expected skill or "AI", words one of which must be in the skill's arguments)
COMMANDS = {
    "ru": [
        ("Нова, открой телеграм", "open", ["телеграм", "telegram"]),
        ("Нова, открой хром", "open", ["хром", "chrome"]),
        ("Нова, громкость тридцать", "volume", ["30", "тридцать"]),
        ("Нова, сделай громче", "volume", ["громче"]),
        ("Нова, следующий трек", "media", ["следующ"]),
        ("Нова, пауза", "media", ["пауз"]),
        ("Нова, сделай скриншот", "screenshot", []),
        ("Нова, который час", "time", []),
        ("Нова, какое сегодня число", "date", []),
        ("Нова, найди рецепт борща", "search", ["борщ"]),
        ("Нова, найди на ютубе музыку для работы", "search", ["музык"]),
        ("Нова, закрой это окно", "close", ["окно"]),
        ("Нова, нажми на кнопку сохранить", "click", ["сохран"]),
        ("Нова, прокрути вниз", "scroll", ["вниз"]),
        ("Нова, напиши Вове, что я буду через десять минут", "send_message", ["вов"]),
        ("Нова, посмотри сюда, что это за ошибка", "look", []),
        ("Нова, какая столица Австралии", "AI", []),
        ("Нова, отмена", "DISMISS", []),
    ],
    "en": [
        ("Nova, open Telegram", "open", ["telegram"]),
        ("Nova, open Chrome", "open", ["chrome"]),
        ("Nova, set volume to forty", "volume", ["40", "forty"]),
        ("Nova, next track", "media", ["next"]),
        ("Nova, take a screenshot", "screenshot", []),
        ("Nova, what time is it", "time", []),
        ("Nova, search for pancake recipes", "search", ["pancake"]),
        ("Nova, close this window", "close", ["window"]),
        ("Nova, click on save", "click", ["save"]),
        ("Nova, scroll down", "scroll", ["down"]),
        ("Nova, what is the capital of Australia", "AI", []),
        ("Nova, cancel", "DISMISS", []),
    ],
}

# Everyday speech that must NOT wake Nova (some of it sounds close to the name).
NOT_FOR_NOVA = {
    "ru": [
        "Я купил новый телефон на прошлой неделе",
        "Это новая версия игры, она вышла вчера",
        "Ну вот, опять дождь пошёл",
        "Нормально всё, не переживай",
        "Давай созвонимся завтра вечером",
        "Снова забыл зарядку дома",
        "Новости сегодня совсем не радуют",
        "Открой окно, пожалуйста, душно",
        "Она пошла домой пораньше",
        "Ова, смотри какая красота",
        "Она сказала, что перезвонит",
        "Норм, давай так и сделаем",
        "Нормально всё прошло",
    ],
    "en": [
        "I bought a new phone last week",
        "Let's grab some coffee later",
        "Nobody knows what happened yesterday",
        "Open the window please, it's hot in here",
        "Supernova explosions are really bright",
        "I'll call you back in a minute",
    ],
}


def synthesize(text: str, voice: str, path: Path) -> np.ndarray:
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.SelectVoice('{voice}'); $s.Rate = 0;"
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{path}', $f); $s.Speak(@'\n{text}\n'@); $s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def add_noise(audio: np.ndarray, snr_db: float, rng) -> np.ndarray:
    """Room-like noise: pink-ish hum under the voice at the given signal-to-noise ratio."""
    white = rng.standard_normal(audio.size + 64).astype(np.float32)
    pink = np.convolve(white, np.ones(64) / 64, mode="valid")[: audio.size] * 4 + white[: audio.size] * 0.3
    power = np.mean(audio ** 2) / (10 ** (snr_db / 10))
    return (audio + pink * np.sqrt(power / np.mean(pink ** 2))).astype(np.float32)


class Recorder:
    """Stands in for every skill and the AI: remembers what would have run."""

    def __init__(self, router: Router):
        self.calls = []
        router._run_skill = self._skill
        router._ask_brain = self._brain

    def _skill(self, name, args, ctx, raise_errors=False):
        self.calls.append((name, args))
        return "ok"

    def _brain(self, t, raw, ctx, on_status):
        self.calls.append(("AI", {"text": raw}))
        return "ok"


def wakes(audio, gate: NameGate, stt: SpeechToText, name: str, lang: str, is_command):
    """The same decision the live wake loop makes; returns (woke, command, timings)."""
    t0 = time.perf_counter()
    early = audio[: int(16000 * UtteranceListener.EARLY_S)]
    quick = gate.text(early, name, lang) if gate.has_speech(early) else ""
    gate_ms = (time.perf_counter() - t0) * 1000
    if not quick or split_name(quick, name, GATE_SURE, is_command) is None:
        return False, quick, {"gate": gate_ms}
    # As in the app: the main model checks the same 1.5 s before Nova shows up.
    early_main = stt.transcribe(early)
    if split_name(early_main, name, NAME_SURE, is_command) is None:
        return False, f"{quick} / early main: {early_main}", {"gate": gate_ms}
    t1 = time.perf_counter()
    text = stt.transcribe(audio)
    stt_ms = (time.perf_counter() - t1) * 1000
    if (rest := split_name(text, name, NAME_SURE, is_command)) is None:
        return False, text, {"gate": gate_ms, "stt": stt_ms}
    if split_name(text, name, NAME_SURE) is None:
        text = rest  # what the live loop does with "Новая пауза"
    return True, text, {"gate": gate_ms, "stt": stt_ms}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    skills.load_all()
    print("Loading models…", flush=True)
    stt = SpeechToText("small", "auto", "auto")
    gate = NameGate(stt.device)
    stt.detector = gate.model  # as in the live app
    # First calls pay for CUDA and the speech detector loading; the live app does this at boot.
    gate.has_speech(np.zeros(16000, np.float32))
    gate.text(np.zeros(16000, np.float32), "Nova", "en")
    stt.transcribe(np.zeros(16000, np.float32))
    rng = np.random.default_rng(7)
    tmp = Path(tempfile.mkdtemp(prefix="nova-bench-"))
    import nova.router as router_mod

    router_mod.MACROS_PATH, router_mod.LEARNED_PATH = tmp / "m.json", tmp / "l.json"  # never the user's

    conditions = [("clean", None)] if args.quick else [("clean", None), ("noise 15dB", 15.0)]
    fails, ok_total, n_total, timings = [], 0, 0, []
    false_wakes, neg_total = [], 0

    for lang, voices in VOICES.items():
        i18n.set_language(lang)
        name = NAMES[lang]
        cfg = dict(config.DEFAULTS, name=name, language=lang)
        Nova.update_hint(SimpleNamespace(cfg=cfg, stt=stt))  # the same prompts the live app uses
        is_command = Router(cfg).is_command
        for voice in voices[:1] if args.quick else voices:
            for cond, snr in conditions:
                print(f"\n== {lang} · {voice.split()[1]} · {cond}")
                for i, (phrase, want, words) in enumerate(COMMANDS[lang]):
                    audio = synthesize(phrase, voice, tmp / f"{lang}{i}.wav")
                    if snr:
                        audio = add_noise(audio, snr, rng)
                    woke, text, tm = wakes(audio, gate, stt, name, lang, is_command)
                    n_total += 1
                    got = "—"
                    if woke:
                        router = Router(cfg)
                        rec = Recorder(router)
                        ctx = Context(config=cfg, confirm=lambda q: True, ask=lambda q: "")
                        t2 = time.perf_counter()
                        router.handle(text, ctx)
                        tm["route"] = (time.perf_counter() - t2) * 1000
                        if ctx.extra.get("dismissed"):
                            got = "DISMISS"
                        elif rec.calls:
                            got = rec.calls[0][0]
                        values = " ".join(str(v) for _, a in rec.calls[:1] for v in a.values())
                        good = got == want and (not words or any(w in normalize(values) for w in words))
                    else:
                        good = False
                    timings.append(tm)
                    ok_total += good
                    mark = "✓" if good else "✗"
                    detail = f"heard {text!r}" + ("" if woke else "  (did not wake)")
                    print(f"  {mark} {phrase:<48} → {got:<12} {detail}")
                    if not good:
                        fails.append((lang, voice, cond, phrase, want, got, text))
                for i, phrase in enumerate(NOT_FOR_NOVA[lang]):
                    audio = synthesize(phrase, voice, tmp / f"n{lang}{i}.wav")
                    if snr:
                        audio = add_noise(audio, snr, rng)
                    woke, text, _ = wakes(audio, gate, stt, name, lang, is_command)
                    neg_total += 1
                    if woke:
                        false_wakes.append((lang, voice, cond, phrase, text))
                    print(f"  {'✗ WOKE' if woke else '✓ quiet'} {phrase:<48} heard {text!r}")

    def avg(key):
        vals = [t[key] for t in timings if key in t]
        return f"{np.mean(vals):.0f} ms (max {np.max(vals):.0f})" if vals else "n/a"

    print("\n==== Summary")
    print(f"Commands understood: {ok_total}/{n_total}")
    print(f"False wake-ups:      {len(false_wakes)}/{neg_total}")
    print(f"Name gate (tiny):    {avg('gate')}")
    print(f"Recognition (small): {avg('stt')}")
    print(f"Routing:             {avg('route')}")
    for f in false_wakes:
        print("  false wake:", f)


if __name__ == "__main__":
    main()
