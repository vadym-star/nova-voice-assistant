"""What Nova knows about the user's world: who "Вова" is, and facts they told it.

Stored in %APPDATA%\\Nova\\knowledge.json, bounded and de-duplicated:
- contacts: chat title -> spoken names ("вова", "вовчик"), at most MAX_ALIASES each,
  MAX_CONTACTS overall (least recently used dropped first);
- facts: short sentences ("Вова — мой брат"); a new fact that says nearly the
  same as an old one replaces it, at most MAX_FACTS (oldest dropped first).
A compact summary goes into the AI's prompt so it can resolve "напиши брату".
"""

import json
import re
import threading
import time

from rapidfuzz import fuzz

from . import config

PATH = config.DATA_DIR / "knowledge.json"
OLD_CONTACTS_PATH = config.DATA_DIR / "contacts.json"
MAX_CONTACTS = 300
MAX_ALIASES = 12
MAX_FACTS = 100
MAX_FACT_LEN = 200
PROMPT_BUDGET = 1500  # characters of memory given to the AI

# Russian/Ukrainian case endings, longest first: "Вове", "Вовой", "Вову" -> "вов".
_ENDINGS = ("ами", "ями", "ого", "его", "ому", "ему", "ой", "ей", "ом", "ем", "ою", "ею",
            "ам", "ям", "ах", "ях", "у", "ю", "е", "ы", "и", "а", "я", "ь")


def name_stem(name: str) -> str:
    """Case-insensitive, declension-insensitive key for a spoken name."""
    words = re.sub(r"[^\w\s-]", " ", name.lower().replace("ё", "е")).split()
    stems = []
    for w in words:
        if re.fullmatch(r"[а-яіїєґ-]+", w) and len(w) > 3:
            for end in _ENDINGS:
                if w.endswith(end) and len(w) - len(end) >= 3:
                    w = w[: -len(end)]
                    break
        stems.append(w)
    return " ".join(stems)


class Knowledge:
    def __init__(self, path=PATH):
        self.path = path
        self._lock = threading.Lock()
        data = config.read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        contacts = data.get("contacts", {})
        self.contacts: dict[str, dict] = {}
        for title, info in (contacts.items() if isinstance(contacts, dict) else ()):
            if isinstance(title, str) and isinstance(info, dict):
                aliases = info.get("aliases")
                info["aliases"] = [a for a in aliases if isinstance(a, str)] if isinstance(aliases, list) else []
                self.contacts[title] = info
        facts = data.get("facts", [])
        self.facts: list[dict] = [f if isinstance(f, dict) else {"text": f, "last": 0}
                                  for f in (facts if isinstance(facts, list) else [])
                                  if (isinstance(f, dict) and isinstance(f.get("text"), str)) or isinstance(f, str)]
        self._migrate()

    def _migrate(self) -> None:
        """contacts.json from the first version: {"вове": "Вовчикинатор"}."""
        if path_exists(OLD_CONTACTS_PATH) and self.path == PATH:
            try:
                for alias, title in json.loads(OLD_CONTACTS_PATH.read_text(encoding="utf-8")).items():
                    if isinstance(alias, str) and isinstance(title, str):
                        self.add_alias(title, alias, save=False)
                self.save()  # saved first: only then is the old file safe to remove
                OLD_CONTACTS_PATH.unlink()
            except Exception:  # noqa: BLE001 - an old, odd file must not stop Nova
                log.warning("Old contacts.json not migrated", exc_info=True)

    def save(self) -> None:
        try:
            with self._lock:
                config.write_json(self.path, {"contacts": self.contacts, "facts": self.facts})
        except OSError:  # disk full: keep working (and sending) with what's in memory
            log.exception("Saving knowledge failed")

    # --- contacts ----------------------------------------------------------

    def is_alias(self, spoken: str, title: str) -> bool:
        """Was this chat remembered under exactly these words (not just the same stem)?"""
        said = re.sub(r"[^\w\s]", "", spoken.lower().replace("ё", "е")).strip()
        info = self.contacts.get(title, {})
        known = [re.sub(r"[^\w\s]", "", a.lower().replace("ё", "е")).strip()
                 for a in info.get("aliases", []) + [title]]
        return said in known

    def find_contact(self, spoken: str, app: str = "telegram") -> str | None:
        """Chat title for "Вове", "вовчику", ... — or None if Nova doesn't know them."""
        key = name_stem(spoken)
        if not key:
            return None
        best, best_score = None, 0
        for title, info in self.contacts.items():
            if info.get("app", app) != app:
                continue
            for alias in info["aliases"] + [title]:
                score = 100 if name_stem(alias) == key else fuzz.ratio(name_stem(alias), key)
                if score > best_score:
                    best, best_score = title, score
        if best_score >= 90:
            return best
        # "Вова — это Вовчикинатор" told as a plain fact.
        for fact in self.facts:
            # Only "Вова — это X" / "Vova is X in Telegram": "Вова — мой брат" is not a chat name.
            m = re.match(r"^\s*(.+?)\s*(?:—|-|–)?\s*(?:это|is|是)\s+«?(.+?)»?\s*(?:в телеграме|in telegram)?\s*$",
                         fact["text"], re.I)
            if m and name_stem(m.group(1)) == key and len(m.group(2)) <= 60:
                return m.group(2).strip()
        return None

    def add_alias(self, title: str, alias: str, app: str = "telegram", save: bool = True) -> None:
        title, alias = title.strip(), alias.strip().lower()
        info = self.contacts.pop(title, {"aliases": [], "app": app, "uses": 0})
        # Every spoken form is kept ("вове", "вову"): the same stem can be another person
        # ("Ивановой" is not "Иванову"), so only an exact form counts as sure.
        if alias and alias not in info["aliases"] and alias != title.lower():
            info["aliases"] = (info["aliases"] + [alias])[-MAX_ALIASES:]
        info["uses"] = info.get("uses", 0) + 1
        info["last"] = time.time()
        self.contacts[title] = info  # re-inserted: most recently used last
        while len(self.contacts) > MAX_CONTACTS:
            oldest = min(self.contacts, key=lambda k: self.contacts[k].get("last", 0))
            del self.contacts[oldest]
        if save:
            self.save()

    def forget_contact(self, spoken: str) -> bool:
        title = self.find_contact(spoken) or (spoken if spoken in self.contacts else None)
        if title and title in self.contacts:
            del self.contacts[title]
            self.save()
            return True
        return False

    # --- facts -------------------------------------------------------------

    def add_fact(self, text: str) -> None:
        text = re.sub(r"\s+", " ", text).strip()[:MAX_FACT_LEN]
        if not text:
            return
        # Same thing said again (or corrected): replace, don't pile up.
        self.facts = [f for f in self.facts if fuzz.ratio(f["text"].lower(), text.lower()) < 90
                      and not _same_subject(f["text"], text)]
        self.facts.append({"text": text, "last": time.time()})
        self.facts = self.facts[-MAX_FACTS:]
        self.save()

    def forget_fact(self, query: str) -> bool:
        before = len(self.facts)
        self.facts = [f for f in self.facts if fuzz.partial_ratio(query.lower(), f["text"].lower()) < 80]
        if len(self.facts) != before:
            self.save()
            return True
        return False

    def prompt_text(self) -> str:
        """Compact memory for the AI, most recent first, within PROMPT_BUDGET."""
        lines = []
        for title in reversed(list(self.contacts)):
            aliases = ", ".join(self.contacts[title]["aliases"])
            lines.append(f"- contact: {aliases or title} = chat «{title}» ({self.contacts[title].get('app')})")
        lines += [f"- {f['text']}" for f in reversed(self.facts)]
        out, size = [], 0
        for line in lines:
            if size + len(line) > PROMPT_BUDGET:
                break
            out.append(line)
            size += len(line) + 1
        return "\n".join(out)

    def stats(self) -> dict:
        return {"contacts": len(self.contacts), "facts": len(self.facts)}

    def clear(self) -> None:
        self.contacts, self.facts = {}, []
        self.save()


def _same_subject(a: str, b: str) -> bool:
    """"Вова — мой брат" and "Вова — мой друг": a correction of the same fact."""
    sa = re.split(r"\s+(?:—|-|–|это|is)\s+", a.lower(), maxsplit=1)
    sb = re.split(r"\s+(?:—|-|–|это|is)\s+", b.lower(), maxsplit=1)
    return len(sa) == 2 and len(sb) == 2 and name_stem(sa[0]) == name_stem(sb[0]) and \
        fuzz.ratio(sa[1], sb[1]) >= 60


def path_exists(path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False
