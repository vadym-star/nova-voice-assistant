"""Skill registry.

A skill is one thing Nova can do on the PC. Each skill declares regex patterns
for the instant (no-AI) path and a description + params so the AI brain can
call it too. To add a skill, drop a module into this package and decorate a
function with @skill — see CONTRIBUTING.md.
"""

import importlib
import pkgutil
import re
from dataclasses import dataclass, field
from typing import Any, Callable


class SkillError(Exception):
    """Raised by a skill with a message that Nova says out loud."""


@dataclass
class Context:
    """What a skill gets besides its own arguments."""

    config: dict
    # Window that was active when the user pressed the shortcut (before Nova's overlay showed up).
    foreground_hwnd: int = 0
    # Set by the app: ask the user a yes/no question by voice.
    confirm: Callable[[str], bool] = lambda question: False
    # Set by the app: ask an open question by voice and get the spoken answer.
    ask: Callable[[str], str] = lambda question: ""
    # Set by the router: the AI backend, the visual memory (nova.vision.VisualMemory)
    # and what Nova knows about people and facts (nova.knowledge.Knowledge).
    brain: Any = None
    memory: Any = None
    knowledge: Any = None
    # A skill records what it just did here, so "не туда" can correct it.
    last_action: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class Skill:
    name: str
    description: str
    params: dict[str, str]
    patterns: list[re.Pattern]
    run: Callable[..., Any]
    dangerous: bool = False
    # Higher runs first on the fast path; catch-all patterns like "открой ..." use -1.
    priority: int = 0
    # The user doesn't need to see the result (sending a message while gaming): if the
    # skill had to bring another window up, Nova puts the user's window back afterwards.
    background: bool = False


REGISTRY: list[Skill] = []


def skill(name: str, description: str, patterns: tuple[str, ...] = (),
          params: dict[str, str] | None = None, dangerous: bool = False, priority: int = 0,
          background: bool = False):
    """Register `fn(ctx, **params) -> str | None`. The returned text is spoken."""

    def wrap(fn):
        REGISTRY.append(Skill(
            name=name, description=description, params=params or {},
            patterns=[re.compile(p) for p in patterns], run=fn, dangerous=dangerous,
            priority=priority, background=background,
        ))
        REGISTRY.sort(key=lambda s: -s.priority)
        return fn

    return wrap


def get(name: str) -> Skill | None:
    return next((s for s in REGISTRY if s.name == name), None)


def load_all() -> None:
    for mod in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{mod.name}")
