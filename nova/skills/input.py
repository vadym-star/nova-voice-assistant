"""Click on-screen elements by name, press keys, type text, scroll."""

import re
import time

import keyboard
from rapidfuzz import fuzz

from ..i18n import t
from ..text import normalize
from . import SkillError, skill

KEYS = {
    "энтер": "enter", "интер": "enter", "ввод": "enter", "enter": "enter",
    "эскейп": "esc", "escape": "esc", "эскейт": "esc", "отмена": "esc",
    "пробел": "space", "таб": "tab", "табуляция": "tab",
    "удалить": "delete", "делит": "delete", "бэкспейс": "backspace", "стереть": "backspace",
    "вверх": "up", "вниз": "down", "влево": "left", "вправо": "right",
    "домой": "home", "хоум": "home", "энд": "end",
    "копировать": "ctrl+c", "скопируй": "ctrl+c", "вставить": "ctrl+v", "вставь": "ctrl+v",
    "вырезать": "ctrl+x", "выделить все": "ctrl+a", "выдели все": "ctrl+a",
    "отменить": "ctrl+z", "сохранить": "ctrl+s", "сохрани": "ctrl+s",
    "новая вкладка": "ctrl+t", "закрыть вкладку": "ctrl+w", "обновить": "f5", "обнови": "f5",
    "виндовс": "windows", "пуск": "windows", "альт таб": "alt+tab",
    # English
    "esc": "esc", "space": "space", "tab": "tab", "delete": "delete", "backspace": "backspace",
    "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end",
    "copy": "ctrl+c", "paste": "ctrl+v", "cut": "ctrl+x", "select all": "ctrl+a", "undo": "ctrl+z",
    "save": "ctrl+s", "new tab": "ctrl+t", "close tab": "ctrl+w", "refresh": "f5", "reload": "f5",
    "windows": "windows", "start": "windows", "alt tab": "alt+tab",
    # 中文
    "回车": "enter", "确认": "enter", "退出": "esc", "空格": "space", "删除": "delete", "退格": "backspace",
    "复制": "ctrl+c", "粘贴": "ctrl+v", "剪切": "ctrl+x", "全选": "ctrl+a", "撤销": "ctrl+z",
    "保存": "ctrl+s", "新标签页": "ctrl+t", "关闭标签页": "ctrl+w", "刷新": "f5", "开始": "windows",
}
KEYS.update({f"ф{i}": f"f{i}" for i in range(1, 13)})
KEYS.update({f"f{i}": f"f{i}" for i in range(1, 13)})

# Many apps have an English UI while the user speaks Russian.
UI_WORDS = {
    "сообщение": "message", "адрес": "address", "почта": "email", "пароль": "password", "имя": "name",
    "логин": "username", "телефон": "phone", "комментарий": "comment", "тема": "subject",
    "确定": "ok", "取消": "cancel", "保存": "save", "下一步": "next", "接受": "accept", "确认": "confirm",
    "开始": "start", "删除": "delete", "退出": "exit", "关闭": "close", "打开": "open", "发送": "send",
    "继续": "continue", "完成": "done", "登录": "sign in", "设置": "settings", "播放": "play",
    "файл": "file", "правка": "edit", "вид": "view", "справка": "help", "помощь": "help",
    "сохранить": "save", "сохранить как": "save as", "открыть": "open", "закрыть": "close",
    "отмена": "cancel", "отменить": "cancel", "да": "yes", "нет": "no", "ок": "ok", "окей": "ok",
    "не сохранять": "don't save", "настройки": "settings", "параметры": "options",
    "поиск": "search", "найти": "find", "назад": "back", "вперед": "forward", "далее": "next",
    "готово": "done", "применить": "apply", "удалить": "delete", "отправить": "send",
    "войти": "sign in", "выйти": "sign out", "продолжить": "continue", "принять": "accept",
    "скачать": "download", "загрузить": "upload", "поделиться": "share", "копировать": "copy",
    "вставить": "paste", "новая вкладка": "new tab", "новый": "new", "печать": "print",
    "свернуть": "minimize", "развернуть": "maximize", "играть": "play", "пропустить": "skip",
}

# UI Automation control types worth clicking, with a small bonus so a button
# beats a text label of the same name.
_CLICKABLE = {
    "ButtonControl": 6, "MenuItemControl": 6, "HyperlinkControl": 5, "TabItemControl": 5,
    "ListItemControl": 4, "TreeItemControl": 4, "CheckBoxControl": 4, "RadioButtonControl": 4,
    "SplitButtonControl": 5, "ComboBoxControl": 3, "EditControl": 2, "TextControl": 0,
    "DataItemControl": 3, "ImageControl": 0,
}


def _find_element(root, target: str, max_nodes: int = 3000, strict: bool = False, budget_s: float = 1.5):
    """The control named `target`. Whole words only ("ok" is not inside "token"),
    never a disabled one or a list cell that can't be pressed. `strict`: the name must
    be (nearly) exactly the target — for words that are also keys ("вставить")."""
    import uiautomation as auto

    target = target.lower()
    whole_word = re.compile(rf"(?<!\w){re.escape(target)}(?!\w)")
    best, best_score = None, 0.0
    seen, started = 0, time.monotonic()
    for control, _depth in auto.WalkControl(root, maxDepth=30):
        seen += 1
        if seen > max_nodes or time.monotonic() - started > budget_s:
            break
        name = (control.Name or "").strip().lower()
        if not name or len(name) > 80:
            continue
        score = fuzz.ratio(target, name) if len(target) >= 4 or name == target else 0
        if not strict and len(target) >= 3 and whole_word.search(name):
            score = max(score, 85 + 10 * len(target) / len(name))
        if score < (90 if strict else 75):
            continue
        kind = _CLICKABLE.get(control.ControlTypeName, 1)
        if kind <= 1 and name != target:
            continue  # a text label or cell named like it ("Sender") is not the button
        score += kind
        if score > best_score:
            try:
                if control.IsOffscreen or not control.IsEnabled:
                    continue  # hidden, or greyed out: pressing it would "succeed" doing nothing
            except Exception:  # noqa: BLE001 - stale element
                continue
            best, best_score = control, score
    return best


# Known browsers, in the order to fall back on when the default one isn't running.
_BROWSERS = ("chrome.exe", "msedge.exe", "opera.exe", "firefox.exe", "brave.exe", "browser.exe", "vivaldi.exe")
# "the browser" in general — not a specific one ("в хроме" is matched by name below).
_BROWSER_WORDS = {"браузер", "браузере", "браузера", "browser", "the browser", "浏览器", "интернет", "интернете"}


def _browser_window() -> int:
    """The user's default browser (Opera GX, Chrome...), started if it isn't running."""
    import subprocess

    from .. import screen

    path, exe = screen.default_browser()
    if exe and (hwnd := screen.main_window_of(exe)):
        return hwnd
    if path:
        subprocess.Popen([path])
        for _ in range(30):
            time.sleep(0.3)
            if hwnd := screen.main_window_of(exe):
                return hwnd
    # Default unknown or failed to start: any browser that is open.
    return next((h for b in _BROWSERS if (h := screen.main_window_of(b))), 0)


# Key names that are also everyday button labels ("click save" means the Save button).
_BUTTON_WORDS = {"save", "delete", "copy", "paste", "cut", "undo", "start", "exit", "close", "open", "cancel",
                 "ok", "home", "end", "print", "refresh", "new tab"}


def _window_for(app: str) -> int:
    """A running app's window by what the user called it ("в телеграме", "в браузере")."""
    from rapidfuzz import fuzz

    from .. import screen
    from ..text import translit
    from .apps import APP_ALIASES

    if app.lower().endswith(".exe"):  # remembered by a learned rule: exact
        return screen.main_window_of(app.lower())
    spoken = normalize(app)
    if spoken in _BROWSER_WORDS:
        return _browser_window()
    alias = APP_ALIASES.get(spoken, APP_ALIASES.get(spoken.rstrip("е"), spoken))
    best, best_score = 0, 0
    for hwnd, title, exe in screen.top_windows():
        queries = {alias, translit(alias), spoken}
        # The program itself first: a browser tab titled "Telegram Web" is not Telegram.
        score = max(fuzz.partial_ratio(q, exe.removesuffix(".exe")) for q in queries) + 5
        if exe not in _BROWSERS:
            score = max(score, max(fuzz.partial_ratio(q, title.lower()) for q in queries))
        if score > best_score:
            best, best_score = hwnd, score
    return best if best_score >= 80 else 0


def _target_window(ctx, app: str | None) -> tuple[int, bool]:
    """(window to act in, whether it's in the background)."""
    if not app:
        return ctx.foreground_hwnd, False
    hwnd = _window_for(app)
    if not hwnd and app.lower().endswith(".exe"):
        # The app a learned rule remembers ("прими игру" -> dota2.exe) is a preference:
        # when it isn't running, look in what is in front instead (another game, a browser).
        return ctx.foreground_hwnd, False
    if not hwnd:
        raise SkillError(t("window_not_found", target=app))
    return hwnd, hwnd != ctx.foreground_hwnd


def _activate(element, allow_mouse: bool = True) -> bool:
    """Press a control the way a screen reader does — no mouse move, no focus change.
    Falls back to a real mouse click only if allowed; returns False if nothing worked."""
    import uiautomation as auto

    for pattern_id, action in ((auto.PatternId.InvokePattern, "Invoke"),
                               (auto.PatternId.TogglePattern, "Toggle"),
                               (auto.PatternId.SelectionItemPattern, "Select"),
                               (auto.PatternId.ExpandCollapsePattern, "Expand")):
        try:
            pattern = element.GetPattern(pattern_id)
            if pattern:
                getattr(pattern, action)()
                return True
        except Exception:  # noqa: BLE001 - pattern present but refused: try the next way
            continue
    if allow_mouse:
        element.Click(simulateMove=False, waitTime=0.05)
        return True
    return False


def _snap_back(ctx, background: bool) -> None:
    """Many apps (browsers, UWP) pull themselves to the front when automated, even
    without a click. In the background case, hand the focus straight back."""
    from .. import screen

    if background and ctypes_foreground() != ctx.foreground_hwnd:
        screen.bring_to_front(ctx.foreground_hwnd)


def _show_then_return(ctx, hwnd: int) -> None:
    """Bring a background app up for an action that needs it; Nova puts the user back after."""
    from .. import guard, screen

    if guard.games.blocking:
        raise SkillError(t("anticheat_blocked"))  # don't minimise the game for nothing

    if not screen.bring_to_front(hwnd):
        raise SkillError(t("cant_focus", app=screen.app_name(hwnd)))
    ctx.extra["restore_focus"] = True


@skill(
    "click",
    "Нажать на кнопку/ссылку/пункт меню по видимому названию. С app — в другой открытой программе, "
    "по возможности в фоне, не мешая пользователю (например, пока он в игре).",
    patterns=(
        r"^(?:нажми|кликни|тыкни|щелкни|ткни)\s+(?:на\s+)?(?:кнопку\s+)?(?P<target>.+)$",
        r"^(?:click|press|tap|hit)\s+(?:on\s+)?(?:the\s+)?(?:button\s+)?(?P<target>.+)$",
        r"^(?:点击|单击|点一下|点|按)\s*(?:一下)?\s*(?P<target>.+)$",
    ),
    params={"target": "видимый текст элемента",
            "app": "необязательно: в какой программе (если не в активном окне)"},
    priority=-1,
)
def click(ctx, target: str, app: str | None = None):
    import uiautomation as auto

    name = normalize(target)
    name = re.sub(r"^(?:the\s+)?(.+?)\s+(?:key|button)$|^下(.+)$|^(.+)键$",
                  lambda m: next(g for g in m.groups() if g), name)  # "the enter key", "按下回车"
    if "+" in name:
        return press_key(ctx, name, app)  # "press ctrl+c"
    # "нажми энтер" is a key; "нажми сохранить" or "click save" is a button first,
    # the key only if there's no such button.
    if name in KEYS and name not in UI_WORDS and name not in _BUTTON_WORDS:
        return press_key(ctx, name, app)
    hwnd, background = _target_window(ctx, app)
    roots = [auto.ControlFromHandle(hwnd)] if hwnd else []
    if not background:
        roots.append(auto.PaneControl(searchDepth=1, ClassName="Shell_TrayWnd"))  # taskbar
    names = [name] + ([UI_WORDS[name]] if name in UI_WORDS else [])
    for root in roots:
        for candidate in names:
            try:
                element = _find_element(root, candidate, strict=name in KEYS)
            except Exception:  # noqa: BLE001 - window closed meanwhile
                continue
            if element:
                # In the background, never move the mouse: the user is busy with something else.
                if not _activate(element, allow_mouse=not background):
                    _show_then_return(ctx, hwnd)
                    _activate(element)
                else:
                    _snap_back(ctx, background)
                ctx.last_action = {"type": "uia", "target": target}
                return None
    if name in KEYS:
        return press_key(ctx, name, app)
    if background:
        _show_then_return(ctx, hwnd)
        ctx.foreground_hwnd = hwnd
    # Games and custom-drawn apps expose no UI tree: find the element by sight.
    from .vision import click_visual

    return click_visual(ctx, target, just_shown=background)


@skill(
    "press_key",
    "Нажать клавишу или сочетание клавиш. С app — в другой программе (Nova покажет её и вернёт обратно).",
    patterns=(
        r"^(?:нажми|зажми)\s+(?:клавишу|кнопку|сочетание)\s+(?P<key>.+)$",
        r"^(?:press|hit)\s+(?:the\s+)?(?:key|keys|shortcut)\s+(?P<key>.+)$",
        r"^按\s*(?:键|快捷键)\s*(?P<key>.+)$",
    ),
    params={"key": "клавиша в формате библиотеки keyboard, например 'enter', 'ctrl+c', 'alt+tab', 'f5'",
            "app": "необязательно: в какой программе"},
)
def press_key(ctx, key: str, app: str | None = None):
    k = KEYS.get(normalize(key), key)
    hwnd, background = _target_window(ctx, app)
    if background:
        _show_then_return(ctx, hwnd)
    try:
        keyboard.send(k)
    except ValueError:
        raise SkillError(t("unknown_key", key=key)) from None
    return None


def _find_field(hwnd: int, field: str):
    import uiautomation as auto
    from rapidfuzz import fuzz

    wanted = {normalize(field), UI_WORDS.get(normalize(field), normalize(field))}
    best, best_score = None, 0
    started, seen = time.monotonic(), 0
    for control, depth in auto.WalkControl(auto.ControlFromHandle(hwnd), maxDepth=25):
        seen += 1
        if seen > 3000 or time.monotonic() - started > 1.5:
            break  # a huge page: don't keep the user waiting
        if control.ControlTypeName not in ("EditControl", "ComboBoxControl", "DocumentControl"):
            continue
        name = normalize(control.Name or "")
        score = max(fuzz.partial_ratio(w, name) for w in wanted) if name else 0
        # Deeper means inside the page: its own search box beats the browser's address bar.
        score += min(depth, 10) * 0.2
        if score > best_score:
            try:
                if control.IsOffscreen or not control.IsEnabled:
                    continue
            except Exception:  # noqa: BLE001
                continue
            best, best_score = control, score
    return best if best_score >= 80 else None


@skill(
    "type_text",
    "Напечатать текст. Без field — в активное поле. С field (название поля: «поиск», «адрес», «email») "
    "и/или app — Nova впишет текст прямо в это поле, по возможности в фоне, не трогая клавиатуру.",
    patterns=(
        # Plain "напиши Вове привет" is a message to someone, so it goes to the AI;
        # only unambiguous typing phrases are instant.
        r"^(?:напечатай|введи|набери)\s+(?:текст\s+)?(?P<text>.+)$",
        r"^напиши\s+текст\s+(?P<text>.+)$",
        r"^type\s+(?:the\s+text\s+)?(?P<text>.+)$",  # not "write": "write an email to mom" is a task
        r"^(?:输入|打字)\s*(?P<text>.+)$",
    ),
    params={"text": "текст для ввода",
            "field": "необязательно: название поля ввода",
            "app": "необязательно: в какой программе",
            "submit": "true — сразу нажать Enter (адресная строка, поиск). Делай так вместо отдельного "
                      "press_key: браузеры стирают недописанный адрес, если окно теряет фокус"},
)
def type_text(ctx, text: str, field: str | None = None, app: str | None = None, submit=False):
    import uiautomation as auto

    from ..text import as_said

    raw = ctx.extra.get("raw", "")
    said = None if ctx.extra.get("from_ai") else re.match(
        r"^\W*(?:напечатай|напиши|введи|набери|впиши|вставь текст|type|write|enter|输入)\s+(?:текст\s+)?(?P<t>.+)$",
        raw, re.I | re.S)
    if said and set(normalize(text).split()) <= set(normalize(said["t"]).split()):
        # The user's own dictation: everything after the verb, untouched (fillers, ё, symbols).
        text = said["t"].strip()
        if field and text.endswith(".") and not text.endswith(".."):
            text = text[:-1]  # Whisper's final period doesn't belong in "user@mail.ru"
        text = re.sub(r"\s+(?:в|into|in)\s+(?:поле|field)\s+\S+\s*$", "", text, flags=re.I) if field else text
    elif not ctx.extra.get("from_ai"):
        text = as_said(raw, text)  # keep capitals and punctuation
    submit = submit in (True, "true", "True", 1, "1")

    hwnd, background = _target_window(ctx, app)
    if field and hwnd:
        box = _find_field(hwnd, field)
        pattern = box.GetPattern(auto.PatternId.ValuePattern) if box else None
        if pattern:
            try:
                pattern.SetValue(text)
                try:
                    took = normalize(pattern.Value) == normalize(text)
                except Exception:  # noqa: BLE001 - a password box won't tell: SetValue worked
                    took = True
                if took or getattr(box, "IsPassword", False):
                    if submit:
                        _submit(ctx, hwnd, box)
                    _snap_back(ctx, background)
                    return None
            except Exception:  # noqa: BLE001 - read-only or custom field
                pass
        if box is None:
            # Typing anyway would land in whatever has focus there (a chat, a game).
            raise SkillError(t("field_not_found", field=field))
        if background:
            _show_then_return(ctx, hwnd)
        try:
            box.SetFocus()  # a field gets focus; "activating" a combo box would open its list
        except Exception:  # noqa: BLE001
            box.Click(simulateMove=False, waitTime=0.05)
        keyboard.send("ctrl+a")  # replace whatever is there, never add to it
    elif background:
        _show_then_return(ctx, hwnd)
    from .. import screen

    # Keys go to whatever window is active: make sure that's the one we mean.
    if hwnd and ctypes_foreground() != hwnd and not screen.bring_to_front(hwnd):
        raise SkillError(t("cant_focus", app=screen.app_name(hwnd)))
    time.sleep(0.15)  # let the hotkey modifiers come up first
    keyboard.write(text, delay=0.005)
    if submit:
        keyboard.send("enter")
    return None


def _submit(ctx, hwnd: int, box) -> None:
    """Press Enter in a field that was filled through UI Automation."""
    from .. import screen

    if ctypes_foreground() != hwnd and not screen.bring_to_front(hwnd):
        raise SkillError(t("cant_focus", app=screen.app_name(hwnd)))
    try:
        box.SetFocus()
    except Exception:  # noqa: BLE001 - the field may already have focus
        pass
    keyboard.send("enter")
    time.sleep(0.1)


def ctypes_foreground() -> int:
    import ctypes

    return ctypes.windll.user32.GetForegroundWindow()

@skill(
    "scroll",
    "Прокрутить страницу вверх или вниз.",
    patterns=(
        r"^(?:прокрути|листай|пролистай|скролл|скроль)\s*(?P<direction>вверх|вниз|наверх)?$",
        r"^scroll\s*(?P<direction>up|down)?$",
        r"^(?:向)?(?P<direction>上|下)?(?:滚动|滑动|翻)(?:一下)?$",
    ),
    params={"direction": "'вверх' или 'вниз'"},
)
def scroll(ctx, direction: str | None = None):
    import uiautomation as auto

    up = direction in ("вверх", "наверх", "up", "上")
    auto.WheelUp(5) if up else auto.WheelDown(5)
    return None