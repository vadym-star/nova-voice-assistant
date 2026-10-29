"""Tests must never touch the real desktop: no keys, clicks, focus changes or Telegram."""

import pytest


class _Blocked(Exception):
    pass


def _blocked(*a, **kw):
    raise _Blocked("a test tried to touch the real desktop")


@pytest.fixture(autouse=True)
def own_data_folder(monkeypatch, tmp_path):
    """Every test gets an empty data folder: the user's settings, memory and contacts
    in %APPDATA%\\Nova are never read or changed by a test run."""
    from nova import config, knowledge, router, vision
    from nova.skills import apps

    data = tmp_path / "NovaData"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "CONFIG_PATH", data / "config.json")
    monkeypatch.setattr(router, "MACROS_PATH", data / "macros.json")
    monkeypatch.setattr(router, "LEARNED_PATH", data / "learned.json")
    monkeypatch.setattr(knowledge, "PATH", data / "knowledge.json")
    monkeypatch.setattr(knowledge, "OLD_CONTACTS_PATH", data / "contacts.json")
    monkeypatch.setattr(vision, "DB_PATH", data / "memory.db")
    monkeypatch.setattr(apps, "_FOUND_PATH", data / "found_apps.json")


@pytest.fixture(autouse=True)
def no_desktop(monkeypatch):
    import keyboard

    from nova import screen
    from nova.skills import messaging

    monkeypatch.setattr(keyboard, "write", lambda *a, **kw: None)
    monkeypatch.setattr(keyboard, "send", lambda *a, **kw: None)
    monkeypatch.setattr(screen, "bring_to_front", lambda hwnd: True)
    monkeypatch.setattr(screen, "click_at", _blocked)
    monkeypatch.setattr(screen, "still_there", lambda hwnd, shot: True)  # fake windows in tests
    monkeypatch.setattr(messaging, "_open_telegram", _blocked)
    monkeypatch.setattr("nova.router._restore_focus", lambda hwnd: None)
