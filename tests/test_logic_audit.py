"""Logic bugs found by reading the code scenario by scenario, so they stay fixed."""

import re
import threading

import pytest

from nova import router as router_mod
from nova.skills import REGISTRY, Context, SkillError

# --- learned rules: similar is not the same ---------------------------------------


@pytest.mark.parametrize("said, rule, same", [
    ("примегру", "прими игру", True),          # Whisper glued two words
    ("включи вайфай", "выключи вайфай", False),  # looks alike, the opposite
    ("сделай яркость 60", "сделай яркость 50", False),
    ("открой steam", "закрой steam", False),
])
def test_learned_rule_needs_the_same_meaning(monkeypatch, tmp_path, said, rule, same):
    monkeypatch.setattr(router_mod, "MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr(router_mod, "LEARNED_PATH", tmp_path / "l.json")
    r = router_mod.Router({"name": "Нова", "brain": "off", "language": "ru"})
    r.learned[rule] = {"actions": [{"skill": "click", "args": {"target": "x"}}], "example": rule,
                       "shape": {}, "hits": 1, "last": 0}
    assert (r._match_learned(said) is not None) is same


def test_a_broken_learned_rule_falls_back_and_is_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(router_mod, "MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr(router_mod, "LEARNED_PATH", tmp_path / "l.json")
    r = router_mod.Router({"name": "Нова", "brain": "off", "language": "ru"})
    r.learned["прими игру"] = {"actions": [{"skill": "click", "args": {"target": "Принять"}}],
                               "example": "прими игру", "shape": {}, "hits": 5, "last": 0}

    def fail(name, args, ctx, raise_errors=False):
        raise SkillError("нет такой кнопки")
    monkeypatch.setattr(r, "_run_skill", fail)
    for _ in range(2):
        r.handle("прими игру", Context(config=r.cfg, confirm=lambda q: True))
    assert "прими игру" not in r.learned


def test_macro_depth_comes_back_down(monkeypatch, tmp_path):
    monkeypatch.setattr(router_mod, "MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr(router_mod, "LEARNED_PATH", tmp_path / "l.json")
    r = router_mod.Router({"name": "Нова", "brain": "off", "language": "ru"})
    r.macros = {"альфа": ["громкость 10"], "бета": ["громкость 20"], "гамма": ["громкость 30"],
                "все сразу": ["альфа", "бета", "гамма"]}
    ran = []
    monkeypatch.setattr(r, "_run_skill", lambda name, args, ctx, raise_errors=False: ran.append(args) or "")
    r.handle("все сразу", Context(config=r.cfg))
    assert len(ran) == 3


def test_a_fact_said_like_a_macro_is_remembered_as_a_fact(monkeypatch, tmp_path):
    monkeypatch.setattr(router_mod, "MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr(router_mod, "LEARNED_PATH", tmp_path / "l.json")
    r = router_mod.Router({"name": "Нова", "brain": "off", "language": "ru"})
    facts = []
    monkeypatch.setattr(r, "_run_skill", lambda name, args, ctx, raise_errors=False: facts.append((name, args)) or "")
    r.handle("Нова, запомни, Вова — это мой брат", Context(config=r.cfg))
    assert r.macros == {} and facts[0][0] == "remember_fact"
    r.handle("Нова, запомни рабочий режим: открой телеграм, потом громкость 30", Context(config=r.cfg))
    assert r.macros == {"рабочий режим": ["открой телеграм", "громкость 30"]}


# --- what the instant path catches -----------------------------------------------

def _caught(phrase):
    from nova import skills
    from nova.text import normalize

    skills.load_all()
    t = normalize(phrase)
    for s in REGISTRY:
        for p in s.patterns:
            if m := p.match(t):
                return s.name, m.groupdict()
    return None, {}


@pytest.mark.parametrize("phrase", ["назад", "дальше"])
def test_back_and_next_are_not_music_keys(phrase):
    assert _caught(phrase)[0] != "media"


@pytest.mark.parametrize("phrase", ["напиши код на питоне", "напиши письмо маме", "напиши мне стих про осень"])
def test_writing_tasks_never_open_telegram(phrase):
    from nova.skills import messaging

    name, args = _caught(phrase)
    if name == "send_message":
        with pytest.raises(SkillError):
            messaging.send_message(Context(config={}, extra={"raw": phrase}), **args)


def test_quoted_message_keeps_the_recipient():
    from nova.skills.messaging import _as_said

    ctx = Context(config={}, extra={"raw": "Напиши Вове, что «я опоздаю»"})
    assert _as_said(ctx, "вове", "я опоздаю") == ("вове", "я опоздаю")
    ctx = Context(config={}, extra={"raw": "напиши Вове что фильм «Дюна» классный"})
    assert _as_said(ctx, "вове", "фильм дюна классный")[0] == "вове"


def test_same_stem_of_another_person_is_not_sure(tmp_path):
    from nova.knowledge import Knowledge

    k = Knowledge(tmp_path / "k.json")
    k.add_alias("Иванов", "иванову")
    assert k.find_contact("Ивановой") == "Иванов"  # found by stem...
    assert not k.is_alias("Ивановой", "Иванов")    # ...but Nova must ask
    assert k.is_alias("Иванову", "Иванов")


# --- the AI's answer -----------------------------------------------------------------

@pytest.mark.parametrize("reply, actions, say", [
    ('{"say": "ok", "actions": null}', [], "ok"),
    ('Sure! {"say": "hi", "actions": [{"skill": "open", "args": {"target": "x"}}]} and {more}', 1, "hi"),
    ('{"say": "x", "actions": "open"}', [], "x"),
    ('just words', [], "just words"),
])
def test_ai_answers_in_odd_shapes(reply, actions, say):
    from nova.brain import _parse

    got = _parse(reply)
    assert got["say"] == say and (len(got["actions"]) == actions if isinstance(actions, int) else got["actions"] == actions)


# --- visual labels -----------------------------------------------------------------

@pytest.mark.parametrize("a, b, match", [
    ("accept", "accept match", True), ("ок", "закрыть окно", False), ("play", "display settings", False),
    ("x", "next", False), ("принять", "принять", True),
])
def test_visual_labels_match_only_real_names(a, b, match):
    from nova.vision import _labels_match

    assert _labels_match(a, b) is match


# --- windows -------------------------------------------------------------------------

def test_close_new_tab_is_ctrl_w(monkeypatch):
    import keyboard

    from nova.skills import apps

    sent = []
    monkeypatch.setattr(keyboard, "send", sent.append)
    apps.close_target(Context(config={}), "новую вкладку")
    assert sent == ["ctrl+w"]


@pytest.mark.parametrize("target", ["телеграм и напиши маме привет", "ютуб в хроме"])
def test_compound_open_goes_to_the_ai(target):
    from nova.skills import apps

    with pytest.raises(SkillError):
        apps.open_target(Context(config={}), target)


def test_too_short_names_are_not_guessed():
    from nova.skills import apps

    index = apps.AppIndex.__new__(apps.AppIndex)
    index._ready = threading.Event()
    index._ready.set()
    index.apps = {"excel": "x", "this pc": "y", "counter-strike 2": "z"}
    assert index.find("кс") is None and index.find("это") is None


# --- listening -----------------------------------------------------------------------

def test_held_key_repeats_are_not_new_presses(monkeypatch):
    from nova import app as app_mod

    nova = object.__new__(app_mod.Nova)
    nova.cfg = {"hotkey_mode": "toggle"}
    nova._listening, nova._stop_recording, nova._cancel = threading.Event(), threading.Event(), threading.Event()
    nova._work = threading.Lock()
    nova._last_press = 0.0
    started = []
    monkeypatch.setattr(app_mod.threading, "Thread", lambda target, daemon: type(
        "T", (), {"start": lambda s: started.append(1) or nova._listening.set()})())
    for _ in range(10):  # one long press: the first event and nine repeats
        nova._on_hotkey()
    assert started == [1] and not nova._stop_recording.is_set()


def test_esc_during_a_question_means_no(monkeypatch):
    from nova import app as app_mod

    nova = object.__new__(app_mod.Nova)
    nova._cancel = threading.Event()
    nova.ui = type("UI", (), {"quiet": True, "state": lambda s, *a, **k: None})()
    nova.stt = type("S", (), {"transcribe": lambda s, a: "да"})()

    def listen(max_s, wait_s=5.0):
        nova._cancel.set()  # Esc while answering
        return None
    nova._listen = listen
    assert nova.confirm("Отправить?") is False
    assert nova.ui.quiet is False  # the question was shown even in game mode
