"""Learning without bloat: rules merge, memory stays bounded, corrections stick."""

import numpy as np
import pytest
from PIL import Image, ImageDraw

from nova import router as router_mod
from nova import skills
from nova.router import Router
from nova.screen import Shot
from nova.skills import Context
from nova.vision import VisualMemory

skills.load_all()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(router_mod, "MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr(router_mod, "LEARNED_PATH", tmp_path / "l.json")


def make_router(memory=None):
    cfg = {"name": "Нова", "brain": "claude", "language": "ru", "confirm_dangerous": True, "app_mapping": False}
    return Router(cfg, memory), Context(config=cfg, confirm=lambda q: True)


def screen_with_button(x, y, text="ACCEPT", size=(1280, 720)):
    """Fake game screen: noisy background, one green button at (x, y)."""
    rng = np.random.default_rng(1)
    bg = (rng.random((size[1], size[0], 3)) * 60 + 20).astype(np.uint8)
    img = Image.fromarray(bg)
    d = ImageDraw.Draw(img)
    d.rectangle((x - 75, y - 25, x + 75, y + 25), fill=(40, 150, 60), outline=(200, 255, 200))
    d.text((x - 25, y - 6), text, fill=(255, 255, 255))
    return Shot(np.asarray(img)[:, :, ::-1].copy(), 0, 0)


# --- text rules ---------------------------------------------------------------

def test_broader_rule_replaces_narrow_ones():
    router, _ = make_router()
    router._learn("открой хром", {}, [{"skill": "open", "args": {"target": "хром"}}])
    router.learned["открой хром"]["hits"] = 5
    router._learn("открой телеграм", {"learn": {"template": "открой {app}",
                                               "actions": [{"skill": "open", "args": {"target": "{app}"}}]}},
                  [{"skill": "open", "args": {"target": "телеграм"}}])
    assert list(router.learned) == ["открой {app}"]
    assert router.learned["открой {app}"]["hits"] == 5  # usage history carried over


def test_rules_are_capped(monkeypatch):
    monkeypatch.setattr(router_mod, "MAX_RULES", 3)
    router, _ = make_router()
    for i in range(6):
        router._learn(f"сделай штуку номер {i}", {}, [{"skill": "time", "args": {}}])
    assert len(router.learned) == 3


def test_wrong_rule_is_forgotten(monkeypatch):
    router, ctx = make_router()
    monkeypatch.setattr(skills.get("time"), "run", lambda ctx: "")
    router._learn("сколько натикало", {}, [{"skill": "time", "args": {}}])
    router.handle("сколько натикало", ctx)
    router.handle("не то", ctx)
    assert "сколько натикало" not in router.learned


# --- visual memory --------------------------------------------------------------

def test_memory_finds_moved_button_and_does_not_duplicate(tmp_path):
    mem = VisualMemory(tmp_path / "v.db")
    first = screen_with_button(555, 425)
    mem.learn("dota2.exe", ["принять", "accept"], first, 555, 425, 150, 50)
    moved = screen_with_button(900, 300)
    hit = mem.find("dota2.exe", "Принять", moved)
    assert hit and abs(hit["x"] - 900) <= 3 and abs(hit["y"] - 300) <= 3
    assert mem.find("dota2.exe", "accept", moved)  # either label works
    assert mem.find("chrome.exe", "принять", moved) is None  # memory is per app
    mem.learn("dota2.exe", ["принять"], moved, 900, 300, 150, 50)  # same button again
    assert mem.stats()["patterns"] == 1
    assert mem.stats()["bytes"] < 30_000


def test_memory_caps_variants(tmp_path, monkeypatch):
    import nova.vision as v

    monkeypatch.setattr(v, "SAME_THRESHOLD", 1.01)  # treat every crop as a new look
    mem = VisualMemory(tmp_path / "v.db")
    for i in range(6):
        mem.learn("game.exe", ["play"], screen_with_button(300 + i * 40, 300, text=f"PLAY{i}"),
                  300 + i * 40, 300, 150, 50)
    assert mem.stats()["patterns"] == v.MAX_VARIANTS


def test_nothing_found_when_button_absent(tmp_path):
    mem = VisualMemory(tmp_path / "v.db")
    mem.learn("dota2.exe", ["принять"], screen_with_button(555, 425), 555, 425, 150, 50)
    empty = screen_with_button(-500, -500)  # button off screen
    assert mem.find("dota2.exe", "принять", empty) is None


# --- the whole "Нова, нажми принять" flow -----------------------------------------

class FakeVisionBrain:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def ask_image(self, prompt, b64, system):
        self.calls.append(prompt)
        return self.answers.pop(0)

    def ask(self, text, history):
        raise AssertionError("text AI should not be needed")


def test_visual_click_learns_then_skips_ai_then_corrects(tmp_path, monkeypatch):
    import nova.screen as scr
    import nova.skills.vision as sv

    shots = {"current": screen_with_button(555, 425)}
    clicks = []
    monkeypatch.setattr(scr, "capture_window", lambda hwnd: shots["current"])
    monkeypatch.setattr(scr, "app_name", lambda hwnd: "dota2.exe")
    monkeypatch.setattr(scr, "click_at", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(sv.screen, "capture_window", scr.capture_window)
    # UI Automation sees nothing inside a game.
    monkeypatch.setattr("nova.skills.input._find_element", lambda root, name: None)
    monkeypatch.setattr("uiautomation.ControlFromHandle", lambda h: None)
    monkeypatch.setattr("uiautomation.PaneControl", lambda **kw: None)

    memory = VisualMemory(tmp_path / "v.db")
    router, ctx = make_router(memory)
    ctx.foreground_hwnd = 1
    # scale is 1.0 for a 1280x720 shot, so AI coords == image coords
    router.brain = FakeVisionBrain([
        '{"found": true, "x": 555, "y": 425, "w": 150, "h": 50, "label": "ACCEPT"}',
        '{"found": true, "x": 700, "y": 425, "w": 150, "h": 50, "label": "DECLINE"}',
    ])

    router.handle("Нова, нажми принять", ctx)
    assert clicks == [(555, 425)] and len(router.brain.calls) == 1

    router.handle("нажми принять", ctx)  # remembered: no AI call
    assert clicks[-1] == (555, 425) and len(router.brain.calls) == 1

    router.handle("не туда", ctx)  # forget that crop, ask again, avoiding the old spot
    assert len(router.brain.calls) == 2 and "WRONG" in router.brain.calls[1]
    assert clicks[-1] == (700, 425)


class StepBrain:
    """Plays back a multi-step task: open the app, look, then type and send."""

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def ask(self, text, history, image_b64=None, progress=None, known=None):
        self.seen.append({"text": text, "image": image_b64 is not None, "progress": list(progress or [])})
        return self.steps.pop(0)


def test_step_by_step_task_learns_whole_sequence(monkeypatch):
    import nova.screen as scr

    calls = []
    for name in ("open", "type_text", "press_key", "wait", "confirm"):
        monkeypatch.setattr(skills.get(name), "run",
                            lambda ctx, _n=name, **kw: calls.append((_n, kw)) or "")
    monkeypatch.setattr(scr, "capture_window", lambda hwnd: screen_with_button(10, 10))
    monkeypatch.setattr(router_mod, "_foreground", lambda: 42)
    send = [{"skill": "press_key", "args": {"key": "ctrl+f"}},
            {"skill": "type_text", "args": {"text": "Вове"}},
            {"skill": "press_key", "args": {"key": "enter"}},
            {"skill": "type_text", "args": {"text": "Привет, я Нова"}},
            {"skill": "confirm", "args": {"question": "Отправить?"}},
            {"skill": "press_key", "args": {"key": "enter"}}]
    first = [{"skill": "open", "args": {"target": "telegram"}}, {"skill": "wait", "args": {"seconds": 1}}]
    tpl = [{"skill": "open", "args": {"target": "telegram"}}, {"skill": "wait", "args": {"seconds": 1}},
           {"skill": "press_key", "args": {"key": "ctrl+f"}}, {"skill": "type_text", "args": {"text": "{кому}"}},
           {"skill": "press_key", "args": {"key": "enter"}}, {"skill": "type_text", "args": {"text": "{текст}"}},
           {"skill": "confirm", "args": {"question": "Отправить?"}}, {"skill": "press_key", "args": {"key": "enter"}}]
    router, ctx = make_router()
    router.brain = StepBrain([
        {"say": "", "actions": first, "continue": True},
        # Planned all the way to the end, but Nova holds everything from confirm on...
        {"say": "", "actions": send, "continue": True},
        # ...until the AI has looked again and starts the step with confirm.
        {"say": "Отправлено", "actions": send[4:],
         "learn": {"template": "перешли {текст} {кому}", "actions": tpl}},
    ])
    router.handle("Нова, перешли Привет, я Нова Вове", ctx)
    # The AI got the phrase as spoken, then a screenshot with the steps done so far.
    assert router.brain.seen[0]["text"] == "перешли Привет, я Нова Вове"
    assert router.brain.seen[1]["image"] and len(router.brain.seen[1]["progress"]) == 2
    assert router.brain.seen[2]["image"] and "paused" in router.brain.seen[2]["progress"][-1]
    assert [c[0] for c in calls] == ["open", "wait", "press_key", "type_text", "press_key", "type_text",
                                     "confirm", "press_key"]
    assert ctx.foreground_hwnd == 42  # clicks/typing follow the newly opened window
    # A flow that ends in sending (it has a confirm) is NOT learned: a replay couldn't look
    # at the screen first and would ask about the spoken name, not the chat really open.
    assert router.learned == {}


def test_step_by_step_stops_on_failure(monkeypatch):
    from nova.skills import SkillError

    def fail(ctx, **kw):
        raise SkillError("Отменено")

    typed = []
    monkeypatch.setattr(skills.get("confirm"), "run", fail)
    monkeypatch.setattr(skills.get("press_key"), "run", lambda ctx, **kw: typed.append(kw) or "")
    router, ctx = make_router()
    blind = [{"skill": "press_key", "args": {"key": "enter"}},
             {"skill": "confirm", "args": {"question": "Отправить?"}},
             {"skill": "press_key", "args": {"key": "enter"}}]
    router.brain = StepBrain([{"say": "", "actions": blind}, {"say": "", "actions": blind[1:]}])
    monkeypatch.setattr("nova.screen.capture_window", lambda hwnd: screen_with_button(10, 10))
    assert router.handle("отправь что-нибудь", ctx) == "Отменено"
    # The blind first plan ran nothing (not even its first Enter); after looking,
    # the user said no: nothing sent, nothing learned.
    assert typed == [] and not router.learned
    assert router.brain.seen[1]["image"]

def test_background_task_returns_focus(monkeypatch):
    restored = []
    monkeypatch.setattr(router_mod, "_restore_focus", lambda hwnd: restored.append(hwnd))
    monkeypatch.setattr(skills.get("send_message"), "run", lambda ctx, **kw: "Отправлено")
    monkeypatch.setattr(skills.get("volume"), "run", lambda ctx, **kw: "")
    router, ctx = make_router()
    ctx.foreground_hwnd = 777  # the game
    router._learn("напиши вове привет", {}, [{"skill": "send_message", "args": {"to": "вове", "text": "привет"}}])
    router.handle("напиши вове привет", ctx)
    assert restored == [777]  # back to the game after sending
    ctx.foreground_hwnd = 777
    router.handle("громкость 30", ctx)
    assert restored == [777]  # nothing to restore for an ordinary command

# --- app map: positions and mapped screens -----------------------------------------

def _glowing(shot, x, y):
    """The same button, pulsing brighter (Dota's ACCEPT glows while it waits)."""
    img = shot.image.copy()
    img[y - 30:y + 30, x - 80:x + 80] = np.clip(img[y - 30:y + 30, x - 80:x + 80].astype(int) * 1.6 + 40, 0, 255)
    return Shot(img.astype(np.uint8), 0, 0)


def test_remembered_place_finds_a_glowing_button(tmp_path):
    mem = VisualMemory(tmp_path / "m.db")
    shot = screen_with_button(640, 400)
    mem.learn("dota2.exe", ["accept", "принять"], shot, 640, 400, 150, 50)
    hit = mem.find("dota2.exe", "принять", _glowing(shot, 640, 400))
    assert hit and abs(hit["x"] - 640) < 6 and abs(hit["y"] - 400) < 6


def test_mapped_screen_is_learned_and_offered_to_the_ai(tmp_path):
    from nova.vision import map_screen

    class MapBrain:
        def ask_image(self, prompt, b64, system):
            return ('{"elements": [{"label": "ARMORY", "x": 200, "y": 40, "w": 110, "h": 40},'
                    ' {"label": "PLAY DOTA", "x": 1100, "y": 680, "w": 200, "h": 50}]}')

    mem = VisualMemory(tmp_path / "m.db")
    shot = screen_with_button(1100, 680, "PLAY")
    assert mem.due_for_map("dota2.exe", shot) and not mem.due_for_map("dota2.exe", shot)
    map_screen(mem, MapBrain(), shot, "dota2.exe")
    assert {"armory", "play dota"} <= set(mem.labels("dota2.exe"))
    assert mem.find("dota2.exe", "play dota", shot)


def test_glued_phrase_still_hits_the_learned_rule(tmp_path):
    """Whisper heard "Примегру" for "Прими игру": the click must not wait for the AI."""
    router, ctx = make_router()
    router.learned["прими игру"] = {"actions": [{"skill": "click", "args": {"target": "Принять"}}],
                                   "example": "прими игру", "shape": {}, "hits": 3, "last": 0}
    assert router._match_learned("примегру")[0] == "прими игру"
    assert router._match_learned("прими звонок") is None
    assert "прими игру" in router.common_phrases()


def test_learned_game_button_is_pressed_from_any_window(monkeypatch):
    """Learned in Dota, said later from the browser: the rule goes to Dota's window."""
    router, ctx = make_router()
    calls = []

    def run(name, args, ctx, raise_errors=False):
        calls.append(dict(args))
        if not args.get("app"):  # the first time: found by sight in the active game
            ctx.last_action = {"type": "visual", "app": "dota2.exe", "target": args["target"]}
        return ""

    monkeypatch.setattr(router, "_run_skill", run)
    router.brain = StepBrain([{"say": "", "actions": [{"skill": "click", "args": {"target": "Принять"}}],
                               "learn": {"template": "прими игру",
                                         "actions": [{"skill": "click", "args": {"target": "Принять"}}]}}])
    router.handle("Нова, прими игру", ctx)
    assert router.learned["прими игру"]["actions"][0]["args"]["app"] == "dota2.exe"
    calls.clear()
    router.handle("Нова, примегру", Context(config=router.cfg, confirm=lambda q: True))
    assert calls == [{"target": "Принять", "app": "dota2.exe"}]
