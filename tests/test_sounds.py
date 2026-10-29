"""The real sound module (the other tests stub it out): files render and play() never raises."""

import wave

import pytest

from nova import config, sounds


@pytest.mark.parametrize("style,limit", [("windows", sounds.LIGHTER_MAX_S + 0.01), ("ticks", 0.15)])
def test_sounds_render_and_play(tmp_path, monkeypatch, style, limit):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    played = []
    monkeypatch.setattr(sounds.winsound, "PlaySound", lambda path, flags: played.append(path))
    sounds.set_style(style)
    try:
        for name in sounds._RECIPES:
            sounds.play(name)
            with wave.open(sounds._file(name)) as w:
                assert w.getnframes() / w.getframerate() <= limit  # short, as asked
    finally:
        sounds.set_style("windows")
    assert len(played) == len(sounds._RECIPES)


def test_play_never_raises(monkeypatch):
    def broken(*a):
        raise OSError("no audio device")

    monkeypatch.setattr(sounds.winsound, "PlaySound", broken)
    sounds.play("listen")  # must not raise
