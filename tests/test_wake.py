from types import SimpleNamespace
import pytest
"""Always-listen mode: chatter never reaches the big model; the name wakes Nova early."""

import threading

import numpy as np

from nova import app as app_mod
from nova.app import Nova
from nova.audio import UtteranceListener


def seconds(s: float) -> np.ndarray:
    return np.zeros(int(16000 * s), dtype=np.float32)


class FakeListener:
    def __init__(self, events):
        self.events = events
        self.paused = threading.Event()
        self._stop = threading.Event()

    def flush(self):
        pass

    def utterances(self):
        yield from self.events


class FakeGate:
    """Pretends to hear `heard[i]` for the i-th quick look; [] = it wasn't speech."""

    def __init__(self, heard):
        self.heard = list(heard)

    def has_speech(self, audio):
        return self.heard[0] is not None

    def text(self, audio, name, language):
        return self.heard.pop(0) or ""


class FakeSTT:
    def __init__(self, texts):
        self.texts, self.calls = list(texts), 0
        self.device = "cpu"

    def transcribe(self, audio):
        self.calls += 1
        return self.texts.pop(0)


class FakeUI:
    quiet = False

    def __init__(self):
        self.states = []

    def state(self, s, text=""):
        self.states.append(s)

    def hide(self):
        self.states.append("hidden")


def make_nova(monkeypatch, gate_heard, stt_texts):
    nova = object.__new__(Nova)
    nova.cfg = {"name": "Нова", "language": "ru", "stt_language": "auto", "silence_timeout": 5,
                "game_mode": False}
    nova._listening, nova._speaking, nova._cancel = threading.Event(), threading.Event(), threading.Event()
    nova._work = threading.Lock()
    nova._spoke_until = 0.0
    nova._wake = None
    nova._idle_timer = None
    nova._gate = FakeGate(gate_heard)
    nova.stt = FakeSTT(stt_texts)
    nova.ui = FakeUI()
    nova.executed = []
    nova.router = SimpleNamespace(is_command=_is_command)
    nova._execute = lambda text, hwnd, low_trust=False: nova.executed.append(text)
    monkeypatch.setattr(app_mod.sounds, "play", lambda name: None)
    monkeypatch.setattr(app_mod.threading, "Timer", lambda *a, **kw: type("T", (), {"start": lambda s: None, "cancel": lambda s: None})())
    return nova


def test_name_wakes_early_and_runs_the_command(monkeypatch):
    # The main model checks the first 1.5 s too, then hears the whole phrase.
    nova = make_nova(monkeypatch, ["Нова, открой."], ["Нова, открой.", "Нова, открой Telegram."])
    nova._wake_loop(FakeListener([("early", seconds(1.5)), ("end", seconds(2.4))]))
    assert nova.ui.states[0] == "listening"  # shown while the user was still talking
    assert nova.executed == ["открой Telegram."]


def test_chatter_never_reaches_the_big_model(monkeypatch):
    nova = make_nova(monkeypatch, ["Слушай, а ты видел?", "Давай поиграем."], [])
    nova._wake_loop(FakeListener([("early", seconds(1.5)), ("end", seconds(3)),
                                  ("early", seconds(1.5)), ("end", seconds(2))]))
    assert nova.stt.calls == 0 and nova.executed == [] and nova.ui.states == []


def test_noise_is_dropped_before_any_whisper(monkeypatch):
    nova = make_nova(monkeypatch, [None], [])
    nova._wake_loop(FakeListener([("end", seconds(0.8))]))
    assert nova.stt.calls == 0


def test_just_the_name_then_the_command(monkeypatch):
    nova = make_nova(monkeypatch, ["Нова."], ["Нова.", "Открой хром."])
    nova._wake_loop(FakeListener([("end", seconds(0.7)), ("end", seconds(1.2))]))
    assert nova.ui.states == ["listening"] and nova.executed == ["Открой хром."]


def test_listener_reports_the_start_of_a_phrase_early():
    listener = UtteranceListener()
    assert listener.EARLY_S <= 1.5  # small enough to react while the user is talking


# Found by tools/voice_bench.py: Whisper glues a short command to the name, and
# "снова" is one letter away from "Нова".
def _is_command(rest):
    from nova.router import DISMISS
    return rest in {"пауза", "громкость 30", "отмена"} or bool(DISMISS.match(rest))


@pytest.mark.parametrize("heard, rest", [
    ("Новая пауза!", "пауза"),
    ("Новая громкость 30.", "громкость 30"),
    ("Новая отмена.", "отмена"),
])
def test_name_glued_to_a_command_still_wakes(heard, rest):
    from nova.text import split_name
    assert split_name(heard, "Нова", 80, is_command=_is_command) == rest


@pytest.mark.parametrize("heard", [
    "Снова забыл зарядку дома",
    "Новая версия игры вышла вчера",
    "Снова пауза",  # "снова" is never the name, even before a command
])
def test_words_built_around_the_name_do_not_wake(heard):
    from nova.text import split_name
    assert split_name(heard, "Нова", 80, is_command=_is_command) is None



def test_made_up_name_from_the_quick_model_shows_nothing(monkeypatch):
    """The tiny model "hears" Nova in "Она пошла": the main model disagrees, no chime, no panel."""
    nova = make_nova(monkeypatch, ["Nova."], ["Она пошла домой."])
    nova._wake_loop(FakeListener([("early", seconds(1.5))]))
    assert nova.ui.states == [] and nova.executed == []
