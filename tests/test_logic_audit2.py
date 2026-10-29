"""Second round of scenario-by-scenario findings (messaging, AI, vision, lifecycle,
other languages, first run), so they stay fixed."""

import threading

import numpy as np
import pytest

from nova import router as router_mod
from nova.skills import Context, SkillError


def _router(monkeypatch, tmp_path, **cfg):
    monkeypatch.setattr(router_mod, "MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr(router_mod, "LEARNED_PATH", tmp_path / "l.json")
    return router_mod.Router({"name": "Нова", "brain": "claude", "language": "ru", **cfg})


# --- messaging ---------------------------------------------------------------------

def test_no_to_send_is_final_and_the_ai_is_not_asked(monkeypatch, tmp_path):
    r = _router(monkeypatch, tmp_path)
    asked_ai = []
    monkeypatch.setattr(r, "_ask_brain", lambda *a, **k: asked_ai.append(1) or "sent anyway")

    def send(name, args, ctx, raise_errors=False):
        ctx.confirm("Отправить?")
        raise SkillError(router_mod.tr("cancelled"))
    monkeypatch.setattr(r, "_run_skill", send)
    r.handle("напиши Вове привет", Context(config=r.cfg, confirm=lambda q: False))
    assert asked_ai == []


@pytest.mark.parametrize("raw, to, text, expected", [
    ("Напиши Вове, что мне плохо", "вове", "плохо", "мне плохо"),
    ("напиши Вове а ты где", "вове", "ты где", "а ты где"),
    ("передай Ване да", "ване", "да", "да"),
    ("напиши Вове что «завтра» не приду", "вове", "завтра не приду", "завтра не приду"),
])
def test_message_text_is_what_was_said(raw, to, text, expected):
    from nova.skills.messaging import _as_said

    assert _as_said(Context(config={}, extra={"raw": raw}), to, text)[1] == expected


def test_chinese_message_after_a_comma():
    from nova import skills
    from nova.skills.messaging import send_message  # noqa: F401 - registers the skill
    from nova.text import normalize

    skills.load_all()
    pattern = next(p for s in skills.REGISTRY if s.name == "send_message" for p in s.patterns if "给" in p.pattern)
    m = pattern.match(normalize("给妈妈发消息，说我晚点到"))
    assert m and m["to"] == "妈妈" and m["text"] == "我晚点到"


def test_chat_title_with_at_sign():
    from nova.skills import messaging

    class U:
        @staticmethod
        def GetWindowTextW(hwnd, buf, n):
            buf.value = "Work @ Home @ Вадим (5)"
    real = messaging.user32
    messaging.user32 = U
    try:
        assert messaging.open_chat_title(1) == "Work @ Home"
    finally:
        messaging.user32 = real


# --- the AI ------------------------------------------------------------------------

def test_a_task_the_ai_gave_up_on_is_not_learned(monkeypatch, tmp_path):
    r = _router(monkeypatch, tmp_path)
    replies = iter([
        {"say": "", "actions": [{"skill": "open", "args": {"target": "steam"}},
                                {"skill": "click", "args": {"target": "Dota 2"}}], "continue": True},
        {"say": "Не получилось", "actions": []},
    ])
    monkeypatch.setattr(r.brain, "ask", lambda *a, **k: next(replies))

    def run(name, args, ctx, raise_errors=False):
        if name == "click":
            raise SkillError("нет кнопки")
        return ""
    monkeypatch.setattr(r, "_run_skill", run)
    r.handle("хочу в доту", Context(config=r.cfg, confirm=lambda q: True))
    assert r.learned == {}


def test_after_a_confirm_the_send_click_is_not_asked_again():
    from nova.router import _needs_user_ok

    ctx = Context(config={})
    before = [{"skill": "confirm", "args": {"question": "Отправить письмо?"}}]
    assert not _needs_user_ok("click", {"target": "Send"}, ctx, before)
    assert _needs_user_ok("click", {"target": "Send"}, ctx, [])


@pytest.mark.parametrize("error, key", [
    ("Not logged in · Please run /login", "claude_login"),
    ("Claude AI usage limit reached|1761234567", "ai_limit"),
    ("API Error: Connection error.", "no_internet"),
    ("error: unknown option '--safe-mode'", "claude_outdated"),
])
def test_claude_errors_are_explained_in_the_users_language(monkeypatch, error, key):
    import json

    from nova import brain
    from nova.i18n import t

    b = brain.Brain({"brain": "claude", "language": "ru"})
    monkeypatch.setattr(b, "find_claude", lambda: "claude.exe")
    monkeypatch.setattr(brain._WARM, "take", lambda *a, **k: None)
    monkeypatch.setattr(b, "_wait", lambda proc, msg: (json.dumps({"type": "result", "is_error": True,
                                                                     "result": error}), ""))
    monkeypatch.setattr(brain._WARM, "close", lambda: None)
    with pytest.raises(brain.BrainError) as e:
        b._claude("hi", "sys")
    assert str(e.value) == t(key)


def test_esc_stops_a_running_ai_call():
    from nova import brain

    class SlowProc:
        killed = False

        def communicate(self, message):
            threading.Event().wait(5)
            return "", ""

        def kill(self):
            SlowProc.killed = True

    b = brain.Brain({"brain": "claude", "language": "ru"})
    b.cancel = threading.Event()
    threading.Timer(0.3, b.cancel.set).start()
    with pytest.raises(brain.BrainError):
        b._wait(SlowProc(), "hi")
    assert SlowProc.killed


# --- vision -----------------------------------------------------------------------

def _shot(img):
    from nova.screen import Shot

    return Shot(img, 0, 0)


def _ui(width=800, height=600):
    rng = np.random.default_rng(3)
    img = (rng.random((height, width, 3)) * 50 + 20).astype(np.uint8)
    img[100:140, 100:260] = [40, 150, 60]  # a "Save" button
    img[110:130, 120:240] = 230
    img[300:340, 100:300] = [150, 60, 40]  # a "Save as" button, elsewhere
    img[310:330, 120:280] = 200
    return img


def test_exact_label_wins_over_a_longer_one(tmp_path):
    from nova.vision import VisualMemory

    mem = VisualMemory(tmp_path / "m.db")
    shot = _shot(_ui())
    mem.learn("app.exe", ["сохранить как"], shot, 200, 320, 200, 40)
    mem.learn("app.exe", ["сохранить"], shot, 180, 120, 160, 40)
    hit = mem.find("app.exe", "сохранить", shot)
    assert hit and abs(hit["y"] - 120) < 10


def test_a_plain_patch_is_never_remembered(tmp_path):
    from nova.vision import VisualMemory

    mem = VisualMemory(tmp_path / "m.db")
    flat = np.full((600, 800, 3), 128, np.uint8)
    assert mem.learn("app.exe", ["кнопка"], _shot(flat), 400, 300, 100, 40) == 0
    assert mem.find("app.exe", "кнопка", _shot(flat)) is None


def test_a_wider_window_still_finds_the_button(tmp_path):
    from nova.vision import VisualMemory

    mem = VisualMemory(tmp_path / "m.db")
    mem.learn("app.exe", ["сохранить"], _shot(_ui(800)), 180, 120, 160, 40)
    wide = np.zeros((600, 1000, 3), np.uint8)
    wide[:, :800] = _ui(800)
    hit = mem.find("app.exe", "сохранить", _shot(wide))
    assert hit and abs(hit["x"] - 180) < 10


def test_similar_labels_are_not_evicted_as_variants(tmp_path):
    from nova.vision import VisualMemory

    mem = VisualMemory(tmp_path / "m.db")
    shot = _shot(_ui())
    mem.learn("app.exe", ["audio settings"], shot, 200, 320, 200, 40)
    mem.learn("app.exe", ["settings"], shot, 180, 120, 160, 40)
    assert "audio settings" in mem.labels("app.exe")


# --- other languages ------------------------------------------------------------------

@pytest.mark.parametrize("said, rest", [
    ("诺娃打开微信", "打开微信"), ("诺娃，打开微信", "打开微信"), ("嘿，诺娃，几点了", "几点了"), ("諾娃打开微信", "打开微信"),
])
def test_chinese_name_without_spaces(said, rest):
    from nova.text import split_name

    assert split_name(said, "诺娃") == rest


def test_chinese_name_is_stripped_from_the_raw_phrase():
    from nova.text import strip_name_raw

    assert strip_name_raw("诺娃，记住工作模式：打开微信", "诺娃") == "记住工作模式：打开微信"


@pytest.mark.parametrize("answer, yes", [
    ("没问题", True), ("no problem", True), ("好啊", True), ("行", True), ("of course", True),
    ("不要", False), ("不用了", False), ("nope", False), ("no thanks", False),
])
def test_yes_and_no_in_english_and_chinese(answer, yes):
    from nova.i18n import is_yes

    assert is_yes(answer) is yes


@pytest.mark.parametrize("phrase, skill", [
    ("volume up", "volume"), ("turn the volume down", "volume"), ("音量调到三十", "volume"),
    ("what's the date", "date"), ("lock my computer", "lock"), ("can you open spotify", "open"),
    ("请打开微信", "open"), ("pause the music", "media"),
])
def test_everyday_phrases_take_the_instant_path(phrase, skill):
    from nova import skills
    from nova.text import normalize

    skills.load_all()
    t = normalize(phrase)
    caught = next((s.name for s in skills.REGISTRY for p in s.patterns if p.match(t)), None)
    assert caught == skill


def test_chinese_numbers():
    from nova.skills.system import _chinese_number

    assert [_chinese_number(x) for x in ("三十", "三十五", "十", "一百", "五", "微信")] == [30, 35, 10, 100, 5, None]


def test_click_save_looks_for_the_button_first(monkeypatch):
    import uiautomation as auto

    from nova.skills import input as inp

    pressed, clicked = [], []
    monkeypatch.setattr(inp, "press_key", lambda ctx, key, app=None: pressed.append(key))
    monkeypatch.setattr(inp, "_target_window", lambda ctx, app: (5, False))
    monkeypatch.setattr(auto, "ControlFromHandle", lambda hwnd: object())
    monkeypatch.setattr(auto, "PaneControl", lambda **kw: object())
    monkeypatch.setattr(inp, "_find_element", lambda root, name, **kw: "button" if name == "save" else None)
    monkeypatch.setattr(inp, "_activate", lambda element, allow_mouse=True: clicked.append(element) or True)
    monkeypatch.setattr(inp, "_snap_back", lambda ctx, background: None)
    inp.click(Context(config={}), "save")            # the Save button, not Ctrl+S
    inp.click(Context(config={}), "the enter key")   # a key, said as a key
    assert clicked == ["button"] and pressed == ["enter"]


def test_dismissals_in_chinese():
    from nova.router import DISMISS

    assert DISMISS.match("不用了") and DISMISS.match("算了吧")


# --- first run ------------------------------------------------------------------------

def test_first_start_follows_the_windows_language(tmp_path, monkeypatch):
    from nova import config

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "_windows_language", lambda: "en")
    cfg = config.load()
    assert cfg["language"] == "en" and cfg["name"] == "Nova"


@pytest.mark.parametrize("said, name, wakes", [
    ("Она пошла", "Нова", False), ("Ова, открой", "Нова", False), ("Она", "Nova", False),
    ("Ново, открой", "Nova", True), ("Нову", "Nova", True), ("Нова, открой", "Nova", True),
    ("Nova, open", "Нова", True),
])
def test_words_that_only_sound_close_dont_wake(said, name, wakes):
    from nova.text import split_name

    assert (split_name(said, name, 80) is not None) is wakes


@pytest.mark.parametrize("phrase, query", [
    ("Включи видео мистер биста, игра в кальмара", "мистер биста игра в кальмара"),
    ("поставь песню imagine dragons", "imagine dragons"),
    ("включи lofi на ютубе", "lofi"),
    ("play the song believer", "believer"),
])
def test_play_a_video_means_play_not_search(phrase, query):
    from nova import skills
    from nova.text import normalize

    skills.load_all()
    t = normalize(phrase)
    hit = next(((s.name, m["query"]) for s in skills.REGISTRY for p in s.patterns if (m := p.match(t))), None)
    assert hit == ("play_video", query)


@pytest.mark.parametrize("phrase", ["включи музыку", "включи телеграм", "play dota"])
def test_other_include_phrases_are_not_videos(phrase):
    from nova import skills
    from nova.text import normalize

    skills.load_all()
    t = normalize(phrase)
    assert all(not p.match(t) for s in skills.REGISTRY if s.name == "play_video" for p in s.patterns)


def test_welcome_shows_only_for_new_users(tmp_path, monkeypatch):
    import json

    from nova import config

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "_windows_language", lambda: "ru")
    assert config.load()["onboarded"] is False              # brand new: the welcome shows
    (tmp_path / "config.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")
    assert config.load()["onboarded"] is True               # an existing user from before: no welcome


@pytest.mark.parametrize("said", ["Норм", "Норм, давай", "Нормально", "Нона", "Нива", "Ноа", "Она", "Ова"])
def test_main_model_wants_the_name_itself(said):
    from nova.app import NAME_SURE
    from nova.text import split_name

    assert split_name(said, "Nova", NAME_SURE) is None and split_name(said, "Нова", NAME_SURE) is None


@pytest.mark.parametrize("said", ["Нова", "Ново, открой", "Нову", "Nova, open", "NOVA", "Эй, Нова"])
def test_main_model_still_hears_the_real_name(said):
    from nova.app import NAME_SURE
    from nova.text import split_name

    assert split_name(said, "Nova", NAME_SURE) is not None
