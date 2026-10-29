# Contributing to Nova

Thanks for stopping by! Every kind of help counts: bug reports, ideas, new skills, translations, design. Issues and pull requests are welcome in English or Russian.

## Developer setup

```powershell
git clone https://github.com/vadym-star/nova.git
cd nova
pip install -r requirements.txt pytest
python -m pytest tests          # fast: routing, safety, wake word; no microphone or models
python tools/voice_bench.py     # slow (~5 min): Windows voices -> Whisper -> router, plus timings
python -m nova                  # run it
```

Log: `%APPDATA%\Nova\nova.log`. Settings: `%APPDATA%\Nova\config.json`.

## Project layout

```
nova/
  app.py        main loop: shortcut / name -> recording -> recognition -> router -> panel
  router.py     what to do with a phrase: your commands -> learned -> skills -> AI; safety checks
  brain.py      Claude Code / Codex through the user's subscription; the AI answers with skills as JSON
  guard.py      anti-cheat games, administrator windows, private windows, speakers check
  hotkey.py     the push-to-talk shortcut and Esc via Windows RegisterHotKey (no keyboard hook)
  stt.py        speech recognition (faster-whisper) and the name gate
  text.py       name matching, normalisation
  audio.py      microphone
  screen.py     screenshots, windows, focus
  vision.py     visual memory of buttons
  knowledge.py  people and facts
  i18n.py       messages in every language
  ui.py, web/   panel, settings, tray
  skills/       <- skills: the easiest place to start
tools/          icon generator, voice benchmark
tests/
```

## Adding a skill

A skill is a function with the `@skill` decorator in any `nova/skills/*.py` file. Nova picks it up by itself, both for instant commands and for the AI.

```python
# nova/skills/weather.py
import webbrowser
import urllib.parse

from . import SkillError, skill


@skill(
    "weather",                                       # unique name
    "Show the weather in a city.",                   # description for the AI
    patterns=(r"^(?:what's the )?weather(?: in (?P<city>.+))?$",
              r"^(?:какая\s+)?погода(?:\s+в\s+(?P<city>.+))?$"),   # instant path
    params={"city": "city name, empty for the current one"},     # parameters for the AI
)
def weather(ctx, city: str = ""):
    if city == "mars":
        raise SkillError("I don't fly there yet")   # shown to the user
    webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(f"weather {city}"))
    return None                                     # a string is shown in the panel
```

Rules:
- **Patterns** match normalised text: lower case, no punctuation or filler words (“please”, “ну”). Named groups `(?P<x>...)` become arguments.
- Return a short reply, or `None` when the result is visible anyway. Add user-facing text to `nova/i18n.py` in every language.
- Anything irreversible (deleting, sending, shutting down) gets `dangerous=True`: Nova asks for a spoken “yes” first.
- Broad patterns like “open …” get `priority=-1` so they don't catch more specific ones.
- Work in the background when you can (UI Automation patterns instead of keystrokes); set `background=True` if the skill has to bring a window up, and Nova will put the user's window back.
- Send keys and clicks only through `keyboard.send` / `keyboard.write` / `uiautomation`: Nova's anti-cheat guard watches those.
- Add a few phrases to `tests/test_router.py`.

## Safety rules for changes

Nova acts on a real PC, so a few things are not negotiable (see [SECURITY.md](SECURITY.md)):

- The AI never gets direct access to the shell, files or the network. New capabilities are skills, and skills go through `router._needs_user_ok`.
- Don't launch arbitrary strings: no `os.startfile`, `subprocess` or `webbrowser.open` on text that came from the AI or the screen without an allowlist.
- Tests must never touch the real desktop. `tests/conftest.py` blocks keys, clicks and Telegram. Keep it that way, and test with Telegram's *Saved Messages* if you test by hand.
- Never commit Windows system sounds, models or anything you don't have the right to redistribute.

## Pull requests

1. Fork, branch, change, `python -m pytest tests`.
2. In the description: what it does and how to check it by voice.
3. Small PRs get reviewed faster.

## Bugs and ideas

Open an issue from a template. For a bug, include the phrase you said, what you expected, and the matching part of `nova.log`. Security problems go through private reporting, see [SECURITY.md](SECURITY.md).
