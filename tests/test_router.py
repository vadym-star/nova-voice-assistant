"""Fast-path routing: which skill and args each phrase maps to (skills are not executed)."""

import pytest

from nova import skills
from nova.router import Router
from nova.skills import REGISTRY, Context

skills.load_all()

CASES = [
    ("Нова, открой хром", "open", {"target": "хром"}),
    ("Ново! Открой телеграмм!", "open", {"target": "телеграмм"}),  # misheard name
    ("Эй, Нова, сделай громкость 50%.", "volume", {"level": "50"}),
    ("открой телеграм пожалуйста", "open", {"target": "телеграм"}),
    ("Запусти YouTube.com", "open", {"target": "youtube.com"}),
    ("открой папку загрузки", "open", {"target": "папку загрузки"}),
    ("закрой это окно", "close", {"target": "это окно"}),
    ("нажми на кнопку Сохранить", "click", {"target": "сохранить"}),
    ("нажми энтер", "click", {"target": "энтер"}),
    ("нажми клавишу ctrl+c", "press_key", {"key": "ctrl+c"}),
    ("напечатай привет как дела", "type_text", {"text": "привет как дела"}),
    ("прокрути вниз", "scroll", {"direction": "вниз"}),
    ("громкость 30", "volume", {"level": "30"}),
    ("сделай громче", "volume", {"level": "громче"}),
    ("включи звук", "volume", {"level": "включи звук"}),
    ("пауза", "media", {"action": "пауза"}),
    ("следующий трек", "media", {"action": "следующий трек"}),
    ("сделай скриншот", "screenshot", {}),
    ("найди рецепт борща", "search", {"query": "рецепт борща"}),
    ("найди на ютубе lofi music", "search", {"where": "ютубе", "query": "lofi music"}),
    ("который час", "time", {}),
    ("какое сегодня число", "date", {}),
    ("заблокируй компьютер", "lock", {}),
    ("выключи компьютер", "power", {"action": "выключи"}),
    # English
    ("Nova, open Telegram.", "open", {"target": "telegram"}),
    ("close this window", "close", {"target": "this window"}),
    ("click on Save", "click", {"target": "save"}),
    ("type hello world", "type_text", {"text": "hello world"}),
    ("set volume to 40", "volume", {"level": "40"}),
    ("louder", "volume", {"level": "louder"}),
    ("next track", "media", {"action": "next track"}),
    ("take a screenshot", "screenshot", {}),
    ("search on youtube for lofi beats", "search", {"where": "youtube", "query": "lofi beats"}),
    ("what time is it", "time", {}),
    ("lock the computer", "lock", {}),
    # 中文
    ("打开微信", "open", {"target": "微信"}),
    ("关闭这个窗口", "close", {"target": "这个窗口"}),
    ("点击保存", "click", {"target": "保存"}),
    ("音量调到30", "volume", {"level": "30"}),
    ("下一首", "media", {"action": "下一首"}),
    ("截图", "screenshot", {}),
    ("搜索天气预报", "search", {"query": "天气预报"}),
    ("现在几点", "time", {}),
    ("现在击点", "time", {}),  # 几 misheard as its homophone 击
]


@pytest.fixture
def calls(monkeypatch, tmp_path):
    monkeypatch.setattr("nova.router.MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr("nova.router.LEARNED_PATH", tmp_path / "l.json")
    log = []
    for s in REGISTRY:
        monkeypatch.setattr(s, "run", lambda ctx, _n=s.name, **kw: log.append((_n, kw)) or "")
    return log


def make_router():
    cfg = {"name": "Нова", "brain": "off", "language": "ru", "confirm_dangerous": True}
    return Router(cfg), Context(config=cfg, confirm=lambda q: True)


@pytest.mark.parametrize("phrase,skill,args", CASES)
def test_fast_path(calls, phrase, skill, args):
    router, ctx = make_router()
    router.handle(phrase, ctx)
    assert calls == [(skill, args)]


def test_macro_record_and_run(calls):
    router, ctx = make_router()
    router.handle("Запомни рабочий режим: открой телеграм, потом открой код", ctx)
    assert router.macros["рабочий режим"] == ["открой телеграм", "открой код"]
    router.handle("рабочий режим", ctx)
    assert calls == [("open", {"target": "телеграм"}), ("open", {"target": "код"})]


def test_macro_chinese(calls):
    router, ctx = make_router()
    router.handle("记住工作模式：打开微信，然后打开浏览器", ctx)
    assert router.macros["工作模式"] == ["打开微信", "打开浏览器"]


def test_macro_english(calls):
    router, ctx = make_router()
    router.handle("Remember movie night: open youtube, then volume 80", ctx)
    assert router.macros["movie night"] == ["open youtube", "volume 80"]


def test_macro_without_separator(calls):
    router, ctx = make_router()
    router.handle("Нова, запомни кино открой ютуб и потом громкость 80", ctx)
    assert router.macros["кино"] == ["открой ютуб", "громкость 80"]


class FakeBrain:
    """Answers like Claude would, once; a second call means learning failed."""

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def ask(self, text, history, **kw):
        self.calls += 1
        return self.answer


def test_learns_template_from_ai(calls):
    router, ctx = make_router()
    router.cfg["brain"] = "claude"
    router.brain = FakeBrain({
        "say": "", "actions": [{"skill": "search", "args": {"query": "погода в киеве"}}],
        "learn": {"template": "какая погода в {город}",
                  "actions": [{"skill": "search", "args": {"query": "погода в {город}"}}]},
    })
    router.handle("Какая погода в Киеве?", ctx)
    router.handle("какая погода в москве", ctx)
    assert router.brain.calls == 1  # second phrase ran from the learned template
    assert calls == [("search", {"query": "погода в киеве"}), ("search", {"query": "погода в москве"})]


def test_rejects_template_that_does_not_match(calls):
    router, ctx = make_router()
    router.cfg["brain"] = "claude"
    actions = [{"skill": "volume", "args": {"level": "30"}}]
    router.brain = FakeBrain({"say": "", "actions": actions,
                              "learn": {"template": "{x}", "actions": actions}})
    router.handle("сделай потише треть", ctx)
    assert "{x}" not in router.learned and router.learned["сделай потише треть"]["actions"] == actions


def test_rejects_template_with_converted_value(calls):
    router, ctx = make_router()
    router.cfg["brain"] = "claude"
    router.brain = FakeBrain({
        "say": "", "actions": [{"skill": "volume", "args": {"level": 33}}],
        "learn": {"template": "поставь громкость на {число}",
                  "actions": [{"skill": "volume", "args": {"level": "{число}"}}]},
    })
    router.handle("поставь громкость на треть", ctx)
    assert list(router.learned) == ["поставь громкость на треть"]


def test_compound_open_falls_back_to_ai(monkeypatch, tmp_path):
    monkeypatch.setattr("nova.router.MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr("nova.router.LEARNED_PATH", tmp_path / "l.json")
    from nova.skills import SkillError, get

    def fail(ctx, **kw):
        raise SkillError("Не могу найти программу")

    monkeypatch.setattr(get("open"), "run", fail)
    router, ctx = make_router()
    router.cfg["brain"] = "claude"
    router.brain = FakeBrain({"say": "Готово", "actions": []})
    assert router.handle("открой хром и найди погоду", ctx) == "Готово"


def test_unknown_without_brain(calls):
    router, ctx = make_router()
    assert router.handle("расскажи анекдот", ctx) == "Не понимаю команду"
    assert calls == []


def test_games_and_cases_are_found(monkeypatch):
    """"Запусти доту": Steam games aren't in the Start menu, and names come in any case."""
    from nova.skills import apps

    index = apps.AppIndex.__new__(apps.AppIndex)
    index._ready = __import__("threading").Event()
    index._ready.set()
    index.apps = {"dota 2": "steam://rungameid/570", "dota 2 addon brawlers": "x.lnk",
                  "fortnite": "com.epicgames.launcher://apps/Fortnite", "telegram": "tg"}
    assert index.find("доту")[0] == "dota 2"
    assert index.find("фортнайт")[0] == "fortnite"
    assert index.find("вова") is None


def _agent(monkeypatch, tmp_path, replies, run):
    monkeypatch.setattr("nova.router.MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr("nova.router.LEARNED_PATH", tmp_path / "l.json")
    r = Router({"name": "Нова", "brain": "claude", "language": "ru"})
    asked = iter(replies)
    progress = []
    monkeypatch.setattr(r.brain, "ask", lambda raw, hist, image_b64=None, progress=(), known=None: progress.append(list(progress))
                        or next(asked))
    monkeypatch.setattr(r, "_run_skill", run)
    return r, progress


def test_ai_tries_another_way_after_a_failed_step(monkeypatch, tmp_path):
    from nova.skills import SkillError

    def run(name, args, ctx, raise_errors=False):
        if args.get("target") == "дота":
            raise SkillError("не нахожу «дота»")
        return ""
    r, _ = _agent(monkeypatch, tmp_path, [
        {"say": "", "actions": [{"skill": "open", "args": {"target": "дота"}}]},
        {"say": "Запускаю Dota 2", "actions": [{"skill": "open", "args": {"target": "Dota 2"}}]},
    ], run)
    assert r.handle("хочу поиграть в доту", Context(config=r.cfg)) == "Запускаю Dota 2"


def test_no_second_try_after_the_user_said_no(monkeypatch, tmp_path):
    calls = []
    r, _ = _agent(monkeypatch, tmp_path, [
        {"say": "", "actions": [{"skill": "power", "args": {"action": "выключи"}}]},
        {"say": "again", "actions": [{"skill": "power", "args": {"action": "выключи"}}]},
    ], lambda name, args, ctx, raise_errors=False: calls.append(name) or "")
    ctx = Context(config=r.cfg, confirm=lambda q: False)
    monkeypatch.setattr("nova.router._needs_user_ok", lambda *a: True)
    r.handle("выключи всё", ctx)
    assert calls == []


@pytest.mark.parametrize("running, expected", [(True, 777), (False, 555)])
def test_remembered_app_is_preferred_only_while_it_runs(monkeypatch, running, expected):
    """"прими игру" -> Dota if Dota is open; otherwise whatever is in front (a browser game)."""
    from nova import screen
    from nova.skills import input as inp

    monkeypatch.setattr(screen, "main_window_of", lambda exe: 777 if running and exe == "dota2.exe" else 0)
    hwnd, background = inp._target_window(Context(config={}, foreground_hwnd=555), "dota2.exe")
    assert hwnd == expected and background == running


def test_an_app_the_user_named_must_be_open(monkeypatch):
    from nova import screen
    from nova.skills import SkillError
    from nova.skills import input as inp

    monkeypatch.setattr(screen, "top_windows", lambda: [])
    with pytest.raises(SkillError):
        inp._target_window(Context(config={}, foreground_hwnd=555), "телеграм")


def test_open_brings_up_a_running_game_instead_of_starting_it_again(monkeypatch):
    from nova import screen
    from nova.skills import apps

    url = "steam://rungameid/570"
    monkeypatch.setitem(apps._INSTALL_DIRS, url, __import__("pathlib").Path(r"C:\Steam\steamapps\common\dota 2 beta"))
    monkeypatch.setattr(screen, "windows_with_paths", lambda: [
        (11, "Opera", r"c:\program files\opera\opera.exe"),
        (42, "Dota 2", r"c:\steam\steamapps\common\dota 2 beta\game\bin\win64\dota2.exe")])
    shown, launched = [], []
    monkeypatch.setattr(screen, "bring_to_front", lambda hwnd: shown.append(hwnd) or True)
    monkeypatch.setattr(apps, "_launch_app_id", launched.append)
    apps._show_or_launch("dota 2", url)
    apps._show_or_launch("balatro", "steam://rungameid/2379780")
    assert shown == [42] and launched == ["steam://rungameid/2379780"]
