"""Phrases Nova says, per reply language.

To add a language: add its code to LANGUAGES, a column to every entry in
MESSAGES (missing ones fall back to English),
and UI strings in web/i18n.js.
"""

import datetime
import re

LANGUAGES = {"ru": "Русский", "en": "English", "zh": "中文"}

# The assistant's default name in each language. A name the user picked is never changed.
DEFAULT_NAMES = {"ru": "Нова", "en": "Nova", "zh": "诺娃"}

# Language names as the AI should see them in "reply in ..." instructions.
AI_LANGUAGE = {"ru": "Russian", "en": "English", "zh": "Simplified Chinese"}

MESSAGES = {
    "listening": {"ru": "Слушаю", "en": "I'm listening", "zh": "我在听"},
    "speak_now": {"ru": "Говорите…", "en": "Go ahead…", "zh": "请说…"},
    "nothing_heard": {"ru": "Ничего не слышно", "en": "I didn't hear anything", "zh": "没有听到声音"},
    "ready": {"ru": "{name} на связи · {hotkey}", "en": "{name} is ready · {hotkey}", "zh": "{name} 已就绪 · {hotkey}"},
    "loading_stt": {"ru": "Загружаю распознавание речи…", "en": "Loading speech recognition…", "zh": "正在加载语音识别…"},
    "not_understood": {"ru": "Не понимаю команду", "en": "I don't understand", "zh": "我不明白这个指令"},
    "cancelled": {"ru": "Отменено", "en": "Cancelled", "zh": "已取消"},
    "confirm": {"ru": "Это действие нельзя отменить. Выполнить? Да или нет?",
                "en": "This can't be undone. Go ahead? Yes or no?",
                "zh": "此操作无法撤销。要继续吗？是还是否？"},
    "cant_do": {"ru": "Не умею {name}", "en": "I can't do {name}", "zh": "我不会 {name}"},
    "failed": {"ru": "Не получилось выполнить {name}", "en": "Couldn't run {name}", "zh": "无法执行 {name}"},
    "error": {"ru": "Что-то пошло не так", "en": "Something went wrong", "zh": "出了点问题"},
    "hello": {"ru": "Привет! Я {name}.", "en": "Hi! I'm {name}.", "zh": "你好！我是{name}。"},
    "open_found": {"ru": "Нашла «{name}». Запустить? Да или нет?", "en": "Found “{name}”. Start it? Yes or no?",
                   "zh": "找到了“{name}”。要启动吗？是还是否？"},
    "stt_failed": {"ru": "Эта модель распознавания не загрузилась — вернула прежнюю",
                   "en": "That recognition model didn't load, so I went back to the previous one",
                   "zh": "该识别模型无法加载，已恢复为之前的模型"},
    "field_not_found": {"ru": "Не нахожу поле «{field}»", "en": "I can't find the “{field}” field",
                        "zh": "找不到“{field}”字段"},
    "window_changed": {"ru": "Окно сменилось, пока я искала — ничего не нажимаю",
                       "en": "The window changed while I was looking, so I didn't click",
                       "zh": "查找时窗口发生了变化，所以我没有点击"},
    "cant_write_here": {"ru": "В чат «{chat}» писать нельзя", "en": "You can't write in “{chat}”",
                        "zh": "无法在“{chat}”中发言"},
    "draft_in_chat": {"ru": "В чате «{chat}» уже есть неотправленный текст — не трогаю его",
                      "en": "There's unsent text in “{chat}” already, so I left it alone",
                      "zh": "“{chat}”中已有未发送的文字，我没有动它"},
    "ai_limit": {"ru": "Лимит подписки исчерпан, попробуйте позже", "en": "The subscription limit is reached, try later",
                 "zh": "订阅额度已用完，请稍后再试"},
    "no_internet": {"ru": "Нет связи с ИИ — проверьте интернет", "en": "Can't reach the AI — check the internet",
                    "zh": "无法连接 AI，请检查网络"},
    "claude_outdated": {"ru": "Claude Code устарел — обновите его (Настройки → Интеллект → Установить)",
                        "en": "Claude Code is out of date — update it (Settings → Intelligence → Install)",
                        "zh": "Claude Code 版本过旧，请更新（设置 → 智能 → 安装）"},
    "no_microphone": {"ru": "Микрофон не отвечает. Проверьте, что он подключён и что Windows разрешает доступ к нему",
                      "en": "The microphone isn't responding. Check it's plugged in and that Windows allows access to it",
                      "zh": "麦克风无响应。请检查是否已连接，以及 Windows 是否允许访问麦克风"},
    "not_finished": {"ru": "Не успела довести дело до конца — повторите или скажите по шагам",
                     "en": "I couldn't finish that — try again or say it step by step",
                     "zh": "没能完成——请重试，或分步骤告诉我"},
    "confirm_needs_quiet": {"ru": "Пока играют колонки, голосом это не подтвердить. Сделайте тише и повторите",
                            "en": "I can't take a spoken yes while the speakers are playing. Turn them down and try again",
                            "zh": "扬声器正在播放，无法用语音确认。请调小音量后重试"},
    "black_capture": {"ru": "Игра в полноэкранном режиме не даёт себя увидеть — переключите её на «окно без рамки»",
                      "en": "The game is in exclusive fullscreen and can't be seen — switch it to borderless window",
                      "zh": "游戏处于独占全屏模式，无法截图——请切换为无边框窗口"},
    # protection
    "private_window": {"ru": "Это окно с паролями — его я не смотрю и никуда не отправляю",
                       "en": "That window holds passwords, so I don't look at it or send it anywhere",
                       "zh": "这是密码窗口，我不会查看或发送它"},
    "confirm_action_heard": {"ru": "Звук идёт из колонок — команда точно ваша? {what}. Да или нет?",
                             "en": "Sound is playing from the speakers — was that really you? {what}. Yes or no?",
                             "zh": "扬声器正在播放声音，这真的是你的指令吗？{what}。是还是否？"},
    "anticheat_blocked": {"ru": "Открыта игра с античитом — нажимать клавиши и кликать не буду, чтобы не было бана",
                          "en": "A game with anti-cheat is running, so I won't press keys or click: that could get you banned",
                          "zh": "正在运行带反作弊的游戏，为避免封号，我不会模拟按键或点击"},
    "elevated_window": {"ru": "Это окно запущено от администратора — здесь шорткат не работает. "
                              "Позовите по имени или включите «Запуск от администратора» в настройках",
                        "en": "This window runs as administrator, so the shortcut can't reach it. "
                              "Call me by name, or turn on “Run as administrator” in Settings",
                        "zh": "此窗口以管理员身份运行，快捷键在这里无效。请喊我的名字，或在设置中开启“以管理员身份运行”"},
    "elevated_failed": {"ru": "Не вышло: окно запущено от администратора. Включите «Запуск от администратора» в настройках",
                        "en": "Couldn't: that window runs as administrator. Turn on “Run as administrator” in Settings",
                        "zh": "无法完成：该窗口以管理员身份运行。请在设置中开启“以管理员身份运行”"},
    # macros
    "macro_saved": {"ru": "Команда «{name}» сохранена, шагов: {n}", "en": "Saved “{name}”, {n} steps",
                    "zh": "已保存“{name}”，共 {n} 步"},
    "macro_hint": {"ru": "Скажите так: запомни название, двоеточие, и что делать",
                   "en": "Say: remember, a name, a colon, then what to do",
                   "zh": "请这样说：记住 名称：要做的事"},
    "macro_what": {"ru": "Не понимаю, что запомнить", "en": "I don't know what to remember", "zh": "不知道要记住什么"},
    "deleted": {"ru": "Удалено: {name}", "en": "Deleted: {name}", "zh": "已删除：{name}"},
    "no_command": {"ru": "Нет команды «{name}»", "en": "No command “{name}”", "zh": "没有“{name}”这个指令"},
    "no_commands": {"ru": "Пока нет своих команд", "en": "No custom commands yet", "zh": "还没有自定义指令"},
    "commands_count": {"ru": "Своих команд: {m}, выученных: {l}. Список — в настройках",
                       "en": "{m} custom and {l} learned commands. See Settings for the list",
                       "zh": "自定义指令 {m} 个，学会的指令 {l} 个。列表见设置"},
    "nothing_to_fix": {"ru": "Не понимаю, что исправить", "en": "I'm not sure what to fix", "zh": "不知道要改正什么"},
    "rule_forgotten": {"ru": "Понятно, больше так делать не буду", "en": "Got it, I won't do that again",
                       "zh": "明白了，下次不会这样做"},
    "messenger_unsupported": {"ru": "Этот навык — для Telegram; в других мессенджерах действую по шагам",
                              "en": "This skill is for Telegram; in other messengers I work step by step",
                              "zh": "此技能用于 Telegram；在其他通讯软件中我会分步骤操作"},
    "chat_not_found": {"ru": "Не нахожу чат «{to}»", "en": "I can't find a chat for “{to}”", "zh": "找不到“{to}”的聊天"},
    "chat_not_opened": {"ru": "Не получилось открыть чат «{to}», ничего не отправляю",
                        "en": "Couldn't open the chat “{to}”, nothing sent", "zh": "无法打开“{to}”的聊天，未发送"},
    "send_confirm": {"ru": "Открыт чат {chat}. Отправить: «{text}»? Да или нет?",
                     "en": "The chat with {chat} is open. Send: “{text}”? Yes or no?",
                     "zh": "已打开与 {chat} 的聊天。发送“{text}”吗？是还是否？"},
    "message_sent": {"ru": "Отправлено: {chat}", "en": "Sent to {chat}", "zh": "已发送给 {chat}"},
    "is_this_contact": {"ru": "«{to}» — это {title}?", "en": "Is “{to}” {title}?", "zh": "“{to}”是 {title} 吗？"},
    "who_is": {"ru": "Как {to} записан в Телеграме?", "en": "What's {to}'s name in Telegram?",
               "zh": "{to} 在 Telegram 里叫什么名字？"},
    "fact_saved": {"ru": "Запомнено", "en": "Got it, I'll remember", "zh": "记住了"},
    "fact_forgotten": {"ru": "Забыто", "en": "Forgotten", "zh": "已忘记"},
    "nothing_to_forget": {"ru": "Я такого не помню", "en": "I don't remember that", "zh": "我不记得这个"},
    "cant_focus": {"ru": "Windows не дал переключиться на {app}, ничего не печатаю",
                   "en": "Windows wouldn't let me switch to {app}, so I'm not typing anything",
                   "zh": "Windows 不允许切换到 {app}，未输入任何内容"},
    "confirm_action": {"ru": "Выполнить: {what}? Да или нет?", "en": "Do this: {what}? Yes or no?",
                       "zh": "要执行：{what}吗？是还是否？"},
    "boot_offline": {"ru": "Нужен интернет, чтобы один раз скачать модели речи (~550 МБ). Попробую снова через минуту",
                     "en": "Nova needs the internet once to download its speech models (~550 MB). Trying again in a minute",
                     "zh": "首次需要联网下载语音模型（约 550 MB）。一分钟后重试"},
    "boot_failed": {"ru": "Не удалось запустить распознавание речи. Подробности в nova.log",
                    "en": "Speech recognition failed to start. See nova.log",
                    "zh": "语音识别启动失败。详情见 nova.log"},
    "bad_name": {"ru": "Имя должно быть не короче трёх букв и не обычным словом вроде «да» или «ок»",
                 "en": "The name needs at least three letters and can't be an everyday word like “yes” or “ok”",
                 "zh": "名称至少需要两个汉字或三个字母，且不能是“是”“好”这类常用词"},
    "bad_hotkey": {"ru": "Такой шорткат не подходит, оставлен прежний",
                   "en": "That shortcut doesn't work, kept the old one", "zh": "该快捷键无效，已保留原快捷键"},
    "settings_title": {"ru": "Nova — настройки", "en": "Nova — Settings", "zh": "Nova — 设置"},
    "tray_settings": {"ru": "Настройки", "en": "Settings", "zh": "设置"},
    "tray_quit": {"ru": "Выход", "en": "Quit", "zh": "退出"},
    # skills
    "app_not_found": {"ru": "Не могу найти программу «{target}»", "en": "Can't find the app “{target}”",
                      "zh": "找不到程序“{target}”"},
    "opening": {"ru": "Открываю {target}", "en": "Opening {target}", "zh": "正在打开{target}"},
    "which_window": {"ru": "Не понимаю, какое окно закрыть", "en": "Which window should I close?",
                     "zh": "要关闭哪个窗口？"},
    "window_not_found": {"ru": "Не вижу окно «{target}»", "en": "I don't see a “{target}” window",
                         "zh": "没有看到“{target}”窗口"},
    "not_on_screen": {"ru": "Не вижу «{target}» на экране", "en": "I don't see “{target}” on screen",
                      "zh": "屏幕上没有看到“{target}”"},
    "unknown_key": {"ru": "Не знаю клавишу «{key}»", "en": "Unknown key “{key}”", "zh": "不认识按键“{key}”"},
    "volume_what": {"ru": "Не понимаю, какую громкость поставить", "en": "What volume should I set?",
                    "zh": "要调到多大音量？"},
    "screenshot_done": {"ru": "Скриншот готов", "en": "Screenshot saved", "zh": "截图已保存"},
    "shutting_down": {"ru": "Выключаю", "en": "Shutting down", "zh": "正在关机"},
    "rebooting": {"ru": "Перезагружаю", "en": "Restarting", "zh": "正在重启"},
    # AI
    "ai_off": {"ru": "ИИ выключен в настройках", "en": "AI is turned off in Settings", "zh": "AI 已在设置中关闭"},
    "ai_slow": {"ru": "ИИ слишком долго думает", "en": "The AI is taking too long", "zh": "AI 响应超时"},
    "claude_missing": {"ru": "Claude Code не установлен. Откройте настройки, раздел «Интеллект»",
                       "en": "Claude Code isn't installed. Open Settings → Intelligence",
                       "zh": "未安装 Claude Code。请打开设置 → AI"},
    "claude_login": {"ru": "Нужно войти в Claude. Откройте настройки, раздел «Интеллект»",
                     "en": "Please sign in to Claude in Settings → AI", "zh": "请在设置 → AI 中登录 Claude"},
    "claude_no_answer": {"ru": "Claude не ответил", "en": "Claude didn't answer", "zh": "Claude 没有回应"},
    "codex_missing": {"ru": "Codex не установлен. Откройте настройки, раздел «Интеллект»",
                      "en": "Codex isn't installed. Open Settings → Intelligence", "zh": "未安装 Codex。请打开设置 → AI"},
    "codex_no_answer": {"ru": "ChatGPT не ответил. Проверьте вход в настройках",
                        "en": "ChatGPT didn't answer. Check sign-in in Settings",
                        "zh": "ChatGPT 没有回应。请在设置中检查登录"},
}

# Words that mean "yes" when Nova asks for confirmation.
YES_WORDS = {"да", "ага", "конечно", "давай", "угу", "верно", "точно", "отправляй", "отправь", "го",
             "yes", "yeah", "yep", "sure", "ok", "okay", "send", "go", "alright", "course", "yup",
             "是", "是的", "好", "好的", "对", "可以", "确定", "发吧", "发送"}
# Chinese yes-words at the start of the answer ("好啊", "是的发吧", "行").
_CJK_YES = ("是", "好", "对", "可以", "确定", "发", "行", "嗯", "当然", "没问题")
# Yeses that contain a negation word: "no problem", "没问题".
_YES_PHRASES = re.compile(r"\bno problem\b|没问题|不错", re.I)

# Any of these makes the answer a "no", even next to a yes-word ("да нет, не надо", "不是").
_NEGATIONS = re.compile(r"\b(?:нет|не|неа|отмена|стоп|no|nope|not|don\W?t|cancel|stop)\b|[不别没否]", re.I)


def is_yes(answer: str) -> bool:
    """Strict: a clear yes and no negation anywhere. Silence or doubt means no."""
    text = answer.lower().replace("ё", "е")
    if _YES_PHRASES.search(text) and not _NEGATIONS.search(_YES_PHRASES.sub(" ", text)):
        return True
    if not text.strip() or _NEGATIONS.search(text):
        return False
    words = re.findall(r"\w+", text)
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", text)
    return any(w in YES_WORDS for w in words) or (bool(cjk) and cjk.startswith(_CJK_YES))


_lang = "ru"


def set_language(code: str) -> None:
    global _lang
    _lang = code if code in LANGUAGES else "en"


def language() -> str:
    return _lang


def t(key: str, **kw) -> str:
    entry = MESSAGES[key]
    return entry.get(_lang, entry["en"]).format(**kw)


_RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
_RU_DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]
_EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_ZH_DAYS = "一二三四五六日"


def say_date(d: datetime.date) -> str:
    if _lang == "ru":
        return f"Сегодня {_RU_DAYS[d.weekday()]}, {d.day} {_RU_MONTHS[d.month - 1]}"
    if _lang == "zh":
        return f"今天是{d.month}月{d.day}日，星期{_ZH_DAYS[d.weekday()]}"
    return f"Today is {_EN_DAYS[d.weekday()]}, {_EN_MONTHS[d.month - 1]} {d.day}"
