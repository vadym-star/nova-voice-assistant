"""Regression tests for the issues the code audit found."""

import pytest

from nova import router as router_mod
from nova import skills
from nova.i18n import is_yes
from nova.knowledge import Knowledge
from nova.router import Router
from nova.skills import REGISTRY, Context
from nova.text import as_said, strip_name

skills.load_all()


@pytest.fixture
def calls(monkeypatch, tmp_path):
    monkeypatch.setattr(router_mod, "MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr(router_mod, "LEARNED_PATH", tmp_path / "l.json")
    log = []
    for s in REGISTRY:
        monkeypatch.setattr(s, "run", lambda ctx, _n=s.name, **kw: log.append((_n, kw)) or "")
    return log


def make(brain="off"):
    cfg = {"name": "Нова", "brain": brain, "language": "ru", "confirm_dangerous": True}
    return Router(cfg), Context(config=cfg, confirm=lambda q: False)


@pytest.mark.parametrize("answer,yes", [
    ("да", True), ("Да, давай", True), ("отправляй", True), ("是的", True), ("好", True),
    ("да нет, не надо", False), ("No, okay?", False), ("不是", False), ("不对", False), ("不好", False), ("", False),
])
def test_yes_no(answer, yes):
    assert is_yes(answer) is yes


@pytest.mark.parametrize("phrase,skill", [
    ("закрой вкладку", "close"),           # ...and close() itself turns it into Ctrl+W
    ("посмотри погоду в киеве", None),      # a search, never a screenshot to the cloud
    ("посмотри сюда", "look"),
    ("look for a pizza place", None),
    ("write an email to mom", None),        # a task for the AI, not typing into the active window
    ("напиши вове привет как дела", "send_message"),
    ("напиши текст привет", "type_text"),
    ("напиши в избранное купить молоко", "send_message"),
])
def test_routing(calls, phrase, skill):
    router, ctx = make()
    router.handle(phrase, ctx)
    assert (calls[0][0] if calls else None) == skill


def test_saved_messages_parse(calls):
    router, ctx = make()
    router.handle("Нова, напиши в избранное купить молоко", ctx)
    assert calls == [("send_message", {"to": "в избранное", "text": "купить молоко"})]


def test_new_tab_keeps_its_word():
    assert strip_name("новая вкладка", "Нова") == "новая вкладка"
    assert strip_name("Ново, открой", "Нова") == "открой"


def test_typing_keeps_case_and_punctuation():
    assert as_said("напечатай Привет, мир!", "привет мир") == "Привет, мир!"


def test_relationship_fact_is_not_a_chat(tmp_path):
    k = Knowledge(tmp_path / "k.json")
    k.add_fact("Вова — мой брат")
    assert k.find_contact("Вове") is None
    k.add_fact("Вова — это Вовчикинатор")
    assert k.find_contact("Вове") == "Вовчикинатор"


def test_ai_win_r_needs_a_human_yes(calls):
    router, ctx = make()
    asked = []
    ctx.confirm = lambda q: asked.append(q) or False
    reply, ok, n = router._run_steps([{"skill": "press_key", "args": {"key": "win+r"}},
                                      {"skill": "type_text", "args": {"text": "powershell"}}], ctx, [], from_ai=True)
    assert not ok and n == 0 and asked and calls == []


def test_ai_submit_after_screenshot_needs_a_yes(calls):
    router, ctx = make()
    ctx.extra["saw_screen"] = True
    ctx.confirm = lambda q: False
    _, ok, n = router._run_steps([{"skill": "type_text", "args": {"text": "hi"}},
                                  {"skill": "press_key", "args": {"key": "enter"}}], ctx, [], from_ai=True)
    assert not ok and n == 1 and calls == [("type_text", {"text": "hi"})]


def test_crashed_step_stops_the_chain(monkeypatch, calls):
    def boom(ctx, **kw):
        raise KeyError("x")

    monkeypatch.setattr(skills.get("click"), "run", boom)
    router, ctx = make()
    _, ok, n = router._run_steps([{"skill": "click", "args": {"target": "ok"}},
                                  {"skill": "type_text", "args": {"text": "secret"}}], ctx, [])
    assert not ok and n == 0 and calls == []


def test_fuzzy_replay_never_changes_recipient(calls):
    router, ctx = make()
    router._learn("напиши маме что я опоздаю", {}, [{"skill": "send_message",
                                                     "args": {"to": "маме", "text": "я опоздаю"}}])
    assert router._match_learned("напиши папе что я опоздаю") is None


def test_self_calling_macro_stops(calls):
    router, ctx = make()
    router.handle("Запомни музыку: включи музыку", ctx)
    router.handle("музыку", ctx)  # must return, not recurse forever


def test_ai_confirm_before_send_is_dropped():
    acts = [{"skill": "confirm", "args": {"question": "?"}}, {"skill": "send_message", "args": {"to": "x"}}]
    assert router_mod._drop_redundant_confirms(acts) == acts[1:]


@pytest.mark.parametrize("phrase", ["отмена", "Нова, стоп", "не надо", "Cancel.", "never mind", "取消"])
def test_dismiss_words_stop_everything(calls, phrase):
    router, ctx = make(brain="claude")
    assert router.handle(phrase, ctx) == "" and ctx.extra.get("dismissed") and calls == []