"""Skills that look at the screen: "посмотри сюда", and clicking things UI Automation can't see."""

import logging

from .. import screen
from ..brain import BrainError
from ..i18n import AI_LANGUAGE, language, t
from ..text import normalize
from ..vision import ai_describe, ai_locate
from . import SkillError, skill

log = logging.getLogger(__name__)

_HERE = {"сюда", "вот сюда", "тут", "здесь", "на это", "это", "here", "this", "at this", "这里", "这儿", "这个"}


def click_visual(ctx, target: str, avoid: tuple[int, int] | None = None, just_shown: bool = False) -> str | None:
    """Find `target` in the active window by sight: remembered crop first, the AI if needed.
    `just_shown`: Nova has just brought this window up (a game being restored from the
    background), so it may still be drawing — give the remembered crop a second look."""
    import time

    app = screen.app_name(ctx.foreground_hwnd) if ctx.foreground_hwnd else "desktop"
    if just_shown:
        time.sleep(0.35)
    shot = screen.capture_window(ctx.foreground_hwnd)
    if shot.image.size and float(shot.image.mean()) < 2.0:
        raise SkillError(t("black_capture"))  # exclusive fullscreen: Windows hands out a black frame
    if avoid is None and ctx.memory is not None:
        hit = ctx.memory.find(app, target, shot)
        if hit is None and just_shown:
            time.sleep(0.5)  # full-screen games take a moment to come back
            shot = screen.capture_window(ctx.foreground_hwnd)
            hit = ctx.memory.find(app, target, shot)
        if hit:
            _check_window(ctx.foreground_hwnd, shot)
            _click_and_return_mouse(hit["x"], hit["y"], back=just_shown)
            ctx.memory.feedback(hit["id"], True)  # used: it stays (and a mapped one becomes trusted)
            ctx.last_action = {"type": "visual", "pattern": hit["id"], "target": target, "app": app,
                               "hwnd": ctx.foreground_hwnd, "point": (hit["x"], hit["y"])}
            return None
    if ctx.brain is None or ctx.config.get("brain") == "off":
        raise SkillError(t("not_on_screen", target=target))
    try:
        found = ai_locate(ctx.brain, shot, target, app, avoid)
    except BrainError as e:
        raise SkillError(str(e)) from None
    if not found:
        raise SkillError(t("not_on_screen", target=target))
    x, y = shot.left + found["x"], shot.top + found["y"]
    _check_window(ctx.foreground_hwnd, shot)  # the AI took seconds: is it still the same window?
    _click_and_return_mouse(x, y, back=just_shown)
    pattern = None
    if ctx.memory is not None:
        pattern = ctx.memory.learn(app, [target, found["label"]], shot,
                                   found["x"], found["y"], found["w"], found["h"])
        # The AI had to look anyway: map the rest of this screen in the background, so
        # the next button here is pressed at once ("плюс минус маппинг приложения").
        if ctx.config.get("app_mapping", True) and ctx.memory.due_for_map(app, shot):
            import threading

            from ..vision import map_screen

            threading.Thread(target=map_screen, args=(ctx.memory, ctx.brain, shot, app),
                             name="map-screen", daemon=True).start()
    ctx.last_action = {"type": "visual", "pattern": pattern, "target": target, "app": app,
                       "hwnd": ctx.foreground_hwnd, "point": (x, y),
                       "avoid": list(avoid) if avoid and not isinstance(avoid[0], (int, float)) else
                       ([avoid] if avoid else [])}
    return None


def _click_and_return_mouse(x: int, y: int, back: bool) -> None:
    """Click; in the background case put the pointer back where the user had it."""
    import ctypes
    import ctypes.wintypes as wt

    point = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    screen.click_at(x, y)
    if back:
        ctypes.windll.user32.SetCursorPos(point.x, point.y)


def _check_window(hwnd: int, shot) -> None:
    if not screen.still_there(hwnd, shot):
        raise SkillError(t("window_changed"))


@skill(
    "look",
    "Посмотреть на экран и ответить на вопрос о том, что там видно (ошибка, текст, картинка). "
    "where: 'cursor' — область вокруг курсора мыши («посмотри сюда»), 'window' — активное окно.",
    patterns=(
        # A "where" is required: "посмотри погоду" is a search, not a screenshot to the cloud.
        r"^(?:посмотри|глянь|взгляни|смотри)\s+(?P<where>сюда|вот сюда|тут|здесь|на это|на экран|на окно)"
        r"\s*(?P<question>.*)$",
        r"^(?:что|чё|че)\s+(?P<where>тут|здесь)\s*(?P<question>.*)$",
        r"^(?:look|look at)\s+(?P<where>here|this|at this|the screen|the window|at the screen)\s*(?P<question>.*)$",
        r"^what\s*(?:is|s)\s+(?P<where>here|on the screen|on my screen)\s*(?P<question>.*)$",
        r"^(?:看|看看|你看)(?P<where>这里|这儿|屏幕)(?P<question>.*)$",
    ),
    params={"where": "'cursor' или 'window'", "question": "что спросить про увиденное"},
    priority=-1,
)
def look(ctx, where: str = "", question: str = ""):
    if ctx.brain is None or ctx.config.get("brain") == "off":
        raise SkillError(t("ai_off"))
    where = normalize(where)
    shot = (screen.capture_around_cursor() if where in _HERE or where == "cursor"
            else screen.capture_window(ctx.foreground_hwnd))
    try:
        return ai_describe(ctx.brain, shot, question, AI_LANGUAGE.get(language(), "English"))
    except BrainError as e:
        raise SkillError(str(e)) from None


@skill(
    "click_here",
    "Кликнуть там, где сейчас курсор мыши.",
    patterns=(r"^(?:нажми|кликни|тыкни|щелкни)\s+(?:сюда|тут|здесь)$",
              r"^(?:click|press)\s+(?:here|this)$",
              r"^(?:点这里|点击这里|点这儿)$"),
)
def click_here(ctx):
    x, y = screen.cursor_pos()
    screen.click_at(x, y)
    return None
