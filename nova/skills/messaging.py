"""Sending messages, done carefully: a wrong recipient can't be undone.

Telegram Desktop flow:
1. click the Search field (UI Automation) — never type into whatever has focus;
2. pick the chat: a known name from nova.knowledge ("вове" -> "Вовчикинатор"),
   else search + the AI reads the results, else suggest from the chat list
   ("«Вова» — это Вовчикинатор?"), else ask how the person is saved;
3. check the window title — Telegram puts the open chat's name there;
4. ask "Открыт чат X. Отправить «...»?" and only then type and press Enter.
"""

import ctypes
import json
import logging
import re
import time

import keyboard
from rapidfuzz import fuzz

from .. import screen
from ..brain import BrainError
from ..i18n import t
from ..text import normalize
from . import SkillError, skill

log = logging.getLogger(__name__)
user32 = ctypes.windll.user32

_TELEGRAM = {"telegram", "телеграм", "телеграмм", "тг", "tg", "телега", "电报"}
_SEARCH_NAMES = ("Search", "Поиск", "Пошук", "搜索")
# "Избранное" / "себе": the user's own chat, named differently per UI language.
_SAVED_SPOKEN = {"избранное", "в избранное", "себе", "мне", "сам себе", "saved messages", "saved", "myself",
                 "me", "收藏", "收藏夹", "自己"}
_SAVED_TITLES = ("Saved Messages", "Избранное", "Збережене", "收藏夹")
_INPUT_NAMES = ("Write a message...", "Написать сообщение...", "Напишіть повідомлення...", "输入消息…", "Message")


def _telegram_hwnd() -> int:
    return screen.main_window_of("telegram.exe")


def _open_telegram() -> int:
    hwnd = _telegram_hwnd()
    if not hwnd:
        from .apps import open_target

        open_target(None, "telegram")
        for _ in range(30):
            time.sleep(0.3)
            if hwnd := _telegram_hwnd():
                break
    if not hwnd:
        raise SkillError(t("app_not_found", target="Telegram"))
    if not screen.bring_to_front(hwnd):
        # Typing now would go into whatever window is active (the game, another chat).
        raise SkillError(t("cant_focus", app="Telegram"))
    time.sleep(0.2)
    return hwnd


def open_chat_title(hwnd: int) -> str:
    """'‎Катюшка @ ‎Вадим (51603)' -> 'Катюшка'; '' when no chat is open."""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value.replace("‎", "").replace("‏", "")
    return title.rsplit(" @ ", 1)[0].strip() if " @ " in title else ""


_edit_cache: dict[tuple[int, tuple], object] = {}


def _find_edit(hwnd: int, names: tuple[str, ...]):
    """Find a text field by name without walking into the message history
    (thousands of elements in a busy chat); remembered per window."""
    import uiautomation as auto

    cached = _edit_cache.get((hwnd, names))
    if cached is not None:
        try:
            if cached.Exists(0, 0):
                return cached
        except Exception:  # noqa: BLE001 - control went away
            pass
    queue = [(auto.ControlFromHandle(hwnd), 0)]
    while queue:
        control, depth = queue.pop(0)
        kind = control.ControlTypeName
        if kind == "EditControl" and (control.Name or "") in names:
            _edit_cache[(hwnd, names)] = control
            return control
        if depth >= 10 or kind in ("ListControl", "DataItemControl", "ListItemControl", "HyperlinkControl"):
            continue  # message lists and their items never contain the fields we want
        queue += [(child, depth + 1) for child in control.GetChildren()]
    return None


def _typing(text: str, hwnd: int = 0) -> None:
    """Type text, then put the keyboard layout back the way the user had it.
    A new line is Shift+Enter (plain Enter would send each line on its own), and if the
    user switches to another window meanwhile, typing stops there."""
    hkl = user32.GetKeyboardLayout(user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None))
    for n, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if n:
            keyboard.send("shift+enter")
        for i in range(0, len(line), 24):
            if hwnd and user32.GetForegroundWindow() != hwnd:
                raise SkillError(t("cant_focus", app="Telegram"))
            keyboard.write(line[i:i + 24], delay=0.005)
    WM_INPUTLANGCHANGEREQUEST = 0x0050
    user32.PostMessageW(user32.GetForegroundWindow(), WM_INPUTLANGCHANGEREQUEST, 0, hkl)


def _search(hwnd: int, query: str) -> None:
    if user32.GetForegroundWindow() != hwnd and not screen.bring_to_front(hwnd):
        raise SkillError(t("cant_focus", app="Telegram"))
    box = _find_edit(hwnd, _SEARCH_NAMES)
    if box is None:
        raise SkillError(t("not_on_screen", target="Search"))
    box.Click(simulateMove=False, waitTime=0.1)
    keyboard.send("ctrl+a")
    _typing(query)
    time.sleep(0.9)  # results appear as you type


def _ai_pick_chat(ctx, hwnd: int, wanted: str) -> dict | None:
    """Look at the search results and pick the chat the user means."""
    shot = screen.capture_window(hwnd)
    # Only the chat list on the left: smaller and faster to read, and the open
    # conversation itself is none of the AI's business.
    shot = screen.Shot(shot.image[:, :min(shot.width, 720)].copy(), shot.left, shot.top)
    b64, scale = shot.to_jpeg_b64()
    prompt = (f'Telegram Desktop, search results are on the left. The user wants to message "{wanted}" '
              "(any grammatical case; the chat may be saved under a nickname or full name, e.g. «Саше» can be "
              "«Санёк» or «Александр Петров»). "
              'Reply ONLY JSON {"found": bool, "x": int, "y": int, "title": "exact chat name as shown", '
              '"retry_query": "a shorter search text to try if nothing matches, or empty"} — x,y is the centre '
              "of the best matching chat row in image pixels. found=false unless one result clearly matches.")
    try:
        reply = ctx.brain.ask_image(prompt, b64, "You read chat apps' screenshots. Reply with one JSON object.")
    except BrainError as e:
        raise SkillError(str(e)) from None
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("found"):
        try:
            data["x"] = shot.left + round(float(data["x"]) / scale)
            data["y"] = shot.top + round(float(data["y"]) / scale)
        except (KeyError, TypeError, ValueError):
            return None  # "found" without usable coordinates: treat as not found
    return data


def _open_by_name(hwnd: int, name: str) -> str:
    """Search an exact chat name, open the top result; '' unless that chat really opened."""
    _search(hwnd, name)
    keyboard.send("enter")
    time.sleep(0.6)
    title = open_chat_title(hwnd)
    return title if title and fuzz.ratio(normalize(title), normalize(name)) >= 85 else ""


def _ai_candidates(ctx, hwnd: int, wanted: str) -> list[dict]:
    """Chats in the visible list that could be who the user means ("Вова" ~ "Вовчикинатор", "Владимир")."""
    shot = screen.capture_window(hwnd)
    shot = screen.Shot(shot.image[:, :min(shot.width, 720)].copy(), shot.left, shot.top)
    b64, scale = shot.to_jpeg_b64()
    prompt = (f'Telegram Desktop, the chat list is on the left. The user wants to message "{wanted}". Which '
              "visible chats could be that person — a nickname, diminutive or full form of the name "
              "(e.g. «Саша» ~ «Санёк», «Александр», «Сашуля»)? Reply ONLY JSON "
              '{"candidates": [{"title": "exact chat name", "x": int, "y": int}]} — at most 3, best first, '
              "x,y the centre of the row in image pixels; empty list if none fit.")
    try:
        reply = ctx.brain.ask_image(prompt, b64, "You read chat apps' screenshots. Reply with one JSON object.")
    except BrainError:
        return []
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        items = json.loads(m.group(0)).get("candidates", []) if m else []
    except (json.JSONDecodeError, AttributeError):
        return []
    out = []
    for c in items[:3] if isinstance(items, list) else []:
        try:  # an odd item ("x": "left", a bare string) is skipped, not a crash
            out.append({"title": str(c.get("title", "")), "x": shot.left + round(float(c["x"]) / scale),
                        "y": shot.top + round(float(c["y"]) / scale)})
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return out


def _same_name(a: str, b: str) -> bool:
    """«Катюшка» said and chat «Катюшка 🌸»; «Кате» and «Катя»: the same name, not a guess."""
    from ..knowledge import name_stem

    a, b = normalize(a), normalize(b)
    # Not just the same stem: "Ивановой" and "Иванов" share one and are two people.
    return bool(a) and (a == b or fuzz.ratio(a, b) >= 92)


def _front(hwnd: int) -> None:
    """A spoken answer took seconds: make sure the click lands in Telegram."""
    if user32.GetForegroundWindow() != hwnd and not screen.bring_to_front(hwnd):
        raise SkillError(t("cant_focus", app="Telegram"))


def _resolve_unknown(ctx, hwnd: int, to: str) -> tuple[str, bool]:
    """Nova doesn't know who "Вова" is yet: search, then suggest from the chat list, then ask.
    Returns (chat title, sure) — sure when the chat is literally named what the user said."""
    # 0. The chat is called exactly what was said ("Катюшка"): open it, no AI.
    if (title := _open_by_name(hwnd, to)) and _same_name(title, to):
        return title, True
    if ctx.brain is None or ctx.config.get("brain") == "off":
        raise SkillError(t("chat_not_found", to=to))
    # 1. Telegram search finds chats whose name contains what was said.
    _search(hwnd, to)
    pick = _ai_pick_chat(ctx, hwnd, to)
    if pick and not pick.get("found") and pick.get("retry_query"):
        _search(hwnd, pick["retry_query"])
        pick = _ai_pick_chat(ctx, hwnd, to)
    rejected = set()
    if pick and pick.get("found"):
        exact = _same_name(pick.get("title", ""), to)
        # A loose search hit is only a guess ("Вове" finds the group «Вове подарок»): check with the user.
        if exact or ctx.confirm(t("is_this_contact", to=to, title=pick.get("title", ""))):
            _front(hwnd)
            screen.click_at(pick["x"], pick["y"])
            time.sleep(0.6)
            title = open_chat_title(hwnd)
            if title and fuzz.ratio(normalize(title), normalize(pick.get("title", ""))) >= 80:
                if ctx.knowledge is not None:
                    ctx.knowledge.add_alias(title, to)
                return title, True  # exact, or the user just confirmed it
        rejected.add(normalize(pick.get("title", "")))
    # 2. A nickname search can't find ("Вова" vs "Вовчикинатор"): suggest from the list, ask the user.
    keyboard.send("esc")
    time.sleep(0.4)
    for cand in _ai_candidates(ctx, hwnd, to):
        if normalize(cand["title"]) in rejected:
            continue
        if ctx.confirm(t("is_this_contact", to=to, title=cand["title"])):
            _front(hwnd)
            screen.click_at(cand["x"], cand["y"])
            time.sleep(0.6)
            title = open_chat_title(hwnd)
            if title and fuzz.ratio(normalize(title), normalize(cand["title"])) >= 80:
                if ctx.knowledge is not None:
                    # The user said so: remember even if they cancel sending. The window
                    # title is the exact chat name (the AI's reading may add emoji).
                    ctx.knowledge.add_alias(title, to)
                return title, True
            break
    # 3. Ask how they're saved, and look that up.
    answer = ctx.ask(t("who_is", to=to))
    if answer and (title := _open_by_name(hwnd, answer)):
        if ctx.knowledge is not None:
            ctx.knowledge.add_alias(title, to)
        return title, True
    if answer:
        _search(hwnd, answer)
        pick = _ai_pick_chat(ctx, hwnd, answer)
        if pick and pick.get("found"):
            _front(hwnd)
            screen.click_at(pick["x"], pick["y"])
            time.sleep(0.6)
            if title := open_chat_title(hwnd):
                return title, False
    keyboard.send("esc")
    raise SkillError(t("chat_not_found", to=to))


def _wait_keys_released(limit_s: float = 1.5) -> None:
    """The user may still hold W or Shift from the game: wait until the keyboard is free,
    or those keys repeat into the message (and Shift turns Enter into a new line)."""
    deadline = time.time() + limit_s
    while time.time() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in range(0x08, 0xFF)
                   if vk not in (0x14, 0x90, 0x91)):  # lock keys report their toggle state
            return
        time.sleep(0.05)


def _draft(box) -> str:
    """Text already in the message box ('' if empty or unreadable)."""
    import uiautomation as auto

    try:
        pattern = box.GetPattern(auto.PatternId.ValuePattern)
        value = (pattern.Value or "").strip() if pattern else ""
    except Exception:  # noqa: BLE001
        return ""
    return "" if value in _INPUT_NAMES else value


# Words the message pattern can catch as "to" that are really a writing task.
_NOT_RECIPIENTS = {"код", "письмо", "текст", "стих", "стихи", "статью", "сочинение", "программу", "скрипт",
                   "функцию", "пост", "комментарий", "отзыв", "песню", "рассказ", "историю", "сообщение",
                   "в", "на", "про", "о", "об", "план", "список", "резюме", "эссе", "рецепт"}


def _as_said(ctx, to: str, text: str) -> tuple[str, str]:
    """The instant path hands over lowercase words without punctuation; take the
    exact spelling from what was said ("«Привет, я Нова»" keeps its capitals)."""
    raw = ctx.extra.get("raw", "")
    if not raw:
        return to, text
    quoted = re.search(r"[«\"“](.+?)[»\"”]", raw)
    if quoted and normalize(quoted.group(1)) == normalize(text):
        return to, quoted.group(1).strip()  # "напиши Вове, что «я опоздаю»": the quote is the message
    if quoted and normalize(to) in normalize(quoted.group(1)).split():
        rest = normalize(raw.replace(quoted.group(0), " ")).split()
        # "отправь сообщение «...» Вове": the recipient is the word that isn't a verb/filler.
        skip = {"напиши", "отправь", "скинь", "передай", "сообщение", "в", "телеграм", "телеграмм", "телеграме",
                "телеграмме", "что", "telegram", "тг", "send", "message", "to", "on", "a", "write", "text"}
        names = [w for w in rest if w not in skip]
        return (names[-1] if names else to), quoted.group(1).strip()
    # Everything said after the recipient ("Вове, что мне плохо" -> "мне плохо"): rebuilding
    # it from normalized words would lose "мне", "а", "ну" and match inside other words.
    who = re.search(rf"(?<!\w){re.escape(to).replace('е', '[её]')}(?!\w)", raw, re.I) if to else None
    if who:
        rest = re.sub(r"^[\s,:;—–-]*(?:(?:в\s+(?:телеграм\w*|telegram|тг|телегу))[\s,:;—–-]*)?"
                      r"(?:что|that|saying)?[\s,:;—–-]*", "", raw[who.end():], flags=re.I).strip()
        if rest and set(normalize(text).split()) <= set(normalize(rest).split()):
            return to, re.sub(r"[«»\"“”]", "", rest).strip()
    words = normalize(text).split()
    if words:
        m = re.search(r"(?<!\w)" + r"\W+".join(re.escape(w) for w in words) + r"[^\s]*", raw, re.I)
        if m:
            return to, re.sub(r"[«»\"“”]", "", m.group(0)).strip()
    return to, text


@skill(
    "send_message",
    "Отправить сообщение человеку или в чат в Telegram Desktop. Сам находит чат, проверяет, что открыт "
    "нужный, и спрашивает подтверждение, только если не уверен. Используй его для любых просьб "
    "написать/отправить кому-то сообщение. НЕ добавляй перед ним confirm.",
    patterns=(
        r"^(?:напиши|отправь|скинь|сохрани)\s+(?:сообщение\s+)?(?P<to>в избранное|себе|мне)\s+(?P<text>.+)$",
        r"^(?:напиши|отправь|скинь|передай)\s+(?:сообщение\s+)?(?:в\s+(?:телеграм\w*|telegram|тг|телегу)\s+)?"
        r"(?P<to>[^\s]+)\s+(?:в\s+(?:телеграм\w*|telegram|тг|телегу)\s+)?(?:что\s+)?(?P<text>.+)$",
        r"^send\s+(?:a\s+)?message\s+to\s+(?P<to>\S+)\s+(?:on\s+telegram\s+)?(?:saying\s+|that\s+)?(?P<text>.+)$",
        r"^(?:text|message)\s+(?P<to>\S+)\s+(?:on\s+telegram\s+)?(?:saying\s+|that\s+)?(?P<text>.+)$",
        r"^(?:给|跟)(?P<to>\S{1,8}?)发(?:个|条|一条)?(?:微信|消息|信息|送)?\s*(?:说)?\s*(?P<text>.+)$",
    ),
    params={"to": "кому — как сказал пользователь (например «Вове»)",
            "text": "текст сообщения точно как сказано, с заглавными буквами и знаками препинания",
            "app": "мессенджер, сейчас только 'telegram'"},
    priority=-1,
    background=True,
)
def send_message(ctx, to: str, text: str, app: str = "telegram"):
    if normalize(app) not in _TELEGRAM:
        raise SkillError(t("messenger_unsupported", app=app))
    from .. import guard

    if guard.games.blocking:
        raise SkillError(t("anticheat_blocked"))  # before Telegram is brought over the game
    if normalize(to) in _NOT_RECIPIENTS:
        # "напиши код на питоне", "напиши письмо маме": a writing task, not a Telegram chat.
        raise SkillError(t("chat_not_found", to=to))
    if not ctx.extra.get("from_ai") and ctx.knowledge is not None and normalize(to) not in _SAVED_SPOKEN:
        typing_here = screen.app_name(ctx.foreground_hwnd) not in ("telegram.exe", "")
        if typing_here and not ctx.knowledge.find_contact(to):
            # In Word, a browser...: "напиши Уважаемые коллеги" is probably dictation, and
            # "Уважаемые" isn't anyone Nova knows. Let the AI decide instead of searching chats.
            raise SkillError(t("chat_not_found", to=to))
    said = re.match(r"^\W*(?:напиши|отправь|скинь|сохрани)\s+мне\b[\s,:]*(?P<text>.+)$", ctx.extra.get("raw", ""), re.I)
    if said and not ctx.extra.get("from_ai"):
        to, text = "мне", said["text"]  # "мне" is dropped as a filler word before matching
    to, text = _as_said(ctx, to, text)
    hwnd = _open_telegram()
    spoken = normalize(to)
    title, sure = "", False
    if spoken in _SAVED_SPOKEN:
        for query in _SAVED_TITLES:
            _search(hwnd, query)
            keyboard.send("enter")
            time.sleep(0.6)
            if open_chat_title(hwnd) in _SAVED_TITLES:
                title, sure = open_chat_title(hwnd), True
                break
        if not title:
            keyboard.send("esc")
            raise SkillError(t("chat_not_found", to=to))
    known = None if title or ctx.knowledge is None else ctx.knowledge.find_contact(to)
    if known and (title := _open_by_name(hwnd, known)):
        # Remembered under exactly these words: no question. Only the same stem
        # ("Ивановой" vs a saved "Иванову") may be another person: ask once.
        sure = ctx.knowledge.is_alias(to, known)
    if not title:
        title, sure = _resolve_unknown(ctx, hwnd, to)
    # Ask only when Nova isn't sure who this is, or when the request came after looking
    # at the screen (text on a page could have asked for it), or if the user wants it always.
    policy = ctx.config.get("confirm_messages", "unsure")
    must_ask = (policy == "always" or ctx.extra.get("saw_screen") or ctx.extra.get("low_trust")
                or (policy == "unsure" and not sure))
    if must_ask and not ctx.confirm(t("send_confirm", chat=title, text=text)):
        raise SkillError(t("cancelled"))
    if open_chat_title(hwnd) != title:  # something changed meanwhile
        raise SkillError(t("chat_not_opened", to=to))
    if not screen.bring_to_front(hwnd):  # the user may have clicked elsewhere
        raise SkillError(t("cant_focus", app="Telegram"))
    box = _find_edit(hwnd, _INPUT_NAMES)
    if box is None:
        # A channel or a read-only group: typing would go into the search box or nowhere.
        raise SkillError(t("cant_write_here", chat=title))
    if _draft(box):
        # Something is already in the box (a draft, or a message being edited): sending
        # now would send that too.
        raise SkillError(t("draft_in_chat", chat=title))
    _wait_keys_released()
    box.Click(simulateMove=False, waitTime=0.1)
    _typing(text, hwnd)
    time.sleep(0.2)
    if user32.GetForegroundWindow() != hwnd or open_chat_title(hwnd) != title:
        raise SkillError(t("chat_not_opened", to=to))  # typed, but not sent: nothing goes out
    keyboard.send("enter")
    if ctx.knowledge is not None and title not in _SAVED_TITLES:
        ctx.knowledge.add_alias(title, to)  # "вове" -> this chat, next time without searching
    ctx.last_action = {"type": "message", "chat": title}
    return t("message_sent", chat=title)