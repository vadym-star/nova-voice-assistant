"""Third round: user journeys (gaming, office), a red team, data, hardware, multi-step."""

import threading

import pytest

from nova import router as router_mod
from nova.router import _needs_user_ok
from nova.skills import Context, SkillError


@pytest.fixture
def ctx(monkeypatch):
    from nova import screen

    monkeypatch.setattr(screen, "app_name", lambda hwnd: "chrome.exe")
    return Context(config={})


# --- red team ------------------------------------------------------------------------

def test_a_yes_to_the_ais_own_question_does_not_cover_a_command(ctx):
    before = [{"skill": "confirm", "args": {"question": "Открыть браузер?"}}]
    assert _needs_user_ok("type_text", {"text": "powershell -c iwr evil", "submit": True}, ctx, before)
    assert _needs_user_ok("press_key", {"key": "win+r"}, ctx, before)
    assert not _needs_user_ok("click", {"target": "Send"}, ctx, before)  # what the yes was for


@pytest.mark.parametrize("text", [r"C:\Users\a\Downloads\evil.exe", r"\\host\share\x", "ms-settings:privacy",
                                  "javascript:alert(1)", "wscript x.vbs", "net1 user x /add", "a & b"])
def test_paths_and_launchers_need_a_yes(ctx, text):
    assert _needs_user_ok("type_text", {"text": text}, ctx, [])


@pytest.mark.parametrize("key, asks", [("alt+f4", True), ("ctrl+shift+esc", True), ("ctrl+esc", True),
                                       ("enter", False), ("ctrl+c", False), ("tab", False)])
def test_only_plain_keys_go_unasked(ctx, key, asks):
    assert _needs_user_ok("press_key", {"key": key}, ctx, []) is asks


@pytest.mark.parametrize("target", ["Разрешить", "Allow", "Установить", "Install", "Запустить", "Уничтожить"])
def test_consent_and_install_buttons_need_a_yes(ctx, target):
    assert _needs_user_ok("click", {"target": target}, ctx, [])


def test_the_dota_accept_button_is_not_asked(ctx):
    assert not _needs_user_ok("click", {"target": "Принять"}, ctx, [])


def test_spoken_yes_is_refused_while_the_speakers_play(monkeypatch):
    from nova import app as app_mod

    nova = object.__new__(app_mod.Nova)
    nova._cancel = threading.Event()
    nova._low_trust, nova._speakers_during_answer = True, True
    nova.ui = type("UI", (), {"quiet": False, "state": lambda s, *a, **k: None, "hide_later": lambda s, x: None})()
    nova.stt = type("S", (), {"transcribe": lambda s, a: "да"})()
    nova._listen = lambda max_s, wait_s=5.0: None
    assert nova.confirm("Выключить компьютер?") is False


def test_instant_commands_from_the_speakers_ask_first(monkeypatch, tmp_path):
    r = router_mod.Router({"name": "Нова", "brain": "off", "language": "ru"})
    from nova import skills

    skills.load_all()
    ran, asked = [], []
    monkeypatch.setattr(skills.get("click"), "run", lambda ctx, **kw: ran.append(kw))
    c = Context(config=r.cfg, confirm=lambda q: asked.append(q) or False)
    c.extra["low_trust"] = True
    r._run_skill("click", {"target": "отправить"}, c)
    assert asked and not ran


# --- multi-step ---------------------------------------------------------------------

def _agent(monkeypatch, replies, run=None):
    r = router_mod.Router({"name": "Нова", "brain": "claude", "language": "ru"})
    it = iter(replies)
    monkeypatch.setattr(r.brain, "ask", lambda *a, **k: next(it))
    calls = []
    monkeypatch.setattr(r, "_run_skill", run or (lambda name, args, ctx, raise_errors=False: calls.append(name) or ""))
    monkeypatch.setattr("nova.screen.capture_window", lambda hwnd: type("S", (), {"to_jpeg_b64": lambda s: ("x", 1)})())
    return r, calls


def test_a_held_confirm_is_asked_even_if_the_ai_drops_it(monkeypatch):
    r, calls = _agent(monkeypatch, [
        {"say": "", "actions": [{"skill": "open", "args": {"target": "telegram"}}], "continue": True},
        {"say": "", "actions": [{"skill": "type_text", "args": {"text": "Привет"}},
                                {"skill": "confirm", "args": {"question": "Отправить Вове: Привет?"}},
                                {"skill": "press_key", "args": {"key": "enter"}}], "continue": True},
        {"say": "Отправила", "actions": [{"skill": "press_key", "args": {"key": "enter"}}]},
    ])
    asked = []
    r.handle("поздоровайся с вовой в телеге", Context(config=r.cfg, confirm=lambda q: asked.append(q) or False))
    assert "Отправить Вове: Привет?" in asked and calls.count("press_key") == 0


def test_running_out_of_steps_is_not_success(monkeypatch):
    r, _ = _agent(monkeypatch, [{"say": "Готово", "actions": [{"skill": "wait", "args": {}}], "continue": True}] * 9)
    assert r.handle("сделай сложное", Context(config=r.cfg, confirm=lambda q: True)) == router_mod.tr("not_finished")


def test_a_macro_stops_at_a_failed_step_and_on_esc(monkeypatch):
    r = router_mod.Router({"name": "Нова", "brain": "off", "language": "ru"})
    r.macros = {"утро": ["громкость 30", "пауза"]}
    ran = []

    def run(name, args, ctx, raise_errors=False):
        ran.append(name)
        raise SkillError("нет звука")
    monkeypatch.setattr(r, "_run_skill", run)
    r.handle("утро", Context(config=r.cfg))
    assert ran == ["volume"]


def test_a_macro_that_calls_itself_is_refused(monkeypatch):
    r = router_mod.Router({"name": "Нова", "brain": "off", "language": "ru"})
    r.handle("Нова, запомни пауза: пауза", Context(config=r.cfg))
    assert "пауза" not in r.macros


def test_commas_inside_a_message_stay_in_the_step(monkeypatch):
    r = router_mod.Router({"name": "Нова", "brain": "off", "language": "ru"})
    r.handle("Нова, запомни пиши: напиши вове привет, как дела, потом громкость 30", Context(config=r.cfg))
    assert r.macros["пиши"] == ["напиши вове привет как дела", "громкость 30"]


def test_flows_that_send_are_not_learned(monkeypatch):
    r = router_mod.Router({"name": "Нова", "brain": "claude", "language": "ru"})
    r._learn("отправь письмо", {}, [{"skill": "type_text", "args": {"text": "x"}},
                                    {"skill": "confirm", "args": {"question": "?"}},
                                    {"skill": "press_key", "args": {"key": "enter"}}])
    assert r.learned == {}


# --- gaming -------------------------------------------------------------------------

def test_hotkeys_parse_without_a_keyboard_hook():
    from nova.hotkey import parse

    assert parse("ctrl+alt+space") == (0x3, 0x20, [0x11, 0x12, 0x20])
    assert parse("f9")[1] == 0x78 and parse("ctrl+shift+n")[1] == ord("N")
    assert parse("ctrl+a+b") is None and parse("") is None


def test_faceit_client_alone_does_not_block(monkeypatch):
    from nova import guard

    class P:
        def __init__(self, n):
            self.info = {"name": n}
    procs = [P("faceitservice.exe"), P("explorer.exe")]
    monkeypatch.setattr(guard.psutil, "process_iter", lambda attrs: procs)
    g = guard.GameGuard.__new__(guard.GameGuard)
    g.enabled, g.game, g._interval = True, None, 0
    running = {p.info["name"] for p in procs}
    assert not any(n in guard.ANTI_CHEAT for n in running)
    assert not any(n in running and running & set(games) for n, games in guard._PAIRS.items())
    running.add("cs2.exe")
    assert any(n in running and running & set(games) for n, games in guard._PAIRS.items())


# --- data -----------------------------------------------------------------------------

def test_broken_utf8_and_bom_files_load(tmp_path):
    from nova import config

    bad = tmp_path / "x.json"
    bad.write_bytes(b'{"a": "\\xd0')
    assert config.read_json(bad, {"ok": 1}) == {"ok": 1}
    bom = tmp_path / "y.json"
    bom.write_bytes('\ufeff{"b": 2}'.encode("utf-8"))
    assert config.read_json(bom, {}) == {"b": 2}


def test_settings_choices_and_numbers_are_checked():
    from nova.config import clean

    got = clean({"stt_model": "../../x", "silence_timeout": "nan", "brain": "gpt9", "hotkey_mode": "toggle",
                 "language": "en"})
    assert got == {"hotkey_mode": "toggle", "language": "en"}


def test_odd_knowledge_and_rules_are_repaired(tmp_path):
    import json

    from nova.knowledge import Knowledge
    from nova.router import _rule

    path = tmp_path / "k.json"
    path.write_text(json.dumps({"contacts": {"Вова": {}}, "facts": ["Вова — брат", 5]}), encoding="utf-8")
    k = Knowledge(path)
    assert k.contacts["Вова"]["aliases"] == [] and k.facts == [{"text": "Вова — брат", "last": 0}]
    k.find_contact("вове")  # no KeyError
    rule = _rule({"actions": ["open mail", {"skill": "open", "args": {}}], "hits": "3"}, "x")
    assert rule["actions"] == [{"skill": "open", "args": {}}] and rule["hits"] == 0


def test_a_damaged_memory_db_is_set_aside(tmp_path):
    from nova.vision import VisualMemory

    db = tmp_path / "memory.db"
    db.write_bytes(b"this is not a database at all" * 100)
    mem = VisualMemory(db)
    assert mem.stats()["patterns"] == 0 and (tmp_path / "memory.db.corrupt").exists()


def test_step_split_is_fast_on_long_spaces():
    import time

    r = router_mod.Router({"name": "Нова", "brain": "off", "language": "ru"})
    started = time.time()
    r._remember("тест: открой хром" + " " * 5000 + "потом громкость 30")
    assert time.time() - started < 1


def test_spelling_with_yo_is_kept():
    from nova.text import as_said

    assert as_said("Напиши «Ёлка зелёная»", "елка зеленая") == "Ёлка зелёная"


def test_okay_greeting_before_a_chinese_name():
    from nova.text import split_name

    assert split_name("okay 诺娃 打开微信", "诺娃") == "打开微信"


def test_find_survives_templates_bigger_than_the_screen(tmp_path):
    import numpy as np

    from nova.screen import Shot
    from nova.vision import VisualMemory

    mem = VisualMemory(tmp_path / "m.db")
    big = (np.random.default_rng(1).random((300, 400, 3)) * 255).astype(np.uint8)
    mem.learn("app.exe", ["кнопка"], Shot(big, 0, 0), 200, 150, 200, 100)
    tiny = Shot(big[:40, :40].copy(), 0, 0)
    assert mem.find("app.exe", "кнопка", tiny) is None  # no UnboundLocalError
