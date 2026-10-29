"""Seeing the screen: AI locates things once, Nova remembers them and finds them locally next time.

Memory layout (one SQLite file, %APPDATA%\\Nova\\memory.db):
- no screenshots are ever stored, only a small crop of each learned element;
- at most MAX_VARIANTS crops per (app, element), MAX_PATTERNS / MAX_BYTES overall;
- a crop that matches an existing one updates it instead of adding a duplicate;
- crops that keep missing (user says "не туда") are dropped; the least useful
  and least recently used ones are evicted first.
"""

import json
import logging
import re
import sqlite3
import threading
import time

import cv2
import numpy as np

from . import config
from .screen import Shot
from .text import normalize

log = logging.getLogger(__name__)

DB_PATH = config.DATA_DIR / "memory.db"
MAX_VARIANTS = 3  # looks of one element (themes, resolutions) kept per app
MAX_PATTERNS = 400
MAX_BYTES = 10 * 1024 * 1024
MATCH_THRESHOLD = 0.82  # normalized cross-correlation needed to trust a remembered crop
# Where the element was last time: a looser match is enough there, so a glowing or
# pulsing button (Dota's ACCEPT) is still found without asking the AI again.
LOCAL_THRESHOLD = 0.68
SAME_THRESHOLD = 0.92  # a new crop this similar to a stored one is the same element
FLAT_STD = 4.0  # a crop with less contrast than this is a plain patch, not an element
MAP_EVERY_S = 12 * 3600  # an app screen is mapped by the AI at most this often
MAX_MAP = 30  # elements taken from one mapped screen


def _crop(shot: Shot, x: int, y: int, w: int, h: int) -> tuple[np.ndarray, int, int]:
    """Grayscale crop around (x, y) in image coords; size clamped to stay small but distinctive."""
    w = int(np.clip(w * 1.2, 32, 260))
    h = int(np.clip(h * 1.2, 20, 140))
    l = int(np.clip(x - w // 2, 0, max(0, shot.width - w)))
    t = int(np.clip(y - h // 2, 0, max(0, shot.height - h)))
    gray = cv2.cvtColor(shot.image[t:t + h, l:l + w], cv2.COLOR_BGR2GRAY)
    return gray, x - l, y - t  # click point inside the crop


def _labels_match(a: str, b: str) -> bool:
    from rapidfuzz import fuzz

    if fuzz.ratio(a, b) >= 88:
        return True
    short, long = sorted((a, b), key=len)
    # One label inside the other only as whole words, and not a tiny one: "ок" is not
    # "закрыть окно", "play" is not "display".
    return len(short) >= 4 and re.search(rf"(?<!\w){re.escape(short)}(?!\w)", long) is not None


class VisualMemory:
    def __init__(self, path=DB_PATH):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        try:
            self.db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.DatabaseError:
            # Damaged (a crash mid-write, a bad disk): keep it aside, start a fresh one.
            self.db.close()
            from pathlib import Path

            Path(path).replace(Path(str(path) + ".corrupt"))
            log.warning("memory.db was damaged; moved aside and started fresh")
            self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY, app TEXT, labels TEXT, png BLOB,
            off_x INT, off_y INT, win_w INT, win_h INT,
            hits INT DEFAULT 0, misses INT DEFAULT 0, created REAL, last_used REAL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_app ON patterns(app)")
        # Added later: where the element sits in its window (0..1), and whether it was only
        # seen on a mapped screen rather than clicked.
        for column in ("rel_x REAL", "rel_y REAL", "seen INT DEFAULT 0"):
            try:
                self.db.execute(f"ALTER TABLE patterns ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # already there
        self.db.execute("CREATE TABLE IF NOT EXISTS maps (app TEXT, size TEXT, at REAL, PRIMARY KEY (app, size))")
        self.db.commit()

    # --- lookup ------------------------------------------------------------

    def _candidates(self, app: str, label: str):
        label = normalize(label)
        rows = self.db.execute(
            "SELECT id, labels, png, off_x, off_y, win_w, win_h, rel_x, rel_y, seen FROM patterns WHERE app=?",
            (app,)).fetchall()
        return [r for r in rows if any(_labels_match(label, lb) for lb in json.loads(r[1]))]

    def labels(self, app: str, limit: int = 40) -> list[str]:
        """What Nova already knows by sight in this app, most useful first (for the AI)."""
        with self._lock:
            rows = self.db.execute(f"SELECT labels FROM patterns WHERE app=? ORDER BY seen ASC, "
                                   f"{self._usefulness()} DESC LIMIT ?", (app, limit)).fetchall()
        return [json.loads(r[0])[0] for r in rows if json.loads(r[0])]

    def find(self, app: str, label: str, shot: Shot) -> dict | None:
        """Look for a remembered element on screen. Returns {"id", "x", "y", "score"} in screen coords."""
        from rapidfuzz import fuzz

        with self._lock:
            rows = self._candidates(app, label)
        said = normalize(label)
        exact = [r for r in rows if any(fuzz.ratio(said, lb) >= 88 for lb in json.loads(r[1]))]
        rows = exact or rows  # "сохранить" must not press "сохранить как" when both are known
        if not rows:
            return None
        gray = cv2.cvtColor(shot.image, cv2.COLOR_BGR2GRAY)
        best = None
        for pid, _labels, png, off_x, off_y, win_w, win_h, rel_x, rel_y, seen in rows:
            original = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
            if original is None or original.std() < FLAT_STD:
                continue  # a plain patch "matches" everywhere with a perfect score
            # Buttons keep their size when a window is resized (only games scale their UI):
            # the crop as it was first, then scaled with the window.
            ratio = shot.width / win_w if win_w else 1.0
            hit, scale = None, 1.0  # never the previous element's hit
            for scale in (1.0, ratio) if abs(ratio - 1) > 0.03 else (1.0,):
                tpl = original if scale == 1.0 else cv2.resize(original, None, fx=scale, fy=scale,
                                                               interpolation=cv2.INTER_AREA)
                if tpl.shape[0] >= gray.shape[0] or tpl.shape[1] >= gray.shape[1] or min(tpl.shape) < 8:
                    continue
                hit = self._match(gray, tpl, shot, rel_x, rel_y, seen)
                if hit:
                    break
            if not hit:
                continue
            score, mx, my = hit
            if best is None or score > best["score"]:
                best = {"id": pid, "score": float(score),
                        "x": shot.left + mx + round(off_x * scale), "y": shot.top + my + round(off_y * scale)}
        if best:
            log.info("Visual memory hit: %s '%s' score %.2f", app, label, best["score"])
        return best

    @staticmethod
    def _match(gray, tpl, shot: Shot, rel_x, rel_y, seen) -> tuple[float, int, int] | None:
            hit = None
            if rel_x is not None:
                # First where it was last time: a small area, a looser match.
                th, tw = tpl.shape
                cx, cy = rel_x * shot.width, rel_y * shot.height
                pad = max(tw, th) + 40
                l, t = int(max(0, cx - tw / 2 - pad)), int(max(0, cy - th / 2 - pad))
                r, b = int(min(gray.shape[1], cx + tw / 2 + pad)), int(min(gray.shape[0], cy + th / 2 + pad))
                if r - l > tw and b - t > th:
                    res = cv2.matchTemplate(gray[t:b, l:r], tpl, cv2.TM_CCOEFF_NORMED)
                    _, score, _, (mx, my) = cv2.minMaxLoc(res)
                    if score >= LOCAL_THRESHOLD + (0.08 if seen else 0):
                        hit = (score, l + mx, t + my)
            if hit is None:
                need = MATCH_THRESHOLD + (0.04 if seen else 0)
                th, tw = tpl.shape
                if gray.shape[1] > 1400 and min(th, tw) >= 24:
                    # A big window: find the spot at half size (~1/4 of the work), then confirm at full size.
                    small = cv2.matchTemplate(cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA),
                                              cv2.resize(tpl, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA),
                                              cv2.TM_CCOEFF_NORMED)
                    _, rough, _, (sx, sy) = cv2.minMaxLoc(small)
                    if rough < need - 0.15:
                        return None
                    l, t = max(0, sx * 2 - 16), max(0, sy * 2 - 16)
                    area = gray[t:t + th + 32, l:l + tw + 32]
                    if area.shape[0] > th and area.shape[1] > tw:
                        res = cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED)
                        _, score, _, (mx, my) = cv2.minMaxLoc(res)
                        return (score, l + mx, t + my) if score >= need else None
                res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
                _, score, _, (mx, my) = cv2.minMaxLoc(res)
                if score >= need:
                    hit = (score, mx, my)
            return hit

    # --- learning ----------------------------------------------------------

    def learn(self, app: str, labels: list[str], shot: Shot, x: int, y: int, w: int, h: int,
              seen: bool = False) -> int:
        """Remember an element the AI found at image coords (x, y). Returns the pattern id.
        `seen`: it was on a mapped screen, not clicked — trusted a little less."""
        labels = sorted({normalize(lb) for lb in labels if lb and normalize(lb)})
        rel_x, rel_y = x / max(1, shot.width), y / max(1, shot.height)
        crop, off_x, off_y = _crop(shot, x, y, w, h)
        if crop.std() < FLAT_STD:
            log.info("Not remembering %s %s: the spot is a plain patch", app, labels)
            return 0
        now = time.time()
        with self._lock:
            # Same element already stored? Update it instead of adding a duplicate.
            for pid, old_labels, png, *_ in self._candidates(app, labels[0] if labels else ""):
                tpl = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
                tpl = cv2.resize(tpl, (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_AREA)
                if cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)[0, 0] >= SAME_THRESHOLD:
                    merged = sorted(set(json.loads(old_labels)) | set(labels))
                    _, new_png = cv2.imencode(".png", crop, [cv2.IMWRITE_PNG_COMPRESSION, 9])
                    # The newest look, its own click point and window size go together.
                    self.db.execute("UPDATE patterns SET labels=?, png=?, off_x=?, off_y=?, hits=hits+?, "
                                    "last_used=?, win_w=?, win_h=?, rel_x=?, rel_y=?, "
                                    "seen=MIN(COALESCE(seen, 0), ?) WHERE id=?",
                                    (json.dumps(merged, ensure_ascii=False), new_png.tobytes(), off_x, off_y,
                                     0 if seen else 1, now, shot.width, shot.height, rel_x, rel_y, int(seen), pid))
                    self.db.commit()
                    return pid
            _, png = cv2.imencode(".png", crop, [cv2.IMWRITE_PNG_COMPRESSION, 9])
            cur = self.db.execute(
                "INSERT INTO patterns (app, labels, png, off_x, off_y, win_w, win_h, hits, created, last_used, "
                "rel_x, rel_y, seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (app, json.dumps(labels, ensure_ascii=False), png.tobytes(), off_x, off_y,
                 shot.width, shot.height, 0 if seen else 1, now, now, rel_x, rel_y, int(seen)))
            self.db.commit()
            pid = cur.lastrowid
            self._evict(app, labels)
        log.info("Learned visual pattern %d: %s %s", pid, app, labels)
        return pid

    def _usefulness(self) -> str:
        # Higher is better: confirmed hits, minus misses, decayed by days unused.
        # Elements only seen on a mapped screen go before ones the user actually clicked.
        return ("(hits - 2 * misses - 3 * COALESCE(seen, 0)) - (strftime('%s','now') - last_used) / 86400.0")

    def _evict(self, app: str, labels: list[str]) -> None:
        mine = set(labels)
        same = [r[0] for r in self.db.execute("SELECT id, labels FROM patterns WHERE app=?", (app,))
                if mine & set(json.loads(r[1]))]
        if len(same) > MAX_VARIANTS:
            keep = {r[0] for r in self.db.execute(
                f"SELECT id FROM patterns WHERE id IN ({','.join('?' * len(same))}) "
                f"ORDER BY {self._usefulness()} DESC LIMIT ?", (*same, MAX_VARIANTS))}
            self.db.executemany("DELETE FROM patterns WHERE id=?", [(i,) for i in same if i not in keep])
        while True:
            count, size = self.db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(png)), 0) FROM patterns").fetchone()
            if count <= MAX_PATTERNS and size <= MAX_BYTES:
                break
            self.db.execute(f"DELETE FROM patterns WHERE id = (SELECT id FROM patterns "
                            f"ORDER BY {self._usefulness()} ASC LIMIT 1)")
        self.db.commit()

    def feedback(self, pid: int, ok: bool) -> None:
        with self._lock:
            col = "hits" if ok else "misses"
            self.db.execute(f"UPDATE patterns SET {col}={col}+1, last_used=?{', seen=0' if ok else ''} WHERE id=?",
                            (time.time(), pid))
            # A crop that is wrong more often than right is worse than none.
            self.db.execute("DELETE FROM patterns WHERE id=? AND misses >= 1 AND misses >= hits", (pid,))
            self.db.commit()

    def forget(self, pid: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM patterns WHERE id=?", (pid,))
            self.db.commit()

    def due_for_map(self, app: str, shot: Shot) -> bool:
        """Claim the right to map this app screen now (at most every MAP_EVERY_S)."""
        size = f"{shot.width // 50}x{shot.height // 50}"
        with self._lock:
            row = self.db.execute("SELECT at FROM maps WHERE app=? AND size=?", (app, size)).fetchone()
            if row and time.time() - row[0] < MAP_EVERY_S:
                return False
            self.db.execute("INSERT OR REPLACE INTO maps VALUES (?, ?, ?)", (app, size, time.time()))
            self.db.commit()
        return True

    def release_map(self, app: str, shot: Shot) -> None:
        with self._lock:
            self.db.execute("DELETE FROM maps WHERE app=? AND size=?", (app, f"{shot.width // 50}x{shot.height // 50}"))
            self.db.commit()

    def stats(self) -> dict:
        with self._lock:
            count, size = self.db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(png)), 0) FROM patterns").fetchone()
        return {"patterns": count, "bytes": size}

    def clear(self) -> None:
        with self._lock:
            self.db.execute("DELETE FROM patterns")
            self.db.execute("DELETE FROM maps")
            self.db.commit()
            self.db.execute("VACUUM")


# --- asking the AI ----------------------------------------------------------

LOCATE_SYSTEM = ("You locate UI elements in screenshots for a voice assistant that will click them. "
                 "Reply with ONE JSON object only, no markdown.")


def ai_locate(brain, shot: Shot, target: str, app: str, avoid=None) -> dict | None:
    """Ask the AI where `target` is. Returns {"x","y","w","h","label"} in shot image coords."""
    b64, scale = shot.to_jpeg_b64()
    w, h = round(shot.width * scale), round(shot.height * scale)
    prompt = (f'Screenshot of the "{app}" window, {w}x{h} px. The user said to click: "{target}" '
              f"(any language; the interface may be in another language — match by meaning). "
              'Reply {"found": true|false, "x": int, "y": int, "w": int, "h": int, "label": "the element\'s '
              'visible text or short description"} where x,y is the centre and w,h the size, in image pixels.')
    points = [avoid] if avoid and isinstance(avoid[0], (int, float)) else list(avoid or [])
    if points:
        spots = ", ".join(f"({round((x - shot.left) * scale)}, {round((y - shot.top) * scale)})" for x, y in points)
        prompt += f" The element(s) at {spots} were WRONG — pick a different one."
    reply = brain.ask_image(prompt, b64, LOCATE_SYSTEM)
    match = re.search(r"\{.*\}", reply, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    if not data.get("found") or "x" not in data or "y" not in data:
        return None
    to_img = lambda v: round(float(v) / scale)  # noqa: E731
    try:
        inside = 0 <= to_img(data["x"]) < shot.width and 0 <= to_img(data["y"]) < shot.height
        to_img(data.get("w", 80)), to_img(data.get("h", 36))
    except (TypeError, ValueError):
        return None  # "x": "center" and the like
    if not inside:
        return None  # outside the window: a click there could hit anything
    return {"x": to_img(data["x"]), "y": to_img(data["y"]), "w": to_img(data.get("w", 80)),
            "h": to_img(data.get("h", 36)), "label": str(data.get("label", ""))}


def ai_map(brain, shot: Shot, app: str) -> list[dict]:
    """Ask the AI for every clickable thing on this screen: Nova's map of the app."""
    b64, scale = shot.to_jpeg_b64()
    w, h = round(shot.width * scale), round(shot.height * scale)
    prompt = (f'Screenshot of the "{app}" window, {w}x{h} px. List the clickable elements a user might ask '
              f"to press (buttons, tabs, menu items, icons with a clear meaning), at most {MAX_MAP}. "
              'Reply {"elements": [{"label": "visible text or short description", "x": int, "y": int, '
              '"w": int, "h": int}]} with centres and sizes in image pixels.')
    reply = brain.ask_image(prompt, b64, LOCATE_SYSTEM)
    match = re.search(r"\{.*\}", reply, re.S)
    try:
        items = json.loads(match.group(0)).get("elements", []) if match else []
    except (json.JSONDecodeError, AttributeError):
        return []
    to_img = lambda v: round(float(v) / scale)  # noqa: E731
    out = []
    for e in items[:MAX_MAP]:
        try:
            x, y = to_img(e["x"]), to_img(e["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if e.get("label") and 0 <= x < shot.width and 0 <= y < shot.height:
            out.append({"label": str(e["label"])[:60], "x": x, "y": y,
                        "w": to_img(e.get("w", 80)), "h": to_img(e.get("h", 36))})
    return out


def map_screen(memory: VisualMemory, brain, shot: Shot, app: str) -> None:
    """In the background, after a successful visual click: remember the rest of the screen."""
    try:
        elements = ai_map(brain, shot, app)
        if not elements:
            memory.release_map(app, shot)  # nothing came back: try again next time
        for e in elements:
            memory.learn(app, [e["label"]], shot, e["x"], e["y"], e["w"], e["h"], seen=True)
        log.info("Mapped %s: %d elements", app, len(elements))
    except Exception:  # noqa: BLE001 - a map is a bonus; never disturb the user
        log.debug("Mapping %s failed", app, exc_info=True)


def ai_describe(brain, shot: Shot, question: str, language: str) -> str:
    b64, _ = shot.to_jpeg_b64()
    system = (f"You are a voice assistant looking at the user's screen. Answer in {language}, in 1-3 short "
              "sentences suitable for reading aloud: no lists, links or markdown.")
    return brain.ask_image(question or "What is here? Briefly explain.", b64, system).strip()
