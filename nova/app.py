"""Nova main loop: shortcut / wake word -> speech-to-text -> router -> reply on the overlay."""

import ctypes
import logging
import threading
import time

import keyboard
import numpy as np

from . import config, guard, i18n, skills, sounds
from .audio import Recorder, UtteranceListener
from .hotkey import Hotkeys, parse as parse_hotkey
from .i18n import t
from .knowledge import Knowledge
from .router import DISMISS, Router
from .screen import bring_to_front, is_fullscreen
from .skills import Context
from .stt import NameGate, SpeechToText
from .text import normalize, split_name, strip_name_raw
from .vision import VisualMemory

log = logging.getLogger(__name__)
user32 = ctypes.windll.user32

# How close a heard word must be to the name (0-100). The quick model must be nearly
# certain to wake Nova up; the main model then has to agree before anything runs.
# Nova's "listening" chime lasts ~0.55 s and the microphone hears it: not the user's voice.
CHIME_S = 0.6
GATE_SURE = 88
NAME_SURE = 95  # main model: the name itself, a bent vowel ("Ново"), or Latin/Cyrillic — not "Норм", "Она"


# Keys that may be a push-to-talk shortcut on their own (they type nothing).
_SOLO_KEYS = {f"f{i}" for i in range(1, 25)} | {"pause", "scroll lock", "insert", "menu", "apps",
                                               "play/pause media", "caps lock"}
_MODIFIERS = {"ctrl", "alt", "shift", "win", "windows", "left windows", "right windows", "alt gr",
              "left ctrl", "right ctrl", "left alt", "right alt", "left shift", "right shift"}
_NOT_NAMES = {"да", "нет", "ок", "окей", "эй", "ну", "вот", "это", "привет", "стоп", "отмена",
              "yes", "no", "ok", "okay", "hey", "hi", "stop", "cancel", "the", "you"}


def usable_hotkey(combo: str) -> bool:
    """Nova suppresses its shortcut everywhere, so a plain letter or Esc would stop
    working in every app: a shortcut needs a modifier, unless it's a key that types nothing."""
    keys = [k.strip().lower() for k in str(combo).split("+") if k.strip()]
    if not keys or "esc" in keys or "escape" in keys or parse_hotkey(combo) is None:
        return False
    if len(keys) == 1:
        return keys[0] in _SOLO_KEYS
    mods = [k for k in keys if k in _MODIFIERS]
    # Shift alone with a letter is just a capital letter.
    return bool(mods) and not (set(mods) <= {"shift", "left shift", "right shift"} and len(keys) == 2)


def usable_name(name: str) -> bool:
    """At least three letters, and not a word people say all the time."""
    letters = sum(ch.isalpha() for ch in str(name))
    return letters >= 3 and normalize(name) not in _NOT_NAMES or (
        letters >= 2 and any("\u4e00" <= ch <= "\u9fff" for ch in str(name)))  # Chinese: 2 characters


class Nova:
    def __init__(self):
        self.cfg = config.load()
        i18n.set_language(self.cfg["language"])
        sounds.set_enabled(self.cfg.get("sounds", True))
        sounds.set_style(self.cfg.get("sound_style", "windows"))
        skills.load_all()
        self.memory = VisualMemory()
        self.knowledge = Knowledge()
        self.router = Router(self.cfg, self.memory, self.knowledge)
        self.recorder = Recorder()
        self.stt: SpeechToText | None = None
        self.ui = None  # set by ui.run()
        self._hotkey = None
        self._listening = threading.Event()
        self._stop_recording = threading.Event()  # second press in toggle mode
        self._cancel = threading.Event()  # Esc: drop what's being recorded or done
        self._work = threading.Lock()
        self._wake: UtteranceListener | None = None
        self._last_press = 0.0
        self._low_trust = False
        self._speakers_during_answer = False
        self._wake_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._mic_failures = 0
        self._idle_timer: threading.Timer | None = None
        self._gate: NameGate | None = None  # loaded with always-listen mode only
        guard.install_input_guard()
        guard.games.enabled = self.cfg.get("anticheat_guard", True)
        guard.games.start()
        # Windows' own hotkeys, not a keyboard hook: no key of the user's is ever delayed.
        self.hotkeys = Hotkeys(on_shortcut=self._on_hotkey, on_esc=self.cancel)

    # --- startup -----------------------------------------------------------

    def boot(self) -> None:
        """Load models (slow) in the background; the UI is already up."""
        # Everything heavy loads before the keyboard hook is installed: importing
        # onnxruntime (Whisper's VAD) can hold the GIL for 15+ s on some PCs.
        if not self.cfg.get("onboarded") and not getattr(self, "_welcomed", False):
            self._welcomed = True
            self.ui.open_settings()  # first start: pick a language while the models load
        try:
            if not getattr(self, "_boot_failures", 0):
                self.ui.state("loading", t("loading_stt"))
            self.load_stt()
        except Exception as e:  # noqa: BLE001
            log.exception("Boot failed")
            offline = any(w in str(e).lower() for w in ("connect", "resolve", "network", "timed out", "offline",
                                                         "max retries", "localentrynotfound", "huggingface"))
            self._boot_failures = getattr(self, "_boot_failures", 0) + 1
            if self._boot_failures == 1:  # once: retries every minute stay quiet
                self.ui.state("error", t("boot_offline" if offline else "boot_failed", log=str(config.LOG_PATH)))
                self.ui.hide_later(10)
                sounds.play("error")
            # The models download on the first start: try again in a minute (internet back).
            threading.Timer(60.0, self.boot).start()
            return
        log.info("Boot: speech ready")
        self.router.brain.warm_up()
        log.info("Boot: AI warmed up")
        if not self.register_hotkey(self.cfg["hotkey"]):
            self.register_hotkey(config.DEFAULTS["hotkey"])
        log.info("Boot: shortcut ready")
        if self.cfg["wake_word_enabled"]:
            self.start_wake_word()
        sounds.prepare()
        threading.Thread(target=self._warm_up, name="warm-up", daemon=True).start()
        self.ui.state("ready", t("ready", name=self.cfg["name"], hotkey=self.cfg["hotkey"]))
        self.ui.hide_later(2.5)

    def _warm_up(self) -> None:
        """Like shader pre-compiling: do every slow first-time thing once now, so the
        user's first command is as fast as the hundredth. Read-only, nothing is pressed."""
        from .skills.system import _volume
        from .screen import capture_window, main_window_of

        def step(name, fn):
            started = time.perf_counter()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - a warm-up must never break anything
                log.debug("Warm-up %s skipped: %s", name, e)
            return f"{name} {(time.perf_counter() - started) * 1000:.0f}"

        def uia():
            import uiautomation as auto
            auto.ControlFromHandle(user32.GetForegroundWindow()).GetChildren()
            # Telegram turns on its accessibility tree only when first asked (~seconds).
            if tg := main_window_of("telegram.exe"):
                auto.ControlFromHandle(tg).GetChildren()

        def vision():
            import cv2
            shot = capture_window(user32.GetShellWindow() or user32.GetForegroundWindow())
            shot.to_jpeg_b64()
            img = np.zeros((200, 300), np.uint8)
            cv2.matchTemplate(img, img[:40, :60].copy(), cv2.TM_CCOEFF_NORMED)

        def speech():
            # A real-length phrase through the full path (language check, prompt, filters).
            rng = np.random.default_rng(1)
            self.stt.transcribe((rng.standard_normal(16000 * 2) * 0.05).astype(np.float32))

        done = [step("volume", lambda: _volume().GetMasterVolumeLevelScalar()), step("uia", uia),
                step("vision", vision), step("fuzzy", lambda: split_name("нова открой", "Нова")),
                step("speech", speech)]
        log.info("Warm-up (ms): %s", ", ".join(done))

    def load_stt(self) -> None:
        self.stt = SpeechToText(self.cfg["stt_model"], self.cfg["stt_device"], self.cfg["stt_language"])
        if self._gate is not None:
            self.stt.detector = self._gate.model
        self.update_hint()
        # Whisper's speech filter loads onnxruntime on first use, which can freeze the
        # process for seconds; do it now rather than on the user's first command.
        from faster_whisper.vad import get_speech_timestamps

        get_speech_timestamps(np.zeros(16000, dtype=np.float32))

    def update_hint(self) -> None:
        n = self.cfg["name"]
        # Phrases this user says often, so Whisper writes them right ("Прими игру", not
        # "Примегру") and they hit the learned rule instead of going to the AI.
        router = getattr(self, "router", None)
        own = ". ".join(f"{n}, {p}" for p in router.common_phrases()) if router else ""
        self.stt.hints = {
            "ru": f"{n}, открой Telegram. {n}, нажми «Сохранить». {n}, громкость 50.",
            "uk": f"{n}, відкрий Telegram. {n}, натисни «Зберегти».",
            "en": f"{n}, open Telegram. {n}, open Chrome. {n}, click Save. {n}, volume 50.",
            "zh": f"{n}，打开微信。{n}，点击保存。{n}，音量调到50。现在几点？以下是普通话的句子。",
        }
        if own:
            for lang in self.stt.hints:
                self.stt.hints[lang] += " " + own + "."
        self._hinted = len(router.learned) + len(router.macros) if router else 0

    def register_hotkey(self, combo: str) -> bool:
        """Swap the push-to-talk shortcut; on a bad or taken combo keep the old one working."""
        return self.hotkeys.set_shortcut(combo)

    # --- push-to-talk ------------------------------------------------------

    def _on_hotkey(self) -> None:
        # Runs inside the keyboard hook: do nothing slow here, or Windows drops the hook.
        # Holding a key makes Windows repeat it ~30 times a second: those aren't presses.
        now = time.monotonic()
        repeat, self._last_press = now - self._last_press < 0.4, now
        if repeat:
            return
        if self._listening.is_set():
            if self.cfg["hotkey_mode"] == "toggle":
                self._stop_recording.set()
            return
        if self._work.locked():
            # Still busy with the last request: pressing again means "stop that".
            self._cancel.set()
            sounds.play("busy")
            return
        threading.Thread(target=self._push_to_talk, daemon=True).start()

    def _push_to_talk(self) -> None:
        if not self.stt or not self._work.acquire(blocking=False):
            return
        hwnd = user32.GetForegroundWindow()
        try:
            self._listening.set()
            self._stop_recording.clear()
            self._cancel.clear()
            self.ui.quiet = self.cfg.get("game_mode", True) and is_fullscreen(hwnd)
            if self._wake:
                self._wake.paused.set()
            sounds.play("listen")
            self.recorder.start()
            self.ui.state("listening", t("speak_now"))
            started, last_level = time.time(), 0.0
            spoke_at = quiet_since = None
            timeout = float(self.cfg.get("silence_timeout", 5))
            while time.time() - started < 30 and not (self._stop_recording.is_set() or self._cancel.is_set()):
                if self.cfg["hotkey_mode"] == "hold" and not self.hotkeys.shortcut_held():
                    break
                level = self.recorder.level if time.time() - started > CHIME_S else 0.0  # not our own chime
                if level > 0.02:
                    spoke_at, quiet_since = spoke_at or time.time(), None
                elif spoke_at:
                    quiet_since = quiet_since or time.time()
                    # Like Siri: in toggle mode a pause after the phrase ends it.
                    if self.cfg["hotkey_mode"] == "toggle" and time.time() - quiet_since > 1.0:
                        break
                elif self.cfg["hotkey_mode"] == "toggle" and time.time() - started > timeout:
                    self._cancel.set()  # called but nothing said: quietly go away
                    break
                if time.time() - last_level > 0.05:  # the orb doesn't need 50 updates a second
                    self.ui.level(level)
                    last_level = time.time()
                time.sleep(0.02)
            audio = self.recorder.stop()
            self._listening.clear()
            if self._cancel.is_set():
                self.ui.hide()  # Esc, or silence
                return
            sounds.play("heard")
            self.ui.state("thinking", "…")
            text = self.stt.transcribe(audio)
            if text:
                self._note_language()
            self._execute(text, hwnd)
        except Exception:  # noqa: BLE001 - never die silently on the user
            log.exception("Push-to-talk failed")
            self._fail()
        finally:
            self._listening.clear()
            if self.recorder._stream is not None:
                self.recorder.stop()
            if self._wake:
                self._wake.paused.clear()
            self._work.release()

    def cancel(self) -> None:
        self._cancel.set()
        if not self._work.locked() and not self._listening.is_set():
            self.ui.hide()  # "Говорите…" and nothing running yet: just go away

    def _fail(self) -> None:
        sounds.play("error")
        self.ui.state("error", t("error"))
        self.ui.hide_later(3)

    # --- shared pipeline ---------------------------------------------------

    def _execute(self, text: str, hwnd: int, low_trust: bool = False) -> None:
        log.info("Heard: %r", text)
        self._low_trust = low_trust
        if not text:
            self.ui.hide()  # nothing said: quietly go away, like Siri
            return
        self.ui.state("thinking", text)
        self._refocus(hwnd)
        ctx = Context(config=self.cfg, foreground_hwnd=hwnd, confirm=self.confirm, ask=self.ask)
        ctx.extra["cancel"] = self._cancel
        if low_trust:
            # Heard in always-listen mode while the speakers play: could be a video or a
            # voice chat saying "Нова, ...". Anything that acts or sends asks first.
            ctx.extra["low_trust"] = True
            log.info("Speakers are playing: treating this command with extra care")
        t0, sent = time.time(), guard.inputs_sent
        reply = self.router.handle(text, ctx, on_status=lambda s: self.ui.state(s, text))
        if self.stt and len(self.router.learned) + len(self.router.macros) != getattr(self, "_hinted", 0):
            self.update_hint()  # a new command was learned: let Whisper know it
        if guard.inputs_sent != sent and not guard.is_admin() and guard.is_elevated(hwnd):
            reply = t("elevated_failed")  # Windows swallowed the keys and clicks
        log.info("Reply (%.2fs): %r", time.time() - t0, reply)
        if self._cancel.is_set() or ctx.extra.get("dismissed"):
            sounds.play("busy")
            self.ui.hide()  # "отмена": no words, just gone
            return
        if not reply:
            self.ui.hide()  # done, nothing to say (the result is visible: an app opened...)
            return
        self.ui.state("speaking", reply)
        # No voice: the answer stays up long enough to be read (and the ✕ / Esc closes it).
        self.ui.hide_later(min(12.0, 2.5 + len(reply) * 0.06))

    @staticmethod
    def _refocus(hwnd: int) -> None:
        """Give focus back to the user's window in case Nova's overlay took it."""
        if hwnd and user32.GetForegroundWindow() != hwnd:
            bring_to_front(hwnd)

    def _listen(self, max_s: float, wait_s: float = 5.0) -> np.ndarray:
        """Record until the user has said something and paused (or gave up), not a fixed time."""
        sounds.play("listen")
        self.recorder.start()
        started, spoke_at, quiet_since = time.time(), None, None
        try:
            self._speakers_during_answer = False
            checked = False
            while time.time() - started < max_s and not self._cancel.is_set():
                time.sleep(0.05)
                if time.time() - started < CHIME_S:
                    continue  # that's Nova's own chime in the microphone, not the answer
                if self._low_trust and not checked and time.time() - started > CHIME_S + 0.3:
                    checked = True  # after Nova's own chime: is something else playing?
                    self._speakers_during_answer = guard.speakers_playing()
                loud = self.recorder.level > 0.02
                if loud:
                    spoke_at, quiet_since = spoke_at or time.time(), None
                elif spoke_at:
                    quiet_since = quiet_since or time.time()
                    if time.time() - quiet_since > 0.7:
                        break
                elif time.time() - started > wait_s:
                    break  # nobody answered
        finally:
            audio = self.recorder.stop()
        sounds.play("heard")
        return audio

    def _show_question(self, question: str) -> None:
        # A question must be seen in full, even in game mode or orb-only mode: silence
        # would count as "no", and the user must see exactly what they approve.
        self.ui.quiet = False
        self.ui.state("listening", question, question=True)

    def confirm(self, question: str) -> bool:
        """Ask a yes/no question (shown on the overlay, answered by voice).
        Silence, anything unclear, Esc or the shortcut count as "no"."""
        self._show_question(question)
        audio = self._listen(max_s=6)
        if self._cancel.is_set():
            return False
        if self._low_trust and self._speakers_during_answer:
            # The command came from the speakers; the same video could say "да" too.
            self.ui.state("error", t("confirm_needs_quiet"), question=True)
            self.ui.hide_later(6)
            return False
        answer = self.stt.transcribe(audio)
        log.info("Confirm answer: %r", answer)
        yes = i18n.is_yes(answer)
        self.ui.state("thinking", "…")  # the question goes away while Nova acts on the answer
        return yes

    def ask(self, question: str) -> str:
        """Ask an open question ("Как Вова записан в Телеграме?"), answered by voice."""
        self._show_question(question)
        audio = self._listen(max_s=8)
        if self._cancel.is_set():
            return ""
        answer = self.stt.transcribe(audio).strip(" .!?")
        self.ui.state("thinking", "…")
        log.info("Ask answer: %r", answer)
        if DISMISS.match(normalize(answer)):
            self._cancel.set()
            return ""
        return answer

    # --- wake word (experimental) -----------------------------------------

    def start_wake_word(self) -> None:
        with self._wake_lock:  # two saves at once must not start two microphones
            if self._wake:
                return
            self._wake = listener = UtteranceListener()
            self._start_gate()
            threading.Thread(target=self._wake_loop, args=(listener,), daemon=True).start()
            log.info("Wake word on: %s", self.cfg["name"])

    def _start_gate(self) -> None:
        if self._gate is None and self.stt:
            try:
                self._gate = NameGate(self.stt.device)
                self.stt.detector = self._gate.model  # also spots the language, faster
            except Exception:  # noqa: BLE001 - no gate: every phrase goes to the main model
                log.exception("Name gate unavailable")

    def stop_wake_word(self) -> None:
        with self._wake_lock:
            if self._wake:
                self._wake.stop()
                self._wake = None

    def _note_language(self) -> None:
        """A real command was just heard: remember which language the user speaks."""
        from collections import Counter

        lang = getattr(self.stt, "last_language", "")
        if lang:
            self._spoken = getattr(self, "_spoken", None) or Counter()
            self._spoken[lang] += 1

    def _gate_language(self) -> str | None:
        lang = self.cfg.get("stt_language", "auto")
        if lang != "auto":
            return lang
        # An English interface doesn't mean English speech: listen for the name in the
        # language the user last spoke (forcing English made "Она" come out as "Nova").
        # Not the last thing heard (a video, a friend, noise): the language of the user's
        # own commands so far.
        spoken = getattr(self, "_spoken", None)
        return spoken.most_common(1)[0][0] if spoken else self.cfg.get("language", "ru")

    def _quick_text(self, audio) -> str:
        """Cheap look at a phrase: "" if it isn't speech at all."""
        if self._gate is None:
            return self.stt.transcribe(audio)
        if not self._gate.has_speech(audio):
            return ""
        return self._gate.text(audio, self.cfg["name"], self._gate_language())

    def _heard_name(self, text: str, sure: int) -> str | None:
        return split_name(text, self.cfg["name"], sure, is_command=self.router.is_command)

    def _wake_loop(self, listener: UtteranceListener) -> None:
        awaiting_command_until = 0.0
        heard_speakers = False
        triggered = False  # the name was heard in the "early" part of the current phrase
        try:
            started_listening = time.time()
            for kind, audio in listener.utterances():
                if time.time() - started_listening > 10:
                    self._mic_failures = 0  # audio has been flowing for a while: the mic is fine
                if self._listening.is_set() or not self.stt:
                    continue
                if guard.session_locked():
                    continue  # locked PC or another user: nobody here is talking to Nova
                try:
                    if self._cancel.is_set() and not self._work.locked():
                        # Esc or ✕ while Nova waited for the command: stop waiting.
                        self._cancel.clear()
                        awaiting_command_until, triggered = 0.0, False
                    # Did Nova wait for a command when this phrase *started*? (A long
                    # command may end after the timeout.)
                    started_at = time.time() - audio.size / 16000
                    awaiting = started_at < awaiting_command_until
                    if kind == "early":
                        triggered = False
                        heard_speakers = guard.speakers_playing()  # during the phrase, not after it
                        if awaiting:
                            continue
                        quick = self._quick_text(audio)
                        if quick and self._heard_name(quick, GATE_SURE) is not None:
                            # The quick model thinks it heard the name; the main one checks the
                            # same 1.5 s (~0.2 s) before Nova shows up, so "Она", "Ова" or a
                            # made-up "Nova" never get a chime.
                            if self._heard_name(self.stt.transcribe(audio), NAME_SURE) is None:
                                log.info("Early wake rejected by the main model")
                                continue
                            # Name heard while the person is still talking: show up right away.
                            triggered = True
                            self.ui.quiet = self.cfg.get("game_mode", True) and is_fullscreen(
                                user32.GetForegroundWindow())
                            sounds.play("listen")
                            self.ui.state("listening", t("speak_now"))
                        continue

                    # kind == "end": the phrase is complete.
                    was_triggered, triggered = triggered, False
                    if not (was_triggered or awaiting):
                        if audio.size >= 16000 * UtteranceListener.EARLY_S:
                            continue  # long phrase whose start had no name: never wakes the big model
                        quick = self._quick_text(audio)  # short phrase ("Нова!"): check it now
                        if not quick or self._heard_name(quick, GATE_SURE) is None:
                            continue
                    text = self.stt.transcribe(audio)
                    if not text and was_triggered:
                        text = self.cfg["name"]  # the name was clearly heard, the rest wasn't: wait for it
                    if not text:
                        continue
                    hwnd = user32.GetForegroundWindow()
                    self.ui.quiet = self.cfg.get("game_mode", True) and is_fullscreen(hwnd)
                    if awaiting and split_name(text, self.cfg["name"]) == "":
                        command = ""  # "Нова?" again while waiting: keep waiting
                    elif awaiting:
                        command = text
                    elif (rest := self._heard_name(text, NAME_SURE)) is None:
                        # The big model doesn't hear the name: it was a false alarm. Stay quiet.
                        if was_triggered:
                            log.debug("Wake rejected by the main model: %r", text)
                            self.ui.hide()
                        continue
                    else:
                        # Keep the phrase as said (capitals, ":" for "запомни ...").
                        command = strip_name_raw(text, self.cfg["name"])
                        if split_name(text, self.cfg["name"], NAME_SURE) is None:
                            command = rest  # name glued to the command: "Новая пауза"
                        elif split_name(text, self.cfg["name"]) == "":
                            command = ""
                    awaiting_command_until = 0.0
                    if not command.strip():
                        if not was_triggered:
                            sounds.play("listen")
                            self.ui.state("listening", t("speak_now"))
                        wait = float(self.cfg.get("silence_timeout", 5))
                        awaiting_command_until = time.time() + wait
                        self._cancel.clear()
                        if self._idle_timer:
                            self._idle_timer.cancel()  # an older "Нова" must not hide this one
                        self._idle_timer = threading.Timer(wait, self._hide_if_idle,
                                                           args=(lambda: time.time() >= awaiting_command_until,))
                        self._idle_timer.start()
                        continue
                    if self._idle_timer:
                        self._idle_timer.cancel()
                    if self._cancel.is_set():
                        self._cancel.clear()
                        continue  # Esc while the command was being said
                    if not self._work.acquire(blocking=False):
                        continue
                    self._note_language()
                    listener.paused.set()  # what's said meanwhile is not a queued command
                    try:
                        # Sound from the speakers while the phrase was said (sampled mid-phrase),
                        # or still now — a video or a call, not necessarily the user.
                        low_trust = heard_speakers or guard.speakers_playing()  # before Nova's chime
                        sounds.play("heard")
                        self._execute(command, hwnd, low_trust=low_trust)
                    finally:
                        self._work.release()
                        listener.flush()
                        listener.paused.clear()
                    self._mic_failures = 0  # the microphone works
                except Exception:  # noqa: BLE001 - one bad phrase must not end wake-word mode
                    log.exception("Wake-word phrase failed")
                    self._fail()
        except Exception:  # noqa: BLE001 - microphone gone or unplugged
            log.exception("Wake-word listener stopped")
        finally:
            if self._wake is listener:
                self._wake = None
        if self.cfg.get("wake_word_enabled") and not listener._stop.is_set():
            # Not turned off by the user: try again (a USB mic plugged back in, a driver
            # reset), more and more slowly, and tell the user once instead of every time.
            self._mic_failures += 1
            if self._mic_failures == 2:
                self.ui.state("error", t("no_microphone"))
                self.ui.hide_later(8)
            threading.Timer(min(300.0, 5.0 * 2 ** min(self._mic_failures, 6)), self._restart_wake_word).start()

    def _restart_wake_word(self) -> None:
        if self.cfg.get("wake_word_enabled") and self._wake is None:
            log.info("Restarting always-listen")
            if not self._listening.is_set():
                # A headset plugged in, the default mic changed: PortAudio only reads the
                # device list at start, so make it look again.
                try:
                    import sounddevice as sd

                    sd._terminate()
                    sd._initialize()
                except Exception:  # noqa: BLE001
                    log.debug("Audio device refresh failed", exc_info=True)
            self.start_wake_word()

    def _reload_stt(self, old: dict) -> None:
        """New recognition model from Settings; if it can't load, keep the one that works."""
        self._gate = None  # the name gate follows the new device (CPU after GPU trouble)
        try:
            self.load_stt()
            if self.cfg.get("wake_word_enabled"):
                self._start_gate()
        except Exception:  # noqa: BLE001
            log.exception("New speech model failed, going back")
            for key in ("stt_model", "stt_device", "stt_language"):
                self.cfg[key] = old[key]
            config.save(self.cfg)
            self.ui.state("error", t("stt_failed"))
            self.ui.hide_later(5)
            try:
                self.load_stt()
            except Exception:  # noqa: BLE001
                log.exception("Old speech model failed too")

    def _restart_elevated(self) -> None:
        """Turned on in Settings: come back as administrator now (if the UAC prompt says yes)."""
        time.sleep(0.4)  # let the settings page show "saved"
        if guard.relaunch(elevated=True):
            self.ui.quit()
        else:
            self.cfg["run_as_admin"] = False
            config.save(self.cfg)

    def _hide_if_idle(self, still_waiting=lambda: True) -> None:
        """"Нова?" and then silence: go away after the timeout, like Siri."""
        if not self._work.locked() and not self._listening.is_set() and still_waiting():
            self.ui.hide()
    # --- settings ----------------------------------------------------------

    def apply_settings(self, new: dict) -> str:
        """Returns "" when all is well, or what to tell the user (the old value stays)."""
        with self._settings_lock:  # the page saves every change at once, from several threads
            return self._apply_settings(new)

    def _apply_settings(self, new: dict) -> str:
        old = dict(self.cfg)
        problem = ""
        if "hotkey" in new and not usable_hotkey(new["hotkey"]):
            new.pop("hotkey")
            problem = t("bad_hotkey")
        if "name" in new and not usable_name(new["name"]):
            new.pop("name")
            problem = t("bad_name")
        self.cfg.update(new)
        if old["hotkey"] != self.cfg["hotkey"] and not self.register_hotkey(self.cfg["hotkey"]):
            self.cfg["hotkey"], problem = old["hotkey"], t("bad_hotkey")
        if old["language"] != self.cfg["language"]:
            # The default name follows the language ("Нова" -> "Nova"); a custom one stays.
            if self.cfg["name"] in i18n.DEFAULT_NAMES.values():
                self.cfg["name"] = i18n.DEFAULT_NAMES.get(self.cfg["language"], self.cfg["name"])
            i18n.set_language(self.cfg["language"])
            self.ui.set_language(self.cfg["language"])
        config.save(self.cfg)
        guard.games.enabled = self.cfg.get("anticheat_guard", True)
        if self.cfg.get("run_as_admin") and not old.get("run_as_admin") and not guard.is_admin():
            threading.Thread(target=self._restart_elevated, daemon=True).start()
        sounds.set_enabled(self.cfg.get("sounds", True))
        sounds.set_style(self.cfg.get("sound_style", "windows"))
        threading.Thread(target=sounds.prepare, daemon=True).start()  # never build a sound in the key hook
        if (old["name"], old["stt_language"]) != (self.cfg["name"], self.cfg["stt_language"]) and self.stt:
            self.update_hint()
        if (old["stt_model"], old["stt_device"], old["stt_language"]) != (
                self.cfg["stt_model"], self.cfg["stt_device"], self.cfg["stt_language"]):
            threading.Thread(target=self._reload_stt, args=(old,), daemon=True).start()
        if (old["brain"], old.get("claude_model"), old["language"]) != (
                self.cfg["brain"], self.cfg.get("claude_model"), self.cfg["language"]):
            threading.Thread(target=self.router.brain.warm_up, daemon=True).start()
        if self.cfg["wake_word_enabled"]:
            self.start_wake_word()
        else:
            self.stop_wake_word()
        return problem
