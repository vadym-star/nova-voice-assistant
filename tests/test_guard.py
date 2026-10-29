import pytest

from nova import guard
from nova.skills import SkillError


@pytest.fixture
def game(monkeypatch):
    monkeypatch.setattr(guard.games, "enabled", True)
    monkeypatch.setattr(guard.games, "game", None)
    return guard.games


def test_no_game_lets_input_through(game):
    before = guard.inputs_sent
    guard.check_input()
    assert guard.inputs_sent == before + 1


def test_anticheat_game_blocks_input(game):
    game.game = "VALORANT-Win64-Shipping.exe"
    with pytest.raises(SkillError):
        guard.check_input()


def test_protection_can_be_turned_off(game):
    game.game = "BEService.exe"
    game.enabled = False
    guard.check_input()  # no error


def test_wrapped_keyboard_is_blocked(game):
    guard.install_input_guard()
    import keyboard

    game.game = "EasyAntiCheat.exe"
    with pytest.raises(SkillError):
        keyboard.send("f24")


def test_known_names_are_lower_case():
    assert all(name == name.lower() for name in guard.ANTI_CHEAT)
