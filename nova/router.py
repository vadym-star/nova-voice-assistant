"""Decides what to do with a phrase: macro, learned action, instant skill, or the AI."""

import functools
import json
import logging
import re
import time

from . import config
from .brain import Brain, BrainError
from .i18n import t as tr
from .skills import REGISTRY, Context, SkillError, get
from .text import normalize, strip_name, strip_name_raw

log = logging.getLogger(__name__)

MACROS_PATH = config.DATA_DIR / "macros.json"
LEARNED_PATH = config.DATA_DIR / "learned.json"
MAX_RULES = 500
MAX_STEPS = 8  # AI look-and-act rounds per request

# Said on its own, any of these means "never mind": stop, hide, say nothing.
DISMISS = re.compile(r"^(?:отмена|отмени|отменить|стоп|стой|хватит|не надо|ничего|забей|неважно|проехали|"
                     r"cancel|stop|never ?mind|forget it|nothing|that s all|取消|算了|算了吧|不用了|不要了|停|停止|没事)$")

_CORRECT = re.compile(r"^(?:нет\s+|no\s+)?(?:не туда|не то|не так|неправильно|ошибка|мимо|не та кнопка|"
                      r"wrong|not there|not that|that s wrong|wrong one|wrong button|不对|错了|不是这个|点错了)$")

# "Запомни, что ..." is a fact (skills/memory.py), not a macro.
_REMEMBER = re.compile(r"^(?:запомни(?![\s,]+что\b)|создай\s+(?:шорткат|команду|макрос)|remember(?!\s+that\b)|"
                       r"create\s+(?:a\s+)?(?:shortcut|command)|记住(?=[^：:]*[：:])|创建指令|创建快捷指令)"
                       r"[\s,:：]*(?P<rest>.+)$", re.I)
# Where the macro name ends when the user didn't say a separator: at the first command verb.
_FIRST_VERB = re.compile(r"\s(?=(?:открой|запусти|включи|закрой|нажми|кликни|напиши|напечатай|"
                         r"найди|громкость|сделай|прокрути|пауза|заблокируй|open|launch|close|click|press|"
                         r"type|search|find|volume|scroll|pause|play|lock)\b)")
_FORGET = re.compile(r"^(?:забудь(?!\s+что\b)|forget(?!\s+that\b)(?=\s+(?:the\s+)?(?:command|shortcut)\b)|удали\s+(?:шорткат|команду|макрос)|forget|delete\s+(?:the\s+)?(?:shortcut|command)|忘记|删除指令)\s*(?P<name>.+)$")
_LIST = re.compile(r"^(?:(?:какие|покажи|список)\s+(?:у тебя\s+)?(?:шорткаты|команды|макросы)|(?:list|show)\s+(?:my\s+)?(?:commands|shortcuts)|what\s+commands\s+do\s+you\s+know|有哪些指令|我的指令|指令列表)$")
_STEP_SPLIT = re.compile(r"\s*(?:,\s*(?:а\s+)?потом|,\s*затем|\s+потом|\s+затем|\s+и\s+потом|,\s*(?:and\s+)?then|\s+and\s+then|\s+then|，?\s*然后|，\s*再|[,，、])\s*")
_TYPING_STEP = re.compile(r"^(?:напиши|напечатай|введи|набери|отправь|скинь|type|write|send|text|输入|发)\b")
_STARTS_COMMAND = re.compile(r"^(?:открой|запусти|включи|закрой|нажми|кликни|напиши|напечатай|найди|громкость|"
                             r"сделай|прокрути|пауза|заблокируй|прими|open|launch|close|click|press|type|search|"
                             r"find|volume|scroll|pause|play|lock|打开|关闭|点击|输入|搜索|音量)\b")


@functools.lru_cache(maxsize=1024)
def _template_regex(template: str) -> re.Pattern | None:
    """'найди погоду в {город}' -> ^найди погоду в\\s*(?P<p0>.+?)$ ; None if too vague."""
    parts = re.split(r"\{(\w+)\}", template)
    pieces, literal = [], ""
    for i, part in enumerate(parts):
        if i % 2 == 0:
            lit = normalize(part)
            literal += lit
            if lit:
                pieces.append(re.escape(lit))
        else:
            pieces.append(f"(?P<p{i // 2}>.+?)")
    if len(literal.replace(" ", "")) < 4:  # "{x}" alone would swallow every phrase
        return None
    return re.compile("^" + r"\s*".join(pieces) + "$")


def _tokens(template: str) -> list[tuple[str, str]]:
    """'отправь {текст} {кому}' -> [("lit", "отправь"), ("var", "текст"), ("var", "кому")]"""
    out = []
    for i, part in enumerate(re.split(r"\{(\w+)\}", template)):
        if i % 2:
            out.append(("var", part))
        else:
            out += [("lit", w) for w in normalize(part).split()]
    return out


def _words(phrase: str) -> list[tuple[str, str]]:
    """Words as spoken plus their normalized form; filler-only words are dropped."""
    return [(w, normalize(w)) for w in phrase.split() if normalize(w)][:80]


def _assignments(tokens, words, i=0, j=0, cur=None, budget=None):
    """Every way the phrase's words can fill the template's placeholders."""
    cur = {} if cur is None else cur
    budget = [300] if budget is None else budget
    if budget[0] <= 0:
        return
    if i == len(tokens):
        if j == len(words):
            budget[0] -= 1
            yield dict(cur)
        return
    kind, val = tokens[i]
    if kind == "lit":
        if j < len(words) and words[j][1] == val:
            yield from _assignments(tokens, words, i + 1, j + 1, cur, budget)
        return
    for k in range(j + 1, len(words) + 1):
        cur[val] = words[j:k]
        yield from _assignments(tokens, words, i + 1, k, cur, budget)
    cur.pop(val, None)


def _value(words) -> str:
    """Placeholder text exactly as said ("Привет, я Нова"), minus quotes and edge punctuation."""
    return " ".join(w for w, _ in words).strip(" «»\"'“”,;:").rstrip(".")

def _fill(value, values: dict):
    """Put captured values into "{name}" placeholders inside learned actions."""
    if isinstance(value, str):
        for name, v in values.items():
            value = value.replace("{" + name + "}", v)
        return value
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, values) for k, v in value.items()}
    return value


def _leaves(value) -> list[str]:
    if isinstance(value, dict):
        return [x for k in sorted(value) for x in [k] + _leaves(value[k])]
    if isinstance(value, list):
        return [x for v in value for x in _leaves(v)]
    return [normalize(str(value))]


def _same_actions(a, b) -> bool:
    """Loose equality: "погоду в киеве" ~ "погода в Киеве", but "треть" != "33"."""
    from rapidfuzz import fuzz

    la, lb = _leaves(a), _leaves(b)
    return len(la) == len(lb) and all(fuzz.ratio(x, y) >= 80 for x, y in zip(la, lb))


def _rule(value, key: str) -> dict:
    """Upgrade rules saved by older versions (a bare list of actions); drop broken ones."""
    if isinstance(value, list):
        value = {"actions": value, "example": "" if "{" in key else key, "hits": 0, "last": 0.0}
    if not isinstance(value, dict) or not isinstance(value.get("actions"), list):
        return {"actions": [], "example": "", "hits": 0, "last": 0.0}
    value["actions"] = [a for a in value["actions"] if isinstance(a, dict) and isinstance(a.get("skill"), str)]
    for number in ("hits", "last", "fails"):
        if not isinstance(value.get(number), (int, float)) or isinstance(value.get(number), bool):
            value[number] = 0
    return value


def _load(path) -> dict:
    data = config.read_json(path, {})
    return data if isinstance(data, dict) else {}


def _save(path, data: dict) -> None:
    try:
        config.write_json(path, data)
    except OSError:  # disk full: the command itself still works
        log.exception("Saving %s failed", path.name)


class Router:
    def __init__(self, cfg: dict, memory=None, knowledge=None):
        self.cfg = cfg
        self.brain = Brain(cfg)
        self.memory = memory  # nova.vision.VisualMemory, None in tests
        self.knowledge = knowledge  # nova.knowledge.Knowledge
        self.brain.knowledge = knowledge
        self.macros: dict[str, list[str]] = {k: v for k, v in _load(MACROS_PATH).items()
                                             if isinstance(v, list) and v and all(isinstance(s, str) for s in v)}
        # Learned by watching the AI: template ("найди погоду в {город}") ->
        # {"actions", "example" (the phrase it was learned from), "hits", "last"}.
        self.learned: dict[str, dict] = {k: r for k, v in _load(LEARNED_PATH).items() if (r := _rule(v, k))["actions"]}
        self.history: list[tuple[str, str]] = []
        self.just_learned = ""
        self.last: dict = {}  # what was done last, for "не туда"

    def _save_learned(self) -> None:
        _save(LEARNED_PATH, self.learned)

    def _match_learned(self, t: str, raw: str = "") -> tuple[str, list[dict]] | None:
        from rapidfuzz import fuzz, process

        words = _words(raw or t)
        # Most specific template first, so "открой {x} в хроме" beats "открой {x}".
        for template in sorted(list(self.learned), key=lambda k: -len(re.sub(r"\{\w+\}", "", k))):
            if "{" not in template:
                if template == t:
                    return template, self.learned[template]["actions"]
                continue
            regex = _template_regex(template)
            if not regex or not regex.match(t):
                continue  # can't be this template: skip the costly word-by-word fitting
            options = list(_assignments(_tokens(template), words))
            if not options:
                continue
            # Several ways to split "отправь {текст} {кому}": take the one shaped
            # like the phrase the rule was learned from.
            shape = self.learned[template].get("shape", {})
            best = min(options, key=lambda a: sum(abs(len(v) - shape.get(k, len(v))) for k, v in a.items()))
            values = {k: _value(v) for k, v in best.items()}
            log.info("Learned: %s %s", template, values)
            return template, _fill(self.learned[template]["actions"], values)
        # Same request in slightly different words ("открой мне почту" ~ "открой почту").
        exact = [k for k in list(self.learned) if "{" not in k]
        # Not for rules that type, send or press: "напиши папе" must never replay "напиши маме".
        exact = [k for k in exact if not any(a.get("skill") in _RISKY_REPLAY for a in self.learned[k]["actions"])]
        hit = process.extractOne(t, exact, scorer=fuzz.ratio, score_cutoff=95)
        if hit and not _same_meaning(t, hit[0]):
            hit = None
        if not hit:
            # Whisper glues short words together ("прими игру" -> "примегру"): compare
            # without spaces, but only when the phrase really lost a space.
            squeezed = {k.replace(" ", ""): k for k in exact if len(k) <= 30 and k.count(" ") > t.count(" ")}
            near = process.extractOne(t.replace(" ", ""), list(squeezed), scorer=fuzz.ratio, score_cutoff=80)
            hit = (squeezed[near[0]], near[1]) if near and _same_meaning(t, squeezed[near[0]]) else None
        if hit:
            log.info("Learned (fuzzy): %s", hit[0])
            return hit[0], self.learned[hit[0]]["actions"]
        return None

    def common_phrases(self, limit: int = 12) -> list[str]:
        """The user's own commands and the most used learned ones: hints for Whisper."""
        learned = sorted((k for k in self.learned if "{" not in k),
                         key=lambda k: -self.learned[k].get("hits", 0))
        return (list(self.macros) + learned)[:limit]

    def _learn(self, t: str, answer: dict, actions: list[dict], raw: str = "") -> None:
        if any(getattr(get(a.get("skill", "")), "dangerous", True) for a in actions):
            return  # unknown or dangerous skills always go through the AI + confirmation
        if any(a.get("skill") == "confirm" for a in actions):
            # Sending, paying, posting: a replay would ask about what was said, not what's
            # on screen (the wrong chat could be open). The AI looks every time instead.
            return
        key, rule_actions, shape = t, actions, {}
        learn = answer.get("learn")
        if isinstance(learn, dict) and isinstance(learn.get("template"), str) and learn.get("actions"):
            template = re.sub(r"\s+", " ", learn["template"].lower()).strip()
            if _template_regex(template):
                # Keep the template only if replaying it on this very phrase gives
                # what the AI actually did (catches "треть" -> 33 style conversions).
                for option in _assignments(_tokens(template), _words(raw or t)):
                    template_actions = _drop_redundant_confirms(learn["actions"])
                    if _same_actions(_fill(template_actions, {k: _value(v) for k, v in option.items()}), actions):
                        key, rule_actions = template, _drop_redundant_confirms(learn["actions"])
                        shape = {k: len(v) for k, v in option.items()}
                        break
        # The AI's template doesn't know where its clicks landed; the executed steps do.
        if len(rule_actions) == len(actions):
            for rule, done_step in zip(rule_actions, actions):
                app = (done_step.get("args") or {}).get("app")
                if rule.get("skill") == "click" == done_step.get("skill") and app:
                    rule.setdefault("args", {}).setdefault("app", app)
        # A broader rule replaces the narrower ones it covers ("открой {x}" makes
        # "открой хром" redundant), so rules don't pile up as near-duplicates.
        hits = 0
        for old in list(self.learned):
            example = self.learned[old].get("example") or old
            option = "{" in key and "{" not in old and next(_assignments(_tokens(key), _words(example)), None)
            # "прими {что}" covers "прими игру" only if it would do the same thing there.
            covered = bool(option) and _same_actions(
                _fill(rule_actions, {k: _value(v) for k, v in option.items()}), self.learned[old]["actions"])
            if old == key or covered:
                hits += self.learned.pop(old).get("hits", 0)
        self.learned[key] = {"actions": rule_actions, "example": t, "shape": shape, "hits": hits,
                             "last": time.time()}
        self.just_learned = key
        # Bounded: drop the least used, least recent rules beyond the limit.
        if len(self.learned) > MAX_RULES:
            for old in sorted(self.learned, key=lambda k: (self.learned[k]["hits"], self.learned[k]["last"]))[
                    :len(self.learned) - MAX_RULES]:
                del self.learned[old]
        self._save_learned()
        log.info("Learned rule: %s", key)
    def _correct(self, ctx: Context, on_status) -> str:
        """The user said "не туда" right after an action."""
        last = self.last
        if last.get("type") == "rule":
            self.learned.pop(last["key"], None)
            self._save_learned()
            self.last = {}
            return tr("rule_forgotten")
        if last.get("type") == "visual":
            from .screen import bring_to_front
            from .skills.vision import click_visual

            if self.memory is not None and last.get("pattern"):
                self.memory.feedback(last["pattern"], False)  # a wrong look loses trust, then goes
            on_status("thinking")
            origin, where = ctx.foreground_hwnd, last.get("hwnd") or ctx.foreground_hwnd
            if where != origin and not bring_to_front(where):
                return tr("nothing_to_fix")
            ctx.foreground_hwnd = where  # the click happened in Telegram, not in the game
            try:
                click_visual(ctx, last["target"], avoid=last.get("avoid", []) + [last["point"]])
            except SkillError as e:
                return str(e)
            finally:
                if where != origin:
                    bring_to_front(origin)
            self.last = ctx.last_action
            return ""
        return tr("nothing_to_fix")
    def is_command(self, t: str) -> bool:
        """Would this (normalized) phrase run something without the AI?"""
        t = normalize(t)
        return bool(DISMISS.match(t)) or t in self.macros or t in self.learned or any(
            p.match(t) for s in REGISTRY if s.priority >= 0 for p in s.patterns)

    def handle(self, text: str, ctx: Context, on_status=lambda s: None) -> str:
        """Run a phrase and return what to say back (may be empty)."""
        t = strip_name(text, self.cfg["name"])
        if not t:
            return tr("listening")
        if DISMISS.match(t):
            ctx.extra["dismissed"] = True
            return ""
        ctx.brain, ctx.memory, ctx.knowledge = self.brain, self.memory, self.knowledge
        self.brain.cancel = ctx.extra.get("cancel")  # Esc stops a running AI call too
        ask_yes, ask_open = ctx.confirm, ctx.ask

        def confirm(question):
            ctx.extra["asked_user"] = True
            return ask_yes(question)

        def ask(question):
            ctx.extra["asked_user"] = True
            return ask_open(question)

        ctx.confirm, ctx.ask = confirm, ask
        ctx.extra["raw"] = strip_name_raw(text, self.cfg["name"])
        origin = ctx.foreground_hwnd
        if _CORRECT.match(t):
            return self._correct(ctx, on_status)
        ctx.last_action = {}
        # Macros need the raw punctuation (":" and ",") that normalize() drops.
        raw = strip_name_raw(text, self.cfg["name"])
        if m := _REMEMBER.match(raw):
            reply = self._remember(m["rest"], ctx)
        else:
            # The AI gets the phrase as spoken (case, punctuation), minus the name:
            # "Привет, я Нова" must be typed exactly like that.
            reply = self._handle(t, ctx, on_status, raw=strip_name_raw(text, self.cfg["name"]))
        if ctx.last_action:
            self.last = ctx.last_action
        if ctx.extra.pop("restore_focus", False):
            _restore_focus(origin)
        self.history = (self.history + [(text, reply)])[-6:]
        return reply

    def _remember(self, rest: str, ctx: Context | None = None) -> str:
        original = rest
        rest = rest.lower().replace("ё", "е")
        parts = re.split(r"\s*(?::|：|\s—\s|\s-\s|\bэто\b|\bзначит\b|\bmeans\b|\bis\b)\s*", rest, maxsplit=1)
        if len(parts) < 2:
            parts = _FIRST_VERB.split(rest, maxsplit=1)
        if len(parts) < 2:
            # "记住：我的生日是5月1日": nothing that looks like a command name and steps — a fact.
            if ctx is not None and original.strip():
                return self._run_skill("remember_fact", {"text": original.strip(" :：")}, ctx)
            return tr("macro_hint")
        name = normalize(parts[0])
        steps = []
        for piece in (normalize(s) for s in _STEP_SPLIT.split(re.sub(r"\s+", " ", parts[1]))):
            # "напиши вове привет, как дела": the comma is inside the message, not a new step.
            if steps and _TYPING_STEP.match(steps[-1]) and not _STARTS_COMMAND.match(piece):
                steps[-1] += " " + piece
            elif piece:
                steps.append(piece)
        if not name or not steps:
            return tr("macro_what")
        if name in steps:
            return tr("macro_what")  # "запомни пауза: пауза" would call itself forever
        if not any(p.match(steps[0]) for s in REGISTRY for p in s.patterns) and steps[0] not in self.macros:
            # "Вова — это мой брат", "my wifi is 1234": nothing to run, so it's a fact.
            if ctx is not None:
                return self._run_skill("remember_fact", {"text": original.strip()}, ctx)
            return tr("macro_what")
        self.macros[name] = steps
        _save(MACROS_PATH, self.macros)
        return tr("macro_saved", name=name, n=len(steps))

    def _handle(self, t: str, ctx: Context, on_status, raw: str = "") -> str:
        if m := _FORGET.match(t):
            name = re.sub(r"^(?:the\s+)?(?:command|shortcut)\s+", "", normalize(m["name"]))
            existed = self.macros.pop(name, None) is not None or self.learned.pop(name, None) is not None
            if existed or not re.match(r"^(?:forget\s|忘记)", t):
                _save(MACROS_PATH, self.macros)
                _save(LEARNED_PATH, self.learned)
                return tr("deleted", name=name) if existed else tr("no_command", name=name)
            # "forget my birthday": not a command of that name — a fact for forget_fact.
        if _LIST.match(t):
            if not self.macros and not self.learned:
                return tr("no_commands")
            return tr("commands_count", m=len(self.macros), l=len(self.learned))

        name = re.sub(r"^(?:запусти|выполни|включи|run|start|do|执行|运行)\s*", "", t)
        for key in (t, name):
            if key in self.macros:
                return self._run_macro(self.macros[key], ctx, on_status)
        if (found := self._match_learned(t, raw)) is not None:
            key, actions = found
            rule = self.learned[key]
            rule["hits"], rule["last"] = rule.get("hits", 0) + 1, time.time()
            # The AI wrote this rule, so its steps get the same checks as the AI's own.
            reply, ok, _ = self._run_steps(actions, ctx, [], from_ai=True)
            cancelled = reply == tr("cancelled") or (ctx.extra.get("cancel") and ctx.extra["cancel"].is_set())
            if ok or cancelled:
                rule["fails"] = 0
                self._save_learned()  # a visual click may have just told the rule its app
                # A visual click inside the rule is the more precise thing to correct.
                ctx.last_action = ctx.last_action or {"type": "rule", "key": key}
                return reply
            # The button moved, the app is gone...: don't repeat the same error forever.
            rule["fails"] = rule.get("fails", 0) + 1
            if rule["fails"] >= 2:
                self.learned.pop(key, None)
                log.info("Learned rule dropped after failing twice: %s", key)
            self._save_learned()
            log.info("Learned rule failed (%s), trying the usual way", reply)

        if (reply := self._fast_path(t, ctx)) is not None:
            return reply
        return self._ask_brain(t, raw or t, ctx, on_status)

    def _fast_path(self, t: str, ctx: Context) -> str | None:
        for skill in REGISTRY:
            for pattern in skill.patterns:
                m = pattern.match(t)
                if not m:
                    continue
                args = {k: v for k, v in m.groupdict().items() if v is not None}
                log.info("Fast path: %s %s", skill.name, args)
                try:
                    return self._run_skill(skill.name, args, ctx, raise_errors=True)
                except SkillError as e:
                    # A catch-all like "открой ..." failed on a compound request
                    # ("открой хром и найди погоду") — let the AI sort it out. Not after
                    # the user said "no" or already answered a question: that's final.
                    final = str(e) == tr("cancelled") or ctx.extra.get("asked_user")
                    if skill.priority < 0 and self.cfg["brain"] != "off" and not final:
                        log.info("Fast path gave up (%s), asking the AI", e)
                        return None
                    ctx.extra["step_failed"] = True
                    return str(e)
        return None

    def _run_skill(self, name: str, args: dict, ctx: Context, raise_errors: bool = False) -> str:
        skill = get(name)
        if not skill:
            if raise_errors:
                raise SkillError(tr("cant_do", name=name))
            return tr("cant_do", name=name)
        if skill.dangerous and (self.cfg.get("confirm_dangerous", True) or ctx.extra.get("low_trust")):
            if not ctx.confirm(tr("confirm")):
                return tr("cancelled")
        elif (ctx.extra.get("low_trust") and name in _ACTS and name not in _SELF_CONFIRMING
              and not ctx.extra.get("from_ai")):
            # Heard while the speakers play (a colleague on a call, a video): even an
            # instant "нажми отправить" asks first.
            if not ctx.confirm(tr("confirm_action_heard", what=_describe(name, args))):
                return tr("cancelled")
        if skill.background:
            ctx.extra["restore_focus"] = True
        try:
            return skill.run(ctx, **args) or ""
        except SkillError as e:
            if raise_errors:
                raise
            return str(e)
        except TypeError as e:  # wrong args from the AI
            log.warning("Bad args for %s: %s", name, e)
            if raise_errors:  # a broken step must stop the chain, not let the next one type blind
                raise SkillError(tr("failed", name=name)) from None
            return tr("failed", name=name)
        except Exception:
            log.exception("Skill %s crashed", name)
            if raise_errors:
                raise SkillError(tr("error")) from None
            return tr("error")

    def _run_actions(self, actions: list[dict], ctx: Context) -> str:
        return self._run_steps(actions, ctx, [])[0]

    def _run_steps(self, actions: list[dict], ctx: Context, done: list[str],
                   from_ai: bool = False, prior: list[dict] = ()) -> tuple[str, bool, int]:
        """Run actions in order; stop at the first failure (typing into the wrong
        window is worse than stopping). Returns (reply, all_ok, number_done)."""
        replies = []
        for i, action in enumerate(actions):
            name, args = action.get("skill", ""), action.get("args") or {}
            if ctx.extra.get("cancel") and ctx.extra["cancel"].is_set():
                return tr("cancelled"), False, i
            pending = ctx.extra.get("pending_confirm") if from_ai else None
            if pending and name == "confirm":
                ctx.extra.pop("pending_confirm", None)
            elif pending and name not in _NAVIGATION and name not in ("look", "wait"):
                # The AI dropped the confirm it planned before looking: ask it ourselves.
                ctx.extra.pop("pending_confirm", None)
                if not ctx.confirm(str((pending.get("args") or {}).get("question") or tr("confirm"))):
                    done.append("confirm -> the user said no")
                    return tr("cancelled"), False, i
                ctx.extra["screen_ok"] = True
            if from_ai and _needs_user_ok(name, args, ctx, list(prior) + actions[:i]):
                question = "confirm_action_heard" if ctx.extra.get("low_trust") else "confirm_action"
                if not ctx.confirm(tr(question, what=_describe(name, args))):
                    done.append(f"{name} -> the user said no")
                    return tr("cancelled"), False, i
                ctx.extra["screen_ok"] = True
            before = ctx.last_action
            ctx.last_action = {}  # so what follows describes this step only
            ctx.extra["from_ai"] = from_ai  # skills may read the user's own words only when not
            try:
                r = self._run_skill(name, args, ctx, raise_errors=True)
            except SkillError as e:
                ctx.last_action = ctx.last_action or before
                done.append(f"{name} {json.dumps(args, ensure_ascii=False)} -> FAILED: {e}")
                ctx.extra["step_failed"] = True
                return str(e), False, i
            if not ctx.last_action:
                ctx.last_action = before  # keys, waits: "не туда" still means the last click
            elif name == "click" and not args.get("app") and ctx.last_action.get("type") == "visual":
                # A button found by sight belongs to its app (a game's ACCEPT): remember
                # that, so the learned rule finds it from any window next time.
                args["app"] = ctx.last_action.get("app")
                action["args"] = args
            done.append(f"{name} {json.dumps(args, ensure_ascii=False)} -> {r or 'ok'}")
            if r:
                replies.append(r)
            # "открой телеграм" moves focus: later clicks and typing must target the new window.
            ctx.foreground_hwnd = _foreground() or ctx.foreground_hwnd
        return ". ".join(replies), True, len(actions)

    def _run_macro(self, steps: list[str], ctx: Context, on_status) -> str:
        depth = ctx.extra.get("macro_depth", 0)
        if depth >= 3:  # "запомни музыку: включи музыку" would call itself forever
            return tr("error")
        ctx.extra["macro_depth"] = depth + 1
        replies = []
        try:
            for step in steps:
                if ctx.extra.get("cancel") and ctx.extra["cancel"].is_set():
                    return tr("cancelled")  # Esc stops the rest of the macro
                ctx.extra.pop("step_failed", None)
                r = self._handle(normalize(step), ctx, on_status)
                if r:
                    replies.append(r)
                if ctx.extra.get("step_failed"):
                    break  # a failed step: the next ones would act in the wrong place
        finally:
            ctx.extra["macro_depth"] = depth
        return ". ".join(replies)

    def _ask_brain(self, t: str, raw: str, ctx: Context, on_status) -> str:
        """Let the AI work step by step: act, look at the screen, act again."""
        if self.cfg["brain"] == "off":
            return tr("not_understood")
        from .screen import capture_window

        done: list[str] = []
        executed: list[dict] = []
        failures = 0
        finished = False
        last_round: list[dict] = []
        image = None
        known = None
        if self.memory is not None and ctx.foreground_hwnd:
            from .screen import app_name

            app = app_name(ctx.foreground_hwnd)
            known = (app, self.memory.labels(app))
        answer = {"say": "", "actions": []}
        for step in range(MAX_STEPS):
            on_status("thinking")
            if self.memory is not None and ctx.foreground_hwnd and step:
                from .screen import app_name  # "открой телеграм" moved us: its buttons now

                app = app_name(ctx.foreground_hwnd)
                known = (app, self.memory.labels(app))
            try:
                answer = self.brain.ask(raw, self.history, image_b64=image, progress=done, known=known)
            except BrainError as e:
                return str(e)
            actions = _drop_redundant_confirms([a for a in answer["actions"] if isinstance(a, dict)])
            if image is not None:
                ctx.extra["saw_screen"] = True  # screen text could have steered this step
            planned = actions
            actions, held = _hold_before_confirm(actions, looked=image is not None)
            if held:
                answer["continue"] = True
                held_confirm = next((a for a in planned[len(actions):] if a.get("skill") == "confirm"), None)
                if held_confirm:
                    ctx.extra["pending_confirm"] = held_confirm
            result, ok, n = self._run_steps(actions, ctx, done, from_ai=True, prior=executed)
            if held and ok:
                done.append("Nova paused here: check on the screenshot that the right recipient/place is open "
                            "and the text is right, then start the next step with confirm.")
            executed += actions[:n]
            last_round = actions[:n]
            if not ok:
                # Like a person would: a step failed, so the AI hears why and tries another
                # way (the official name, another route). Never after the user said "no".
                failures += 1
                cancelled = ctx.extra.get("cancel") and ctx.extra["cancel"].is_set()
                if result == tr("cancelled") or cancelled or failures > 2 or step == MAX_STEPS - 1:
                    return result
                image = None
                continue
            if answer.get("continue") not in (True, "true", "True", 1) or (not actions and not held):
                finished = True
                break
            time.sleep(0.5)  # let the window react before looking at it
            try:
                image = capture_window(ctx.foreground_hwnd).to_jpeg_b64()[0]
            except SkillError as e:  # a password window came up: stop here
                return str(e)
            log.info("Agent step %d done, looking at the screen", step + 1)
        # Remember "do something" requests so next time they skip the AI (replays get the
        # same checks as the AI). Not "facts" a page may have suggested, and nothing from a
        # voice that may not have been the user's.
        steered = ctx.extra.get("saw_screen") and any(
            a.get("skill") in ("remember_fact", "forget_fact") for a in executed)
        if not finished:
            ctx.extra.pop("pending_confirm", None)
            return tr("not_finished")  # never "Отправила" when the last steps never ran
        if failures:
            # Earlier attempts went the wrong way ("open steam", then FAILED). Learn only
            # if the AI's own template reproduces what finally worked; if it gave up, nothing.
            executed = executed if answer.get("actions") and answer.get("learn") else []
        if executed and finished and not steered and not ctx.extra.get("low_trust"):
            self.just_learned = ""
            self._learn(t, answer, executed, raw)
            if self.just_learned and ctx.last_action.get("type") != "visual":
                ctx.last_action = {"type": "rule", "key": self.just_learned}  # "не туда" undoes it
        return " ".join(x for x in (answer["say"], result) if x).strip()


_NAVIGATION = {"open", "wait"}
# Words that flip a command: "включи" vs "выключи" look alike but mean the opposite.
_POLARITY = {"включи", "выключи", "открой", "закрой", "громче", "тише", "вверх", "вниз", "больше", "меньше",
             "прими", "отклони", "запусти", "останови", "on", "off", "open", "close", "up", "down", "start",
             "stop", "accept", "decline", "louder", "quieter", "mute", "unmute"}


def _same_meaning(a: str, b: str) -> bool:
    """Close enough to replay b's rule for a: same numbers, same direction words."""
    def direction(s: str) -> set[str]:
        glued = s.replace(" ", "")
        return {w for w in _POLARITY if w in s.split() or glued.startswith(w)}

    da, db = direction(a), direction(b)
    # Both name a direction and they differ ("включи" vs "выключи"): never the same.
    # One side lost it to a glued word ("примегру"): the rest of the check decides.
    return re.findall(r"\d+", a) == re.findall(r"\d+", b) and not (da and db and da != db)
_RISKY_REPLAY = {"send_message", "type_text", "press_key", "confirm", "power"}
_SHELLISH = re.compile(r"\b(?:powershell|pwsh|cmd|del|rm|rmdir|format|reg|curl|wget|iwr|irm|iex|invoke-\w+|"
                       r"start-process|shutdown|taskkill|bcdedit|diskpart|net\s+user|mshta|certutil|bitsadmin|"
                       r"rundll32|regsvr32|msiexec|schtasks|wmic|vssadmin|cipher|takeown|icacls|"
                       r"set-mppreference|add-mppreference|-enc(?:odedcommand)?)\b|&&|\|\s*\w|\$env:|%\w+%",
                       re.I)
# Typing into these runs commands: everything the AI types there needs a "yes".
_TERMINALS = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe", "openconsole.exe",
              "conhost.exe", "bash.exe", "wsl.exe", "mintty.exe", "alacritty.exe", "wezterm-gui.exe",
              "putty.exe", "regedit.exe", "mmc.exe", "explorer.exe", "taskmgr.exe"}
# Keys the AI may press without asking: editing and moving around, nothing that runs or closes.
_SAFE_KEYS = {"enter", "esc", "escape", "tab", "space", "backspace", "delete", "up", "down", "left", "right",
              "home", "end", "pageup", "pagedown", "page up", "page down", "shift+enter", "ctrl+enter",
              "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+a", "ctrl+f", "ctrl+s", "ctrl+t",
              "ctrl+k", "f5", "play/pause media", "next track", "previous track",
              *"abcdefghijklmnopqrstuvwxyz0123456789"}
# Text that starts something when Enter follows: drive paths, network shares, URL schemes.
_LAUNCHY = re.compile(r"(?:^|\s)(?:[a-z]:[\\/]|\\\\|%\w+%|(?:file|ms-[\w-]+|search-ms|javascript|vbscript|"
                      r"shell|data|mshta|steam|powershell)\s*:)|\.(?:exe|bat|cmd|ps1|vbs|js|jse|wsf|lnk|scr|msi|"
                      r"hta|cpl|reg|com|pif)\b|\b(?:wscript|cscript|net1)\b|\s&\s", re.I)
_TERMINAL_WORDS = re.compile(r"терминал|консол|командн|powershell|\bcmd\b|terminal|console|shell|реестр|"
                             r"registry|终端|命令", re.I)
# Buttons the AI may not press on its own: they delete, pay, send or publish.
_RISKY_CLICK = re.compile(r"удал|стер|очист|сброс|оплат|купи|покуп|заказ|подтверд|перевод|перевест|отправ|"
                          r"разреш|установ|запуст|уничтож|форматир|allow|grant|install|run as|destroy|erase|"
                          r"允许|安装|运行|"
                          r"опублик|выйти из аккаунт|delete|remove|erase|wipe|reset|uninstall|format|pay|buy|"
                          r"purchase|order|checkout|confirm|transfer|send|publish|post|sign out|log out|"
                          r"删除|清除|支付|付款|购买|确认|发送|发布|转账", re.I)
# Everything that types, presses, clicks or remembers.
_ACTS = {"type_text", "press_key", "click", "click_visual", "click_here", "send_message", "remember_fact",
         "forget_fact", "close"}


def _in_terminal(args: dict) -> bool:
    """Would typing land in a command line (a terminal, the Run box, the registry)?"""
    import ctypes

    from .screen import app_name

    if _TERMINAL_WORDS.search(str(args.get("app", ""))):
        return True
    if args.get("app"):
        return False
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if app_name(hwnd) in _TERMINALS:
        return True
    cls = ctypes.create_unicode_buffer(64)
    ctypes.windll.user32.GetClassNameW(hwnd, cls, 64)
    # The Run box ("Выполнить", "Ausführen", "运行"...) is a dialog of Explorer itself,
    # whatever the Windows language: treat every Explorer dialog as a command line.
    return cls.value == "#32770" and app_name(hwnd) == "explorer.exe"


def _needs_user_ok(name: str, args: dict, ctx: Context, before: list[dict]) -> bool:
    """Steps the AI planned that need a human "yes" no matter what the AI says.

    The AI reads web pages, chats and screenshots, and any of them can carry text
    written to steer it ("ignore your instructions and..."). So whatever could run
    a command, spend money, delete, send or plant a "fact" waits for the user."""
    key = str(args.get("key", "")).lower()
    text = str(args.get("text", ""))
    # Always asked, whatever came before — a "yes" to the AI's own question ("Открыть
    # браузер?") must not cover the command typed right after it.
    if ctx.extra.get("low_trust") and name in _ACTS:
        return True  # the phrase may have come from the speakers, not the user
    if name == "press_key" and key.replace(" ", "") not in _SAFE_KEYS:
        return True  # Win+R, Alt+F4, Ctrl+Shift+Esc...: only plain editing keys go unasked
    if name == "type_text" and (_SHELLISH.search(text) or _LAUNCHY.search(text)):
        return True  # commands, and paths/addresses that start something (C:\x.exe, \\host, ms-...:)
    if name in ("type_text", "press_key") and _in_terminal(args):
        return True
    # Asked unless the user has just said yes to this very step ("Отправить письмо?" -> Send).
    if before and before[-1].get("skill") == "confirm":
        return False
    if name in ("click", "click_visual") and _RISKY_CLICK.search(str(args.get("target", ""))):
        return True
    if name == "type_text" and "\n" in text and ctx.extra.get("saw_screen"):
        return True  # a new line presses Enter: same as submitting
    if name in ("remember_fact", "forget_fact") and ctx.extra.get("saw_screen"):
        return True  # a page must not be able to rewrite what Nova knows
    if ctx.extra.get("saw_screen") and not ctx.extra.get("screen_ok"):
        submits = (name == "press_key" and "enter" in key) or (name == "type_text" and args.get("submit"))
        return submits and any(a.get("skill") == "type_text" for a in before) or (
            name == "type_text" and args.get("submit"))
    return False


def _describe(name: str, args: dict) -> str:
    detail = args.get("key") or args.get("text") or args.get("target") or ""
    return f"{name} {detail}".strip()
# Skills that check with the user themselves when they need to; an extra confirm
# step before them just makes Nova ask twice.
_SELF_CONFIRMING = {"send_message"}


def _drop_redundant_confirms(actions: list[dict]) -> list[dict]:
    out = []
    for i, a in enumerate(actions):
        nxt = actions[i + 1].get("skill") if i + 1 < len(actions) else None
        if a.get("skill") == "confirm" and nxt in _SELF_CONFIRMING:
            continue
        out.append(a)
    return out


def _hold_before_confirm(actions: list[dict], looked: bool) -> tuple[list[dict], bool]:
    """Never let the AI reach an irreversible step blind.

    A "confirm" (send, post, pay...) may only be the first action of a step that
    starts from a fresh screenshot. Before the first look, only navigation runs.
    Returns (actions to run now, whether the rest was held back for a look).
    """
    names = [a.get("skill") for a in actions]
    if "confirm" not in names:
        return actions, False
    if not looked:
        safe = []
        for a in actions:
            if a.get("skill") not in _NAVIGATION:
                break
            safe.append(a)
        return safe, True
    i = names.index("confirm")
    return (actions, False) if i == 0 else (actions[:i], True)


def _restore_focus(hwnd: int) -> None:
    """Put the user back where they were (the game) after a background task."""
    import ctypes

    user32 = ctypes.windll.user32
    if hwnd and user32.IsWindow(hwnd) and user32.GetForegroundWindow() != hwnd:
        from .screen import bring_to_front

        # (exclusive-fullscreen games minimise when they lose focus; this restores them too)
        log.info("Focus returned to the user's window: %s", bring_to_front(hwnd))


def _foreground() -> int:
    import ctypes

    return ctypes.windll.user32.GetForegroundWindow()