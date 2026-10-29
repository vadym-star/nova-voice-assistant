"""Volume, media keys, screenshots, web search, time, lock."""

import datetime
import os
import re
import logging
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path

import keyboard

from ..i18n import say_date, t
from ..text import normalize
from . import SkillError, skill

log = logging.getLogger(__name__)

_WORD_NUMBERS = {
    "ноль": 0, "десять": 10, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100,
    "половину": 50, "половина": 50, "максимум": 100, "на максимум": 100,
    "zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "half": 50, "max": 100, "maximum": 100,
    "一半": 50, "最大": 100,
}
_MUTE = {"выключи звук", "без звука", "мут", "выключи", "mute", "静音"}
_UNMUTE = {"включи звук", "верни звук", "включи", "unmute", "取消静音"}
_LOUDER = {"громче", "погромче", "louder", "volume up", "turn it up", "大声点", "声音大一点"}
_QUIETER = {"тише", "потише", "quieter", "volume down", "turn it down", "小声点", "声音小一点"}
_YOUTUBE = ("ютуб", "youtube", "油管")


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_number(text: str) -> int | None:
    """三十 -> 30, 三十五 -> 35, 一百 -> 100, 五 -> 5; None if it isn't one."""
    if not text or any(ch not in _CN_DIGITS and ch not in "十百" for ch in text):
        return None
    if text == "一百" or text == "百":
        return 100
    if "十" in text:
        tens, _, ones = text.partition("十")
        return _CN_DIGITS.get(tens, 1) * 10 + _CN_DIGITS.get(ones, 0)
    return _CN_DIGITS.get(text) if len(text) == 1 else None


def _volume():
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


@skill(
    "volume",
    "Изменить громкость звука: число 0-100, 'громче', 'тише', 'выключи', 'включи'.",
    patterns=(
        r"^(?:сделай\s+)?(?:громкость|звук)\s+(?:на\s+)?(?P<level>\d{1,3}|\w+)(?:\s*процент\w*)?$",
        r"^(?:сделай\s+)?(?P<level>громче|тише|потише|погромче)$",
        r"^(?P<level>выключи звук|без звука|включи звук|мут|верни звук)$",
        r"^(?P<level>louder|quieter|volume up|volume down|turn it up|turn it down|mute|unmute|"
        r"turn (?:the )?volume (?:up|down)|turn (?:up|down) the volume)$",
        r"^(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(?P<level>\d{1,3}|\w+)(?:\s*percent)?$",
        r"^(?:把)?音量(?:调到|调成|设为|设置为|调至)?\s*(?:百分之)?(?P<level>\d{1,3}|[零一二两三四五六七八九十百]+|最大|最小|一半)\s*%?$",
        r"^(?P<level>大声点|小声点|大声一点|小声一点|声音大一点|声音小一点|调大音量|调小音量|静音|取消静音)$",
    ),
    params={"level": "число 0-100, 'громче', 'тише', 'выключи звук' или 'включи звук'"},
)
def volume(ctx, level: str):
    lvl = normalize(str(level))
    vol = _volume()
    if lvl in _MUTE:
        vol.SetMute(1, None)
        return None
    if lvl in _UNMUTE:
        vol.SetMute(0, None)
        return None
    current = vol.GetMasterVolumeLevelScalar()
    if lvl in _LOUDER:
        target = min(1.0, current + 0.1)
    elif lvl in _QUIETER:
        target = max(0.0, current - 0.1)
    elif lvl.isdigit():
        target = min(100, int(lvl)) / 100
    elif (n := _chinese_number(lvl)) is not None:
        target = min(100, n) / 100
    elif lvl in _WORD_NUMBERS:
        target = _WORD_NUMBERS[lvl] / 100
    else:
        raise SkillError(t("volume_what"))
    vol.SetMute(0, None)
    vol.SetMasterVolumeLevelScalar(target, None)
    return None


@skill(
    "media",
    "Управление музыкой/видео: пауза, играть, следующий, предыдущий трек.",
    patterns=(
        r"^(?P<action>пауза|поставь на паузу|стоп|останови музыку|продолжи|играй|"
        r"следующий трек|следующая|следующую|предыдущий трек|предыдущая)$",
        r"^(?P<action>pause|pause the music|play|resume|stop the music|stop music|next|next track|next song|skip|"
        r"previous|previous track|previous song)$",
        r"^(?P<action>暂停|播放|继续|继续播放|下一首|上一首)$",
    ),
    params={"action": "'пауза', 'играй', 'следующий' или 'предыдущий'"},
)
def media(ctx, action: str):
    a = normalize(action)
    if a.startswith(("след", "next", "skip", "下一")):
        keyboard.send("next track")
    elif a.startswith(("пред", "prev", "上一")):
        keyboard.send("previous track")
    else:
        keyboard.send("play/pause media")
    return None


@skill(
    "screenshot",
    "Сделать скриншот экрана и сохранить в Изображения\\Nova.",
    patterns=(
        r"^(?:сделай\s+)?(?:скриншот|скрин|снимок экрана|сфоткай экран|заскринь)(?:\s+экрана)?$",
        r"^(?:take\s+a\s+)?(?:screenshot|screen shot|screen capture)$",
        r"^(?:截图|截屏|屏幕截图|截个图)$",
    ),
)
def screenshot(ctx):
    import mss
    import mss.tools

    folder = Path.home() / "Pictures" / "Nova"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / datetime.datetime.now().strftime("screenshot_%Y-%m-%d_%H-%M-%S.png")
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])  # all monitors
        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
    ctx.extra["last_screenshot"] = str(path)
    return t("screenshot_done")


_FILE_SEARCH = re.compile(r"^(?:файл|файлы|документ|документы|папку|папка|file|files|document|folder|文件)\s+(?P<q>.+)$")


@skill(
    "search",
    "Поиск в интернете (Google) или на YouTube.",
    patterns=(
        r"^(?:найди|загугли|погугли|поищи|ищи)\s+(?:в|на)\s+(?P<where>ютубе|youtube)\s+(?P<query>.+)$",
        r"^(?:найди|загугли|погугли|поищи|ищи)\s+(?P<query>.+?)(?:\s+(?:в|на)\s+(?P<where>ютубе|youtube))?$",
        r"^(?:включи|поставь)\s+(?:на\s+ютубе\s+)?(?P<query>.+?)\s+(?:на|в)\s+(?P<where>ютубе)$",
        r"^(?:search|find|look up)\s+(?:on\s+)?(?P<where>youtube)\s+(?:for\s+)?(?P<query>.+)$",
        r"^(?:search\s+for|search|google|look\s+up|find)\s+(?P<query>.+?)(?:\s+on\s+(?P<where>youtube))?$",
        r"^(?:play)\s+(?P<query>.+?)\s+on\s+(?P<where>youtube)$",
        r"^(?:在)?(?P<where>youtube|油管)(?:上)?(?:搜索|搜|找)\s*(?P<query>.+)$",
        r"^(?:搜索|搜一下|搜|查一下|查找)\s*(?P<query>.+)$",
    ),
    params={"query": "что искать", "where": "'google' (по умолчанию) или 'youtube'"},
)
def search(ctx, query: str, where: str | None = None):
    if (m := _FILE_SEARCH.match(normalize(query))) and not where:
        # "найди файл отчёт": Windows search on this PC, not Google.
        os.startfile("search-ms:query=" + urllib.parse.quote(m["q"]))
        return None
    q = urllib.parse.quote_plus(query)
    if where and any(y in where.lower() for y in _YOUTUBE):
        webbrowser.open(f"https://www.youtube.com/results?search_query={q}")
    else:
        webbrowser.open(f"https://www.google.com/search?q={q}")
    return None


_YT = r"(?:ютубе|ютуб|ютьюбе|youtube|ютубчике)"


def first_video(query: str) -> str | None:
    """The first real video (not an ad or a Short) YouTube finds for `query`."""
    import urllib.request

    request = urllib.request.Request(
        "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query),
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru,en;q=0.8"})
    try:
        html = urllib.request.urlopen(request, timeout=5).read().decode("utf-8", "replace")
    except OSError as e:
        log.info("YouTube search failed: %s", e)
        return None
    m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', html)
    return m.group(1) if m else None


@skill(
    "play_video",
    "Включить видео, песню или клип на YouTube: сразу запускает первый подходящий ролик "
    "(не просто поиск). Используй для «включи/поставь видео …», «включи песню …», «play … on YouTube».",
    patterns=(
        r"^(?:включи|поставь|запусти|покажи|открой|найди и включи)\s+(?:на\s+" + _YT + r"\s+)?"
        r"(?:видео|видос|видосик|ролик|клип|песню|трек|серию|мультик|выпуск)\s+(?P<query>.+?)"
        r"(?:\s+(?:на|в|из)\s+" + _YT + r")?$",
        r"^(?:включи|поставь|запусти)\s+на\s+" + _YT + r"\s+(?P<query>.+)$",
        r"^(?:включи|поставь|запусти)\s+(?P<query>.+?)\s+(?:на|в)\s+" + _YT + r"$",
        r"^play\s+(?:the\s+|a\s+)?(?:video|song|clip|track|music video)\s+(?:of\s+|by\s+)?(?P<query>.+?)"
        r"(?:\s+on\s+youtube)?$",
        r"^play\s+(?P<query>.+?)\s+on\s+youtube$",
        r"^(?:在\s*)?(?:youtube|油管)\s*上?\s*播放\s*(?P<query>.+)$",
        r"^播放\s*(?P<query>.+?)\s*的?(?:视频|歌曲|歌|mv)$",
    ),
    params={"query": "что включить, как сказал пользователь"},
    priority=1,
)
def play_video(ctx, query: str):
    video = first_video(query)
    if video:
        webbrowser.open(f"https://www.youtube.com/watch?v={video}")
    else:  # offline or YouTube changed its page: at least show the results
        webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
    return None


@skill(
    "time",
    "Сказать текущее время.",
    patterns=(
        r"^(?:сколько\s+(?:сейчас\s+)?времени|который\s+час|время)$",
        r"^(?:what\s+time\s+is\s+it|what\s*s\s+the\s+time|the\s+time|time)(?:\s+now)?$",
        # 几/击/机 are all "jī": recognition sometimes picks the wrong one.
        r"^(?:现在)?(?:[几击机]点(?:了|钟)?|什么时间|几点了)$",
    ),
)
def tell_time(ctx):
    now = datetime.datetime.now()
    return f"{now.hour}:{now.minute:02d}"


@skill(
    "date",
    "Сказать сегодняшнюю дату и день недели.",
    patterns=(
        r"^(?:какое\s+)?(?:сегодня\s+)?(?:число|дата|какой\s+сегодня\s+день)(?:\s+сегодня)?$",
        r"^(?:what\s+(?:is\s+|s\s+)?(?:the\s+)?date(?:\s+today)?|what\s+day\s+is\s+(?:it|today)(?:\s+today)?|"
        r"today\s*s\s+date)$",
        r"^(?:今天)?(?:几号|星期几|什么日子|日期)$",
    ),
)
def tell_date(ctx):
    return say_date(datetime.date.today())


@skill(
    "lock",
    "Заблокировать компьютер.",
    patterns=(
        r"^(?:заблокируй|заблокировать|блокируй)(?:\s+(?:компьютер|комп|пк|экран))?$",
        r"^lock(?:\s+(?:the\s+|my\s+)?(?:computer|pc|screen))?$",
        r"^(?:锁屏|锁定电脑|锁定屏幕|锁电脑)$",
    ),
)
def lock(ctx):
    subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
    return None


@skill(
    "power",
    "Выключить, перезагрузить компьютер или отправить в сон. Спрашивает подтверждение.",
    patterns=(
        r"^(?P<action>выключи|перезагрузи|усыпи)\s+(?:компьютер|комп|пк)$",
        r"^(?P<action>спящий режим)$",
        r"^(?P<action>shut down|shutdown|turn off|restart|reboot|put to sleep)\s+(?:the\s+)?(?:computer|pc)$",
        r"^(?P<action>关机|重启|睡眠)$",
    ),
    params={"action": "'выключи', 'перезагрузи' или 'усыпи'"},
    dangerous=True,
)
def power(ctx, action: str):
    a = normalize(action)
    if a.startswith(("выкл", "shut", "turn off", "关机")):
        os.system("shutdown /s /t 5")
        return t("shutting_down")
    if a.startswith(("перезаг", "restart", "reboot", "重启")):
        os.system("shutdown /r /t 5")
        return t("rebooting")
    subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    return None


@skill(
    "wait",
    "Подождать несколько секунд (0.2-5), например пока откроется программа или загрузится окно.",
    params={"seconds": "число секунд"},
)
def wait(ctx, seconds=1):
    import time

    time.sleep(min(5.0, max(0.2, float(seconds))))
    return None


@skill(
    "confirm",
    "Спросить пользователя «да или нет» голосом перед необратимым действием, которое затрагивает других "
    "людей (отправка сообщения, письма, публикация, оплата). Если ответ «нет» — задача останавливается.",
    params={"question": "короткий вопрос, например «Отправить Вове: Привет, я Нова?»"},
)
def confirm(ctx, question: str):
    if not ctx.confirm(question):  # always asked: the AI only adds this for irreversible steps
        raise SkillError(t("cancelled"))
    return None