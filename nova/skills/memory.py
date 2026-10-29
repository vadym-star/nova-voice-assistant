""""Запомни, что Вова — это Вовчикинатор": facts and contact names the user tells Nova."""

import re

from ..i18n import t
from . import SkillError, skill


def _as_said(ctx, fallback: str) -> str:
    """The fact as spoken (capitals kept), not the normalized lowercase text."""
    raw = ctx.extra.get("raw", "")
    m = re.search(r"(?:что|that|记住)\s*,?\s*(.+)$", raw, re.I)
    return m.group(1).strip() if m else fallback


@skill(
    "remember_fact",
    "Запомнить факт о пользователе или его окружении (кто есть кто, как зовут в мессенджере, предпочтения). "
    "Например «Вова — это Вовчикинатор в Телеграме», «Вова — мой брат».",
    patterns=(r"^запомни\s+что\s+(?P<text>.+)$",
              r"^remember\s+that\s+(?P<text>.+)$",
              r"^记住\s*(?P<text>[^：:]+)$"),
    params={"text": "факт одним коротким предложением"},
)
def remember_fact(ctx, text: str):
    if ctx.knowledge is None:
        raise SkillError(t("error"))
    ctx.knowledge.add_fact(_as_said(ctx, text))
    return t("fact_saved")


@skill(
    "forget_fact",
    "Забыть факт, который пользователь раньше просил запомнить.",
    patterns=(r"^забудь\s+что\s+(?P<text>.+)$",
              r"^forget\s+that\s+(?P<text>.+)$",
              r"^忘记\s*(?P<text>.+)$"),
    params={"text": "о чём забыть"},
)
def forget_fact(ctx, text: str):
    if ctx.knowledge is None:
        raise SkillError(t("error"))
    forgot = ctx.knowledge.forget_fact(text) | ctx.knowledge.forget_contact(text)
    return t("fact_forgotten") if forgot else t("nothing_to_forget")
