"""AI backends that run on the user's existing subscription via the official CLIs.

- Claude: Claude Code CLI (`claude -p`), signed in with Claude Pro/Max.
- ChatGPT: Codex CLI (`codex exec`), signed in with ChatGPT Plus/Pro.

The model never touches the PC directly: it answers with JSON naming Nova's
own skills, and Nova runs them. That keeps it fast and safe.
"""

import atexit
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

from . import config
from .i18n import AI_LANGUAGE, t
from .skills import REGISTRY

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are {name}, a voice assistant on a Windows PC. The user speaks; their speech was \
transcribed automatically and may be in any language and contain recognition errors — infer the meaning.

You control the PC ONLY through the skills listed below. Reply with ONE JSON object, no markdown:
{{"say": "short spoken reply or empty string", "actions": [{{"skill": "name", "args": {{...}}}}]}}

Rules:
- "say" is spoken aloud: 1-2 short sentences, no lists, links, emoji or markdown.
- ALWAYS write "say" in {language}, whatever language the user spoke.
- If asked to do something, fill "actions" (several steps in order are fine); "say" may stay empty.
- If it's a question, answer in "say" and leave "actions" empty.
- If the available skills can't do it, say so honestly in "say".
- Skill arguments (app names, search queries, text to type) stay in the language the user meant them.
  Text to type keeps the user's capitalisation and punctuation.
- You can operate ANY program by combining skills: open it, wait, click elements by their visible name,
  type text, press keys. Never answer that you can't do something that can be done this way.
- Working step by step: if you need to see the screen to continue (e.g. after opening an app or a menu),
  give the first actions and set "continue": true. You'll then get a screenshot of the active window and
  the list of actions done so far, and you decide the next actions. Leave "continue" out when the task
  is finished. Prefer keyboard shortcuts you are sure about (e.g. in Telegram Desktop: Ctrl+F or Esc
  then typing searches chats, Enter opens the chosen chat, typing + Enter sends).
- If a step you planned FAILED (you'll see "-> FAILED: reason" in the list of done actions), don't give
  up at once: try another way, e.g. the program's official or English name ("Dota 2" for "дота"), its
  launcher, or a different route to the same result. Give up with a short explanation only when nothing
  sensible is left, and never retry something the user said "no" to.
- The user may be busy in another window (often a game). To act in a different program, pass its name
  as "app" to click / type_text / press_key instead of opening it: Nova then works in the background
  when the program allows it, and otherwise shows it briefly and returns the user to where they were.
  For typing into a specific field, pass "field" (e.g. "адрес", "поиск").
- To send someone a message, ALWAYS use the send_message skill (it finds and verifies the chat itself);
  never send messages by typing and pressing Enter yourself.
- Before an irreversible step that affects other people (sending a message or email, posting, paying),
  (except send_message, which checks with the user itself) put
  {{"skill": "confirm", "args": {{"question": "..."}}}} right before it, with the exact text in the
  question, in the reply language.

Learning: Nova remembers your solutions so it can handle similar requests without you next time. \
If "actions" is not empty, add "learn": {{"template": "...", "actions": [...]}} where
- "template" is the user's phrase in lowercase without punctuation, with the changing parts \
(names, cities, numbers, queries) replaced by {{name}} placeholders;
- "actions" are the same actions with "{{name}}" in place of those values.
Example: "найди погоду в Киеве" → "learn": {{"template": "найди погоду в {{город}}", \
"actions": [{{"skill": "search", "args": {{"query": "погода в {{город}}"}}}}]}}.
Don't add "learn" for questions or answers without actions. For a step-by-step task give "learn" only \
in the final step, covering ALL actions of the whole task in order (including waits and confirm).

Skills:
{skills}"""


def _skills_text() -> str:
    lines = []
    for s in REGISTRY:
        params = ", ".join(f"{k}: {v}" for k, v in s.params.items()) or "без параметров"
        lines.append(f"- {s.name}: {s.description} Параметры: {params}")
    return "\n".join(lines)


def _parse(reply: str) -> dict:
    """The AI's JSON answer, robust to text around it and to wrong types inside."""
    start = reply.find("{")
    while start != -1:
        try:
            data, _ = json.JSONDecoder().raw_decode(reply[start:])
        except json.JSONDecodeError:
            start = reply.find("{", start + 1)
            continue
        if isinstance(data, dict):
            if not isinstance(data.get("say"), str):
                data["say"] = str(data.get("say") or "")
            actions = data.get("actions")
            data["actions"] = [a for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []
            return data
        start = reply.find("{", start + 1)
    # No JSON at all: plain words are the answer, but never show raw JSON-ish text.
    return {"say": "" if "{" in reply else reply.strip(), "actions": []}


def _history_text(history: list[tuple[str, str]]) -> str:
    if not history:
        return ""
    turns = "\n".join(f"User: {u}\nYou: {a}" for u, a in history[-3:])
    return f"Recent conversation:\n{turns}\n\n"


class BrainError(Exception):
    pass


class _WarmClaude:
    """Keeps one Claude Code process started ahead of time.

    Starting `claude` costs ~2.5 s; a spare process that is already up answers in
    ~1.5 s. Each request uses a fresh process (no history piles up inside it), and
    the next spare is started right away in the background.
    """

    MAX_SPARES = 1  # one warm process for commands (~200 MB each); screenshots and tests get none

    def __init__(self):
        self._lock = threading.Lock()
        self._spares: dict[tuple, subprocess.Popen] = {}
        atexit.register(self.close)
    @staticmethod
    def _start(exe: str, model: str, system: str, workdir: Path) -> subprocess.Popen:
        digest = hashlib.sha1(system.encode("utf-8")).hexdigest()[:12]
        sys_file = workdir / f"system_prompt_{digest}.txt"
        if not sys_file.exists():
            sys_file.write_text(system, encoding="utf-8")
            for old in workdir.glob("system_prompt_*.txt"):  # prompts nothing has used for a day
                if time.time() - old.stat().st_mtime > 86400:
                    old.unlink(missing_ok=True)
        else:
            os.utime(sys_file)
        cmd = [exe, "-p", "--model", model, "--system-prompt-file", str(sys_file),
               "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               # No built-in tools, MCP servers, plugins or saved sessions: Nova only needs a
               # JSON answer, and skipping all of that cuts startup time a lot.
               "--tools", "", "--strict-mcp-config", "--safe-mode", "--no-session-persistence"]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", cwd=workdir,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as e:  # moved, half-updated, blocked by antivirus
            raise BrainError(t("claude_missing")) from e
        proc.nova_started = time.time()
        return proc

    def _put(self, key: tuple, proc: subprocess.Popen) -> None:
        with self._lock:
            old = self._spares.pop(key, None)
            self._spares[key] = proc
            while len(self._spares) > self.MAX_SPARES:
                self._spares.pop(next(iter(self._spares))).kill()  # oldest kind
        if old and old.poll() is None:
            old.kill()

    MAX_AGE_S = 30 * 60  # a spare older than this may hold an expired sign-in or an old CLI

    def keep_fresh(self, workdir: Path) -> None:
        """Replace spares a few minutes before they'd count as stale, so a command never
        waits for a cold start just because the user was quiet for half an hour."""
        def loop():
            while True:
                time.sleep(60)
                with self._lock:
                    old = [(k, p) for k, p in self._spares.items()
                           if time.time() - getattr(p, "nova_started", 0) > self.MAX_AGE_S - 300]
                for key, proc in old:
                    try:
                        self._put(key, self._start(*key, workdir))
                    except Exception:  # noqa: BLE001
                        log.debug("Spare refresh failed", exc_info=True)

        if not getattr(self, "_fresh_thread", None):
            self._fresh_thread = threading.Thread(target=loop, name="claude-spares", daemon=True)
            self._fresh_thread.start()

    def take(self, exe: str, model: str, system: str, workdir: Path, refill: bool = True) -> subprocess.Popen:
        key = (exe, model, system)
        with self._lock:
            proc = self._spares.pop(key, None)
        fresh = proc is not None and time.time() - getattr(proc, "nova_started", 0) < self.MAX_AGE_S
        if proc is not None and (proc.poll() is not None or not fresh):
            proc.kill()
            proc = None
        if proc is None:
            proc = self._start(exe, model, system, workdir)
        if refill:
            threading.Thread(target=lambda: self._put(key, self._start(exe, model, system, workdir)),
                             daemon=True).start()
        return proc

    def warm_up(self, exe: str, model: str, system: str, workdir: Path) -> None:
        """Start a spare at boot so even the first request is fast."""
        key = (exe, model, system)
        with self._lock:
            ready = key in self._spares
        if not ready:
            self._put(key, self._start(exe, model, system, workdir))

    def close(self) -> None:
        with self._lock:
            for proc in self._spares.values():
                if proc.poll() is None:
                    proc.kill()
            self._spares.clear()

_WARM = _WarmClaude()


# --- Connecting a subscription (used by the Settings window) -----------------

def find_claude() -> str | None:
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
        Path.home() / ".local/bin/claude.exe",  # native installer
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return shutil.which("claude.exe") or shutil.which("claude")


def _quiet(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None


def find_codex() -> str | None:
    """codex on PATH, or where npm puts it (installed while Nova was running)."""
    found = shutil.which("codex")
    npm = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
    return found or (str(npm) if npm.exists() else None)


def provider_status(provider: str) -> dict:
    """{"installed", "logged_in", "account", "plan"} for "claude" or "codex"."""
    if provider == "claude":
        exe = find_claude()
        if not exe:
            return {"installed": False, "logged_in": False}
        proc = _quiet([exe, "auth", "status"])
        try:
            data = json.loads(proc.stdout) if proc else {}
        except json.JSONDecodeError:
            data = {}
        return {"installed": True, "logged_in": bool(data.get("loggedIn")),
                "account": data.get("email", ""), "plan": data.get("subscriptionType", "")}
    if provider == "codex":
        exe = find_codex()
        if not exe:
            return {"installed": False, "logged_in": False}
        proc = _quiet([exe, "login", "status"])
        text = ((proc.stdout + proc.stderr) if proc else "").strip()
        logged_in = bool(proc and proc.returncode == 0 and "not logged in" not in text.lower())
        return {"installed": True, "logged_in": logged_in, "account": text.splitlines()[0] if text else "",
                "plan": ""}
    return {"installed": False, "logged_in": False}


def _terminal(command: str) -> None:
    """Run an interactive command in a new console window the user can see."""
    subprocess.Popen(["powershell", "-NoExit", "-Command", command],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)


def install_provider(provider: str) -> str:
    """Start installing the CLI in a visible terminal. Returns "started" or "need_node"."""
    if provider == "claude":
        # Native installer: no Node.js needed.
        _terminal("irm https://claude.ai/install.ps1 | iex; Write-Host 'Готово / Done. Закройте окно.'")
        return "started"
    if not shutil.which("npm"):
        webbrowser.open("https://nodejs.org/")
        return "need_node"
    _terminal("npm install -g @openai/codex; Write-Host 'Готово / Done. Закройте окно.'")
    return "started"


def login_provider(provider: str) -> None:
    """Open the provider's own sign-in flow (browser) in a visible terminal."""
    if provider == "claude":
        exe = find_claude() or "claude"
        _terminal(f"& '{exe}' auth login")
    elif provider == "codex":
        _terminal("codex login")


class Brain:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.workdir = config.DATA_DIR / "brain"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.cancel = None  # threading.Event: set by the router for the request in progress
        for leftover in self.workdir.glob("tmp*"):  # Codex files from a call that never finished
            leftover.unlink(missing_ok=True)
        self.one_off = False  # a Settings test: don't keep a spare for this exact prompt

    knowledge = None  # nova.knowledge.Knowledge, set by the router

    def system_prompt(self) -> str:
        prompt = SYSTEM_PROMPT.format(
            name=self.cfg["name"], language=AI_LANGUAGE.get(self.cfg["language"], "English"),
            skills=_skills_text())
        memory = self.knowledge.prompt_text() if self.knowledge else ""
        if memory:
            prompt += ("\n\nWhat you know about the user (they told you or you learned it). Use it to resolve "
                       "who and what they mean, e.g. pass the chat name to send_message:\n" + memory)
        return prompt

    def ask(self, text: str, history: list[tuple[str, str]], image_b64: str | None = None,
            progress: list[str] | None = None, known: tuple[str, list[str]] | None = None) -> dict:
        language = AI_LANGUAGE.get(self.cfg["language"], "English")
        # Repeated next to the request: otherwise the model tends to answer in
        # the language the user spoke.
        prompt = _history_text(history) + f"User: {text}"
        if known and known[1]:
            prompt += (f"\n\nNova already knows these elements of {known[0]} by sight; clicking one by exactly "
                       f"this name is instant: " + ", ".join(known[1]))
        if progress:
            prompt += "\n\nDone so far (in order):\n" + "\n".join(f"- {p}" for p in progress)
        if image_b64:
            prompt += "\n\nThe attached image is a screenshot of the active window right now."
        prompt += f"\n\n(Write \"say\" in {language}.)"
        reply = self._dispatch(prompt, self.system_prompt(), image_b64)
        log.info("Brain reply: %s", reply)
        return _parse(reply)

    def warm_up(self) -> None:
        try:
            if self.cfg["brain"] == "claude" and (exe := self.find_claude()):
                _WARM.warm_up(exe, self.cfg.get("claude_model") or "haiku", self.system_prompt(), self.workdir)
                _WARM.keep_fresh(self.workdir)
        except Exception as e:  # noqa: BLE001 - no spare just means a slower first answer
            log.warning("AI warm-up skipped: %s", e)

    def ask_image(self, text: str, jpeg_b64: str, system: str) -> str:
        """One question about a screenshot (JPEG, base64); returns the raw reply text."""
        reply = self._dispatch(text, system, jpeg_b64, refill=False)  # screenshots are rare: no spare
        log.info("Vision reply: %s", reply)
        return reply

    def _dispatch(self, prompt: str, system: str, image_b64: str | None = None, refill: bool = True) -> str:
        backend = self.cfg["brain"]
        if backend == "claude":
            return self._claude(prompt, system, image_b64, refill=refill and not self.one_off)
        if backend == "codex":
            return self._codex(prompt, system, image_b64)
        raise BrainError(t("ai_off"))

    def _run(self, cmd: list[str], stdin: str, timeout: int = 60) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                cmd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                cwd=self.workdir, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise BrainError(t("ai_slow")) from None

    # --- Claude Code -------------------------------------------------------

    @staticmethod
    def find_claude() -> str | None:
        return find_claude()

    def _claude(self, prompt: str, system: str, image_b64: str | None = None, refill: bool = True) -> str:
        exe = self.find_claude()
        if not exe:
            raise BrainError(t("claude_missing"))
        content = [{"type": "text", "text": prompt}]
        if image_b64 is not None:
            # The image goes straight in as a content block: no file, no Read tool round trip.
            content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                           "data": image_b64}})
        message = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
        proc = _WARM.take(exe, self.cfg.get("claude_model") or "haiku", system, self.workdir, refill=refill)
        out, err = self._wait(proc, message)
        data = None
        for line in reversed(out.strip().splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                data = event
                break
        if data is None or data.get("is_error"):
            text = str((data or {}).get("result") or "") + " " + out[-500:] + " " + err[-500:]
            log.error("claude failed: %s", text.strip()[:800])
            if re.search(r"log ?in|sign ?in|authenticat|unauthori[sz]ed|oauth|token", text, re.I):
                _WARM.close()  # every spare carries the same expired sign-in
                raise BrainError(t("claude_login"))
            if re.search(r"usage limit|rate limit|limit reached|quota|overloaded", text, re.I):
                raise BrainError(t("ai_limit"))
            if re.search(r"connection|network|ENOTFOUND|ECONN|timed? ?out|offline|getaddrinfo", text, re.I):
                raise BrainError(t("no_internet"))
            if re.search(r"unknown (?:option|argument)|unrecognized", text, re.I):
                raise BrainError(t("claude_outdated"))
            raise BrainError(t("claude_no_answer"))
        return data.get("result", "")

    def _wait(self, proc: subprocess.Popen, message: str, timeout: float = 90) -> tuple[str, str]:
        """Send the request and wait for the answer — but stop at once on Esc."""
        result: list = []
        worker = threading.Thread(target=lambda: result.append(proc.communicate(message)), daemon=True)
        worker.start()
        started = time.time()
        while worker.is_alive():
            worker.join(0.2)
            if self.cancel is not None and self.cancel.is_set():
                proc.kill()
                raise BrainError(t("cancelled"))
            if time.time() - started > timeout:
                proc.kill()
                raise BrainError(t("ai_slow"))
        return result[0] if result else ("", "")
    # --- Codex (ChatGPT) ---------------------------------------------------

    def _codex(self, prompt: str, system: str, image_b64: str | None = None) -> str:
        exe = find_codex()
        if not exe:
            raise BrainError(t("codex_missing"))
        fd, out_path = tempfile.mkstemp(suffix=".txt", dir=self.workdir)
        os.close(fd)
        image_path = None
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
               "--output-last-message", out_path]
        if self.cfg.get("codex_model"):
            cmd += ["-m", self.cfg["codex_model"]]
        if image_b64 is not None:
            # Codex takes images as files; this one is deleted right after the call.
            fd, image_path = tempfile.mkstemp(suffix=".jpg", dir=self.workdir)
            with os.fdopen(fd, "wb") as fh:
                fh.write(base64.b64decode(image_b64))
            cmd.append(f"--image={image_path}")  # "--image a.jpg -" would take "-" as a second image
        cmd.append("-")  # prompt from stdin
        try:
            proc = self._run(cmd, system + "\n\n" + prompt, timeout=90)
            reply = Path(out_path).read_text(encoding="utf-8").strip()
        finally:
            Path(out_path).unlink(missing_ok=True)
            if image_path:
                Path(image_path).unlink(missing_ok=True)
        if not reply:
            log.error("codex failed: %s %s", proc.stdout[-500:], proc.stderr[-500:])
            raise BrainError(t("codex_no_answer"))
        return reply