"""Open and close apps, sites and folders."""

import json
import logging
import os
import re
import subprocess
import threading
import webbrowser
from pathlib import Path

from rapidfuzz import fuzz, process

from ..i18n import t
from ..text import normalize, translit
from . import SkillError, skill

log = logging.getLogger(__name__)

# Spoken name -> what to look for in the Start menu.
APP_ALIASES = {
    "微信": "wechat", "威信": "wechat", "weixin": "wechat",
    "хром": "google chrome", "гугл хром": "google chrome", "браузер": "google chrome",
    "эдж": "microsoft edge", "едж": "microsoft edge", "фаерфокс": "firefox", "файрфокс": "firefox",
    "яндекс": "yandex", "опера": "opera",
    "телеграм": "telegram", "телеграмм": "telegram", "тг": "telegram",
    "дискорд": "discord", "стим": "steam", "спотифай": "spotify", "зум": "zoom",
    "вотсап": "whatsapp", "ватсап": "whatsapp", "скайп": "skype", "слак": "slack",
    "блокнот": "notepad", "калькулятор": "calculator", "параметры": "settings",
    "настройки": "settings", "проводник": "file explorer", "терминал": "terminal",
    "ворд": "word", "эксель": "excel", "поверпоинт": "powerpoint", "аутлук": "outlook",
    "вс код": "visual studio code", "вскод": "visual studio code", "визуал студио код": "visual studio code",
    "код": "visual studio code", "пайчарм": "pycharm", "фотошоп": "photoshop",
    "обс": "obs studio", "диспетчер задач": "task manager", "паинт": "paint", "пейнт": "paint",
    "роблокс": "roblox player", "роблокс студио": "roblox studio", "майнкрафт": "minecraft",
    # English
    "chrome": "google chrome", "browser": "google chrome", "edge": "microsoft edge",
    "vs code": "visual studio code", "vscode": "visual studio code", "explorer": "file explorer",
    "files": "file explorer", "task manager": "task manager",
    # 中文
    "浏览器": "google chrome", "谷歌浏览器": "google chrome", "记事本": "notepad", "计算器": "calculator",
    "设置": "settings", "资源管理器": "file explorer", "文件资源管理器": "file explorer",
    "任务管理器": "task manager", "终端": "terminal", "微信": "wechat", "画图": "paint",
}

# Commands that the Start menu index does not contain.
BUILTIN = {
    "file explorer": "explorer.exe",
    "task manager": "taskmgr.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "settings": "ms-settings:",
    "terminal": "wt.exe",
}

SITES = {
    "ютуб": "https://youtube.com", "youtube": "https://youtube.com",
    "гугл": "https://google.com", "почту": "https://mail.google.com", "почта": "https://mail.google.com",
    "гмейл": "https://mail.google.com", "вк": "https://vk.com", "вконтакте": "https://vk.com",
    "гитхаб": "https://github.com", "чат гпт": "https://chatgpt.com", "чатгпт": "https://chatgpt.com",
    "клод": "https://claude.ai", "тикток": "https://tiktok.com", "инстаграм": "https://instagram.com",
    "твиттер": "https://x.com", "реддит": "https://reddit.com", "твич": "https://twitch.tv",
    "википедию": "https://wikipedia.org", "википедия": "https://wikipedia.org",
    "карты": "https://maps.google.com", "переводчик": "https://translate.google.com",
    "кинопоиск": "https://kinopoisk.ru", "авито": "https://avito.ru", "озон": "https://ozon.ru",
    "google": "https://google.com", "gmail": "https://mail.google.com", "github": "https://github.com",
    "chatgpt": "https://chatgpt.com", "reddit": "https://reddit.com", "twitch": "https://twitch.tv",
    "油管": "https://youtube.com", "谷歌": "https://google.com", "百度": "https://baidu.com",
    "b站": "https://bilibili.com", "哔哩哔哩": "https://bilibili.com", "知乎": "https://zhihu.com",
    "淘宝": "https://taobao.com", "微博": "https://weibo.com",
}

_HOME = Path.home()
def _known_folder(guid: str, fallback: str) -> Path:
    """Where Windows really keeps Desktop, Documents... (OneDrive backup moves them)."""
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_byte * 16)]

    path = ctypes.c_wchar_p()
    try:
        g = GUID()
        ctypes.memmove(g.data, uuid.UUID(guid).bytes_le, 16)
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(path)) == 0:
            result = Path(path.value)
            ctypes.windll.ole32.CoTaskMemFree(path)
            return result
    except (AttributeError, OSError, ValueError):
        pass
    return _HOME / fallback


_DOWNLOADS = _known_folder("374DE290-123F-4565-9164-39C4925E467B", "Downloads")
_DOCUMENTS = _known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7", "Documents")
_DESKTOP = _known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "Desktop")
_PICTURES = _known_folder("33E28130-4E1E-4676-835A-98395C3BC3BB", "Pictures")
_MUSIC = _known_folder("4BD8D571-6D19-48D3-BE97-422220080E43", "Music")
_VIDEOS = _known_folder("18989B1D-99B5-455B-841C-AB7C74E4DDFC", "Videos")

FOLDERS = {
    "загрузки": _DOWNLOADS, "документы": _DOCUMENTS, "рабочий стол": _DESKTOP, "картинки": _PICTURES,
    "изображения": _PICTURES, "фото": _PICTURES, "музыку": _MUSIC, "музыка": _MUSIC, "видео": _VIDEOS,
    "downloads": _DOWNLOADS, "documents": _DOCUMENTS, "desktop": _DESKTOP,
    "pictures": _PICTURES, "music": _MUSIC, "videos": _VIDEOS,
    "下载": _DOWNLOADS, "文档": _DOCUMENTS, "桌面": _DESKTOP, "图片": _PICTURES, "音乐": _MUSIC, "视频": _VIDEOS,
}


_NOT_GAMES = re.compile(r"redistributable|steamworks|proton|steam linux runtime|directx|vc\+\+|dedicated server",
                        re.I)


# Where a launcher-started game lives, so Nova can tell it's already running:
# "steam://rungameid/570" -> ...\steamapps\common\dota 2 beta
_INSTALL_DIRS: dict[str, Path] = {}


def _steam_games() -> dict[str, str]:
    """Installed Steam games from every library: name -> steam://rungameid/<id>."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return {}
    libraries = {steam}
    try:
        vdf = (steam / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        libraries |= {Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"(.+?)"', vdf)}
    except OSError:
        pass
    games = {}
    for library in libraries:
        for manifest in (library / "steamapps").glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name, appid = re.search(r'"name"\s+"(.+?)"', text), re.search(r'"appid"\s+"(\d+)"', text)
            if name and appid and not _NOT_GAMES.search(name[1]):
                url = f"steam://rungameid/{appid[1]}"
                games[name[1].lower()] = url
                if folder := re.search(r'"installdir"\s+"(.+?)"', text):
                    _INSTALL_DIRS[url] = library / "steamapps" / "common" / folder[1]
    return games


def _epic_games() -> dict[str, str]:
    """Installed Epic Games Store games: name -> launcher URL."""
    games = {}
    for item in Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                     r"Epic\EpicGamesLauncher\Data\Manifests").glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("DisplayName") and data.get("AppName"):
            url = f"com.epicgames.launcher://apps/{data['AppName']}?action=launch&silent=true"
            games[data["DisplayName"].lower()] = url
            if data.get("InstallLocation"):
                _INSTALL_DIRS[url] = Path(data["InstallLocation"])
    return games


def _desktop_shortcuts() -> dict[str, str]:
    """Shortcuts on the desktop (top level only: a desktop can hold huge folders)."""
    out = {}
    for desktop in (_DESKTOP, Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"):
        try:
            for f in desktop.iterdir():
                if f.suffix.lower() in (".lnk", ".url"):
                    out[f.stem.lower()] = str(f)
        except OSError:
            continue
    return out


# Start menu entries that aren't the program itself.
_NOT_LAUNCHABLE = re.compile(r"\b(?:help|uninstall|readme|read me|documentation|manual|website|license|"
                             r"release notes|справка|удал|документац)\b", re.I)
_NOT_APPS = re.compile(r"unins|setup|install|crash|report|redist|vc_?redist|dxsetup|helper|updat|elevat|"
                       r"service|repair|cleanup|diagnos|feedback|notif|telemetry|\bdriver|runtime|sdk\b|clicktorun|server|\bsrv\b|mcp", re.I)


def _registry_apps() -> dict[str, str]:
    """Programs Windows knows are installed: "Apps & features" and App Paths."""
    import winreg

    out: dict[str, str] = {}
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    uninstall = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                 r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    for hive in hives:
        for base in uninstall:
            try:
                root = winreg.OpenKey(hive, base)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                        values = {winreg.EnumValue(k, j)[0]: winreg.EnumValue(k, j)[1]
                                  for j in range(winreg.QueryInfoKey(k)[1])}
                except OSError:
                    continue
                name = str(values.get("DisplayName") or "")
                if not name or values.get("SystemComponent") == 1 or _NOT_APPS.search(name):
                    continue
                icon = str(values.get("DisplayIcon") or "").split(",")[0].strip().strip('"')
                if icon.lower().endswith(".exe") and not _NOT_APPS.search(Path(icon).name) and Path(icon).exists():
                    out.setdefault(name.lower(), icon)
                elif values.get("InstallLocation"):
                    exe = _main_exe(Path(str(values["InstallLocation"]).strip('"')), name, depth=1)
                    if exe:
                        out.setdefault(name.lower(), str(exe))
        try:
            paths = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths")
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(paths)[0]):
            try:
                exe_name = winreg.EnumKey(paths, i)
                target = winreg.QueryValue(paths, exe_name).strip('"')
            except OSError:
                continue
            if target.lower().endswith(".exe") and not _NOT_APPS.search(exe_name) and Path(target).exists():
                out.setdefault(Path(exe_name).stem.lower(), target)
    return out


def _main_exe(folder: Path, name: str, depth: int = 3, deadline: float = 0.0) -> Path | None:
    """The program's own .exe inside its install folder (not the uninstaller or a helper)."""
    import time

    best, best_score = None, 0.0
    candidates = []
    try:
        for pattern in ("*.exe", "*/*.exe", "*/*/*.exe", "*/*/*/*.exe")[:depth + 1]:
            for p in folder.glob(pattern):
                candidates.append(p)
                if len(candidates) >= 400 or (deadline and time.monotonic() > deadline):
                    break
            if deadline and time.monotonic() > deadline:
                break
    except OSError:
        return None
    for exe in candidates[:400]:
        if _NOT_APPS.search(exe.name):
            continue
        score = max(fuzz.WRatio(_sound(exe.stem), _sound(name)), fuzz.WRatio(_sound(exe.stem), _sound(folder.name)))
        score -= 3 * (len(exe.relative_to(folder).parts) - 1)  # prefer shallow
        if score > best_score:
            best, best_score = exe, score
    return best if best_score >= 60 else None


def _install_roots() -> list[Path]:
    """Where programs and games get installed, on every local drive. Never Downloads or
    Temp: whatever sits there was downloaded, not installed."""
    import ctypes
    import string

    roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"]
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        drive = f"{letter}:\\"
        if not mask >> i & 1 or ctypes.windll.kernel32.GetDriveTypeW(drive) != 3:  # fixed disks only
            continue
        for sub in ("Program Files", "Program Files (x86)", "Games", "Игры", "SteamLibrary\\steamapps\\common",
                    "Steam\\steamapps\\common", "Program Files (x86)\\Steam\\steamapps\\common",
                    "Epic Games", "Program Files\\Epic Games", "Riot Games", "GOG Games", "XboxGames",
                    "Battle.net", "Ubisoft", "EA Games"):
            p = Path(drive, sub)
            if p.is_dir():
                roots.append(p)
    return roots


def search_pc(spoken: str, budget_s: float = 4.0) -> tuple[str, str, int] | None:
    """Last resort for "открой X": look through install folders on every disk.
    Returns (name, exe, score) or None. Bounded in time: it runs while the user waits."""
    import time

    started = time.monotonic()
    queries = {_sound(q) for q in _spoken_forms(spoken)}
    folders: list[tuple[int, Path]] = []
    for root in _install_roots():
        try:
            for folder in root.iterdir():
                if folder.is_dir():
                    key = _sound(folder.name.lower())
                    score = max(fuzz.WRatio(q, key) for q in queries)
                    if score >= 86:
                        folders.append((score, folder))
        except OSError:
            continue
        if time.monotonic() - started > budget_s:
            break
    for score, folder in sorted(folders, key=lambda f: (-f[0], len(f[1].name))):
        if time.monotonic() - started > budget_s:
            break
        if exe := _main_exe(folder, folder.name, depth=3, deadline=started + budget_s):
            log.info("Found on disk: %s -> %s (%d)", spoken, exe, score)
            return folder.name, str(exe), int(score)
    return None


def _spoken_forms(spoken: str) -> set[str]:
    """"доту" -> also "дота": names are said in any grammatical case."""
    forms = {spoken}
    words = spoken.split()
    if words:
        last = words[-1]
        for ending, base in (("ую", "ая"), ("ой", "а"), ("ей", "я"), ("у", "а"), ("ю", "я"), ("е", "а"),
                             ("ы", "а"), ("и", "я")):
            if last.endswith(ending) and len(last) > len(ending) + 2:
                forms.add(" ".join(words[:-1] + [last[: -len(ending)] + base]))
    return forms | {translit(f) for f in forms}


def _found_before() -> dict[str, str]:
    from .. import config

    found = config.read_json(_FOUND_PATH, {})
    return {k: v for k, v in found.items() if "://" in v or Path(v).exists()} if isinstance(found, dict) else {}


def _found_path():
    from .. import config

    return config.DATA_DIR / "found_apps.json"


_FOUND_PATH = _found_path()

_SOUNDS = (("ck", "k"), ("c", "k"), ("ph", "f"), ("y", "i"), ("w", "v"), ("x", "ks"), ("q", "k"),
           ("ee", "i"), ("oo", "u"), ("kh", "h"))


def _sound(text: str) -> str:
    """A rough "how it sounds" key, so "киберпанк" (kiberpank) meets "Cyberpunk"."""
    key = translit(text)
    for a, b in _SOUNDS:
        key = key.replace(a, b)
    return key


_REFRESH_LOCK = threading.Lock()


class AppIndex:
    """Everything that can be started by name, loaded in the background: Start menu
    apps (Get-StartApps), Steam and Epic games, and desktop shortcuts."""

    def __init__(self):
        self.apps: dict[str, str] = {}  # lowercase name -> AppID
        self._ready = threading.Event()
        self._loaded_at = 0.0
        threading.Thread(target=self._load, daemon=True).start()

    def refresh(self, older_than: float = 60.0) -> None:
        """Something was just installed? Re-read everything (at most once a minute).
        The old list keeps answering meanwhile; the new one replaces it when ready."""
        import time

        with _REFRESH_LOCK:
            if time.monotonic() - self._loaded_at <= older_than:
                return
            self._loaded_at = time.monotonic()  # claimed: other misses don't start another scan
        self._load()

    def remember(self, spoken: str, name: str, target: str) -> None:
        """A program found by searching the disk opens at once next time."""
        from .. import config

        self.apps[name.lower()] = target
        found = config.read_json(_FOUND_PATH, {})
        found[name.lower()] = target
        if spoken and spoken != name.lower():
            found[spoken] = target
        config.write_json(_FOUND_PATH, dict(list(found.items())[-200:]))
        self.apps.update(found)

    def _load(self):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                 "Get-StartApps | ConvertTo-Json -Compress"],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
            items = json.loads(out) if out.strip() else []
            if isinstance(items, dict):
                items = [items]
            apps = {i["Name"].lower(): i["AppID"] for i in items if not _NOT_LAUNCHABLE.search(i["Name"])
                    and not i["AppID"].lower().endswith((".chm", ".txt", ".pdf", ".html", ".htm", ".url"))}
            for source in (_steam_games, _epic_games, _desktop_shortcuts, _found_before, _registry_apps):
                try:
                    for name, target in source().items():
                        apps.setdefault(name, target)  # the Start menu entry wins
                except Exception as e:  # noqa: BLE001 - one broken source must not hide the rest
                    log.warning("App index: %s failed: %s", source.__name__, e)
            self.apps = apps
            log.info("App index: %d apps and games", len(self.apps))
            import time

            self._loaded_at = time.monotonic()
        except Exception as e:  # noqa: BLE001
            log.warning("App index failed: %s", e)
        finally:
            self._ready.set()

    def find(self, spoken: str) -> tuple[str, str] | None:
        self._ready.wait(10)
        query = APP_ALIASES.get(spoken, spoken)
        for q in (spoken, query):
            if q in self.apps:
                return q, self.apps[q]
        if len(re.sub(r"\W", "", spoken)) < 4 and not re.search(r"[\u4e00-\u9fff]", spoken):
            # "кс", "это", "вк": too short to guess from. Only a whole word of a name counts.
            hits = [n for n in self.apps if spoken in re.findall(r"\w+", n)]
            return (min(hits, key=len), self.apps[min(hits, key=len)]) if hits else None
        if query != spoken:
            spoken = query  # a known alias whose app isn't installed: don't guess another one
        choices = list(self.apps)
        sounds = {_sound(name): name for name in choices}
        best = None
        for q in _spoken_forms(spoken) | {query}:
            hits = [(n, s) for n, s, _ in process.extract(q, choices, scorer=fuzz.WRatio, score_cutoff=82, limit=5)]
            hits += [(sounds[k], s) for k, s, _ in process.extract(
                _sound(q), list(sounds), scorer=fuzz.WRatio, score_cutoff=86, limit=5)]
            for name, score in hits:
                # Equal scores: the name closest to what was said as a whole ("dota 2",
                # not "dota 2 addon ...").
                rank = (score, fuzz.ratio(_sound(q), _sound(name)))
                if best is None or rank > best[1]:
                    best = (name, rank)
        if best:
            return best[0], self.apps[best[0]]
        return None


INDEX = AppIndex()


def _running_window(name: str, target: str) -> int:
    """The window of `name` if it is already running, else 0: "открой доту" while Dota
    is open must bring it up, not start a second copy."""
    from .. import screen

    folder = str(_INSTALL_DIRS.get(target, "")).lower()
    exe = Path(target.replace("\\", "/").rsplit("/", 1)[-1]).name.lower() if target.lower().endswith(".exe") else ""
    squeezed = re.sub(r"[^\w]", "", name.lower())
    for hwnd, _title, path in screen.windows_with_paths():
        if "python" in Path(path).name:  # Nova's own windows
            continue
        stem = re.sub(r"[^\w]", "", Path(path).stem)
        if (folder and path.startswith(folder + "\\")) or (exe and path.endswith("\\" + exe)) or (
                len(stem) >= 4 and (stem == squeezed or (stem in squeezed and len(stem) * 2 >= len(squeezed)))):
            return hwnd
    return 0


def _show_or_launch(name: str, target: str) -> None:
    from .. import screen

    if hwnd := _running_window(name, target):
        if not screen.bring_to_front(hwnd):
            raise SkillError(t("cant_focus", app=name))  # not a second copy
        log.info("Already running, brought up: %s", name)
    else:
        _launch_app_id(target)


def _launch_app_id(app_id: str) -> None:
    if os.path.exists(app_id) or app_id.lower().endswith((".exe", ".lnk")):
        os.startfile(app_id)
    elif "://" in app_id:
        webbrowser.open(app_id)
    else:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])


_FOLDER_PREFIX = re.compile(r"^(?:папку|папка|the\s+folder|folder|my|文件夹)\s*")
_TABS = {"вкладку", "вкладка", "эту вкладку", "tab", "the tab", "this tab", "标签页", "标签", "这个标签页"}
_CURRENT_WINDOW = {"", "это", "окно", "это окно", "текущее окно", "this", "it", "that", "window", "this window",
                   "它", "那个",
                   "the window", "current window", "这个", "窗口", "这个窗口", "当前窗口"}


_COMPOUND = re.compile(r"\s(?:и|а потом|потом|затем|в|во|на|and|then|in|on)\s|然后|接着|并且|之后")


@skill(
    "open",
    "Открыть программу, сайт или папку по названию.",
    patterns=(
        r"^(?:открой|открыть|запусти|запустить|включи|зайди на|перейди на)\s+(?:сайт\s+)?(?P<target>.+)$",
        r"^(?:open|launch|start|run|go to)\s+(?:the\s+)?(?:app\s+|website\s+|site\s+)?(?P<target>.+)$",
        r"^(?:打开|启动|运行|开启)\s*(?P<target>.+)$",
    ),
    params={"target": "название программы, сайта (или адрес) или папки"},
    priority=-1,
)
def open_target(ctx, target: str):
    name = _FOLDER_PREFIX.sub("", normalize(target))
    raw = normalize(ctx.extra.get("raw", ""))
    if raw.startswith("включи") and name in FOLDERS and not _FOLDER_PREFIX.match(normalize(target)):
        # "включи музыку" means play music, not open the Music folder: let the AI decide.
        raise SkillError(t("app_not_found", target=target))
    if _COMPOUND.search(f" {name} ") and name not in INDEX.apps and name not in SITES:
        # "открой телеграм и напиши маме", "ютуб в хроме": more than an app name.
        # Opening just the first part would drop the rest; the AI does the whole thing.
        raise SkillError(t("app_not_found", target=target))
    if name in FOLDERS:
        os.startfile(FOLDERS[name])
        return t("opening", target=name)
    if name in SITES:
        webbrowser.open(SITES[name])
        return None
    if "." in name and " " not in name:  # "youtube.com"
        webbrowser.open(name if "://" in name else f"https://{name}")
        return None
    alias = APP_ALIASES.get(name, name)
    found = INDEX.find(name)
    if not found:
        # Installed a minute ago? Re-read the list in the background (next time it's
        # there); the disk search below finds it right now anyway.
        threading.Thread(target=INDEX.refresh, daemon=True).start()
    if found:
        _show_or_launch(found[0], found[1])
        return None
    if alias not in BUILTIN and len(name.split()) <= 3 and (hit := search_pc(name)):
        app, exe, score = hit
        # A close match runs; a loose one is checked with the user first.
        if score >= 92 or ctx.confirm(t("open_found", name=app)):
            INDEX.remember(name, app, exe)
            _show_or_launch(app, exe)
            return None
        raise SkillError(t("cancelled"))
    if alias in BUILTIN:
        cmd = BUILTIN[alias]
        os.startfile(cmd) if cmd.endswith(":") else subprocess.Popen(cmd)
        return None
    raise SkillError(t("app_not_found", target=target))


@skill(
    "close",
    "Закрыть окно программы по названию, или текущее окно.",
    patterns=(
        r"^(?:закрой|закрыть)\s*(?P<target>.*)$",
        r"^close\s*(?:the\s+)?(?P<target>.*)$",
        r"^(?:关闭|关掉)\s*(?P<target>.*)$",
    ),
    params={"target": "название окна/программы; пусто или 'это' — текущее окно"},
)
def close_target(ctx, target: str = ""):
    import uiautomation as auto

    import keyboard

    from .. import screen

    name = normalize(target)
    if name in _TABS or re.search(r"вкладк|\btabs?\b|标签", name):
        # "закрой (новую) вкладку" is Ctrl+W, never "the window whose title says вкладка".
        keyboard.send("ctrl+w")
        return None
    if name in _CURRENT_WINDOW:
        hwnd = ctx.foreground_hwnd
    else:
        hwnd = _window_to_close(name)
        if not hwnd:
            raise SkillError(t("window_not_found", target=target))
    if not hwnd or not screen.is_app_window(hwnd):
        raise SkillError(t("which_window"))  # the desktop, the taskbar, or Nova itself
    try:
        win = auto.ControlFromHandle(hwnd)
        pattern = win.GetWindowPattern() if win and hasattr(win, "GetWindowPattern") else None
    except Exception:  # noqa: BLE001 - the window closed meanwhile
        pattern = None
    if pattern:
        pattern.Close()
    elif screen.bring_to_front(hwnd):
        keyboard.send("alt+f4")
    else:
        raise SkillError(t("cant_focus", app=screen.app_name(hwnd)))
    return None


def _window_to_close(name: str) -> int:
    """The window meant by "закрой телеграм": by the program first (its exe, a game's
    folder), by title only as a fallback, and never a browser just because a tab's
    title mentions the name."""
    from .. import screen
    from .input import _BROWSERS

    found = INDEX.find(name)
    if found and (hwnd := _running_window(*found)):
        return hwnd
    forms = _spoken_forms(name) | {APP_ALIASES.get(name, name)}
    windows = screen.windows_with_paths()
    for hwnd, _title, path in windows:  # the program's own file name
        stem = re.sub(r"\W", "", Path(path).stem)
        if len(stem) >= 4 and any(re.sub(r"\W", "", _sound(f)) == _sound(stem) for f in forms):
            return hwnd
    for hwnd, title, path in windows:  # titles, but not browser tabs
        if Path(path).name in _BROWSERS:
            continue
        words = re.findall(r"\w+", title.lower())
        for f in forms:
            if (len(f) < 6 and f in words) or (len(f) >= 6 and fuzz.partial_ratio(f, title.lower()) >= 90):
                return hwnd
    return 0


