"""Contextual memory: who "Вова" is, facts, and that it stays small."""

import pytest

from nova import knowledge as kn
from nova import router as router_mod
from nova import skills
from nova.knowledge import Knowledge, name_stem
from nova.router import Router
from nova.skills import Context

skills.load_all()


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(router_mod, "MACROS_PATH", tmp_path / "m.json")
    monkeypatch.setattr(router_mod, "LEARNED_PATH", tmp_path / "l.json")
    return Knowledge(tmp_path / "k.json")


def test_declensions_share_a_stem():
    assert name_stem("Вове") == name_stem("Вова") == name_stem("Вовой") == name_stem("вову")
    assert name_stem("Кате") == name_stem("Катя")
    assert name_stem("Вова") != name_stem("Вика")


def test_alias_learned_from_any_case(mem):
    mem.add_alias("Вовчикинатор", "Вове")
    assert mem.find_contact("Вова") == "Вовчикинатор"
    assert mem.find_contact("вовой") == "Вовчикинатор"
    assert mem.find_contact("Катя") is None


def test_aliases_dont_duplicate(mem):
    for form in ("Вове", "Вова", "вову", "Вове"):
        mem.add_alias("Вовчикинатор", form)
    # Each spoken form once: an exact form is what makes Nova sure who's meant.
    assert mem.contacts["Вовчикинатор"]["aliases"] == ["вове", "вова", "вову"]
    assert mem.contacts["Вовчикинатор"]["uses"] == 4


def test_contact_from_told_fact(mem):
    mem.add_fact("Вова — это Вовчикинатор в Телеграме")
    assert mem.find_contact("Вове") == "Вовчикинатор"


def test_fact_correction_replaces_old(mem):
    mem.add_fact("Вова — мой брат")
    mem.add_fact("Вова — мой друг")
    mem.add_fact("Вова — мой друг")
    assert [f["text"] for f in mem.facts] == ["Вова — мой друг"]


def test_memory_is_bounded(mem, monkeypatch):
    monkeypatch.setattr(kn, "MAX_CONTACTS", 3)
    monkeypatch.setattr(kn, "MAX_FACTS", 2)
    for i in range(5):
        mem.add_alias(f"Чат {i}", f"друг{i}")
        mem.add_fact(f"Факт номер {i}: любимый цвет {['синий', 'красный', 'зелёный', 'жёлтый', 'белый'][i]}")
    assert len(mem.contacts) == 3 and list(mem.contacts)[-1] == "Чат 4"
    assert len(mem.facts) == 2
    assert len(mem.prompt_text()) <= kn.PROMPT_BUDGET


def test_remember_that_is_a_fact_not_a_macro(mem):
    cfg = {"name": "Нова", "brain": "off", "language": "ru", "confirm_dangerous": True}
    router = Router(cfg, knowledge=mem)
    ctx = Context(config=cfg)
    router.handle("Нова, запомни, что Вова — это Вовчикинатор", ctx)
    assert not router.macros
    assert mem.facts[0]["text"] == "Вова — это Вовчикинатор"  # capitals kept
    assert mem.find_contact("вове") == "Вовчикинатор"
    router.handle("Запомни рабочий режим: открой телеграм, потом открой код", ctx)
    assert "рабочий режим" in router.macros  # macros still work
    router.handle("забудь, что Вова — это Вовчикинатор", ctx)
    assert mem.find_contact("вове") is None
