"""Text helpers for matching spoken Russian against app and button names."""

import re

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}

_GREETINGS = {"эй", "хей", "привет", "слушай", "окей", "ок", "hey", "ok", "okay", "hi", "hello", "嘿", "你好", "喂"}
_CJK = re.compile(r"[\u4e00-\u9fff]")
# Other ways Whisper writes a Chinese name: traditional characters, near-homophones.
_CJK_VARIANTS = {"诺娃": ("諾娃", "诺瓦", "諾瓦", "娜娃", "诺哇")}

_FILLERS =r"\b(пожалуйста|плиз|please|ну|давай|а|ка|мне|можешь|можно|can you|could you|would you)\b"
_CJK_FILLERS = re.compile(r"^(?:请|麻烦你?|帮我|帮忙|能不能|可以)+")


def translit(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())


def normalize(text: str) -> str:
    """Lowercase, drop punctuation and filler words, collapse spaces."""
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)  # zero-width marks in window titles
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s.+#-]", " ", text)
    text = re.sub(r"(?<!\w)[.-]|[.-](?!\w)", " ", text)  # keep dots only inside words (youtube.com)
    text = re.sub(_FILLERS, " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _CJK_FILLERS.sub("", text) if text[:1] and "\u4e00" <= text[0] <= "\u9fff" else text


def split_name(text: str, name: str, threshold: int = 75, is_command=None) -> str | None:
    """'Нова, открой хром' -> 'открой хром'; None if the name wasn't said.

    Fuzzy, because speech recognition often hears "Ново" or "Нову" for "Нова".
    A greeting before the name is allowed ("эй Нова ...").
    `is_command(rest)`: lets a name fused with a short command through ("Новая пауза"
    is how Whisper writes "Нова, пауза") while "новая версия" stays an ordinary phrase.
    """
    from rapidfuzz import fuzz

    if _CJK.search(name):
        return _split_cjk_name(text, name)
    words = normalize(text).split()
    name_words = normalize(name).split()
    n = len(name_words)
    if not n:
        return None
    starts = [0, 1] if words and words[0] in _GREETINGS else [0]
    for start in starts:
        head = " ".join(words[start:start + n])
        target = " ".join(name_words)
        # A longer word built around the name is a different word ("новая", "снова")...
        if len(head) > len(target) and target[:-1] in head:
            rest = " ".join(words[start + n:])
            # ...unless it is the name glued to its ending and a real command follows.
            fused = head.startswith(target[:-1]) and len(head) - len(target) <= 2
            if fused and rest and is_command is not None and is_command(rest):
                return rest
            continue
        # Speech recognition often bends the last vowel ("Ново", "Нову" for "Нова"):
        # accept exactly those, not anything vaguely similar.
        bent = {target[:-1] + v for v in "аоуыеэи"} if len(target) >= 4 and target[-1] in "аоуыеэи" else set()
        # The same in Latin letters: the name "Nova" said in Russian comes out as "Ново", "Нову".
        t_target = translit(target)
        bent_latin = {t_target[:-1] + v for v in "aouyei"} if len(t_target) >= 4 and t_target[-1] in "aouyei" else set()
        if head in bent or translit(head) in bent_latin:
            return " ".join(words[start + n:])
        # Transliterated too, so "Нова" also answers to "Nova" said in English. A near
        # miss must still start with the name's sound: "Она", "Ова" are ordinary words.
        same_start = translit(head)[:1] == translit(target)[:1]
        if same_start and max(fuzz.ratio(head, target), fuzz.ratio(translit(head), translit(target))) >= threshold:
            return " ".join(words[start + n:])
    return None


def _split_cjk_name(text: str, name: str) -> str | None:
    """Chinese has no spaces: "诺娃打开微信" -> "打开微信"."""
    glued = re.sub(r"[\s\W_]+", "", text.lower())
    for greeting in ("你好", "okay", "hello", "hey", "嘿", "喂", "hi", "ok"):  # longest first
        if glued.startswith(greeting) and len(glued) > len(greeting):
            glued = glued[len(greeting):]
            break
    for form in (name, *_CJK_VARIANTS.get(name, ()), translit(name), "nova"):
        if form and glued.startswith(form.lower()):
            rest = text[text.lower().find(form.lower()) + len(form):] if form.lower() in text.lower() else glued[len(form):]
            return normalize(rest)
    return None


def as_said(raw: str, words: str) -> str:
    """Find normalized words ("привет мир") in the raw phrase and return them as
    spoken ("Привет, мир!") — the instant path only sees normalized text."""
    parts = normalize(words).split()
    if not raw or not parts:
        return words
    # Search the phrase as said, with е matching ё too, so "Ёлка зелёная" keeps its letters.
    pattern = r"[\W_]*".join(re.escape(p).replace("е", "[её]") for p in parts) + r"[^\s]*"
    m = re.search(pattern, raw, re.I)
    return m.group(0).strip(" «»\"“”") if m else words


def strip_name_raw(text: str, name: str) -> str:
    """Like strip_name, but keeps the rest exactly as said: "Нова, напиши Привет!" -> "напиши Привет!"."""
    if _CJK.search(name):
        for form in (name, *_CJK_VARIANTS.get(name, ())):
            m = re.match(rf"^\W*(?:(?:嘿|你好|喂|hey|hi)\W*)?{re.escape(form)}[\s,，。！？!?:：、]*", text, re.I)
            if m:
                return text[m.end():] or text
        return text.strip()
    words = text.split()
    n = max(1, len(normalize(name).split()))
    for cut in (n, n + 1):  # the name, or a greeting + the name
        head = " ".join(words[:cut])
        if split_name(head, name) == "":
            return " ".join(words[cut:]).lstrip(",.!:;—- ") or text
    return text.strip()


def strip_name(text: str, name: str) -> str:
    """Remove the assistant's name from the start of a phrase, if it is there."""
    rest = split_name(text, name)
    return rest if rest is not None else normalize(text)
