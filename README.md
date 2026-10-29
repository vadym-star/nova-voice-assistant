<div align="center">

<img src="nova/web/nova.png" width="96" alt="Nova">

# Nova

**A free, open-source voice assistant for Windows.**
Hold a shortcut or just say its name, speak, and Nova does it.

[Русская версия](README.ru.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![License: MIT](https://img.shields.io/badge/License-MIT-5cf2bd)

</div>

<!-- Screenshots: overlay listening, overlay answering, settings window. -->

## What it does

Say **“Nova, open Telegram”**, **“Nova, volume 30”**, **“Nova, click Save”** or **“Nova, text Alex that I'll be ten minutes late”**. Common commands run instantly and offline. Anything else goes to **Claude** or **ChatGPT** through the subscription you already have, with no API keys. The answer appears in a small glass panel at the top of the screen.

- 🎙️ **Two ways to call it.** Hold `Ctrl+Alt+Space` (configurable), or just say the name. The name is yours to pick; the default is “Nova”.
- ⚡ **Instant commands, no AI, offline.** Open apps, sites and folders, close windows, click buttons by name, press keys, type text, scroll, set the volume, control media, take screenshots, search Google or YouTube, tell the time and date, lock or shut down the PC.
- 👁️ **Sees the screen when asked.** “Look here, what's this error?” reads the area around your mouse. “Click Accept” finds a button even where Windows can't see it, such as inside games, and remembers what it looks like and where it sits. It also maps the rest of that screen in the background, so next time it presses any of those buttons at once, without the AI.
- 💬 **Telegram messages.** It finds the chat and learns nicknames (“Alex” is “Alexandrinator” in Telegram, it asks once and remembers). It never sends to a guessed chat without asking.
- 🧩 **Your own commands.** “Remember work mode: open Telegram, then VS Code, then volume 30.” After that, “work mode” runs the whole list.
- 📚 **Learns.** When the AI has solved a request once, Nova repeats it next time instantly, without the AI.
- 🎮 **Stays out of your game.** Nova presses buttons the way a screen reader does, without moving the mouse or stealing focus where the app allows it, and puts your window back afterwards. No panel over full-screen games. While a game with anti-cheat is running, it sends no keystrokes or clicks at all.
- 🌍 **Any language in, three languages out.** Speech is recognised in any language. The interface and replies are in English, Russian or Chinese, and more can be added with a translation.
- 🔒 **Private by design.** Speech recognition runs on your PC. Only requests Nova can't handle itself go to the AI, and only if you have connected one: the text, plus a screenshot when you ask it to look.

## How it works

```
shortcut / name ─► microphone ─► Whisper (on your PC) ─┬─► your commands & learned ones ─► action
                                                        ├─► instant skills ──────────────► action
                                                        └─► Claude / ChatGPT ─► skills ──► action
                                                                                             │
                                             answer in the glass panel ◄─────────────────────┘
```

| Part | Built with |
|---|---|
| Speech recognition | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`small`, plus `tiny` for the name), [Silero VAD](https://github.com/snakers4/silero-vad) |
| Pressing buttons | Windows UI Automation, plus OpenCV template matching for buttons it can only see |
| Interface | [pywebview](https://pywebview.flowrl.com/) (WebView2) and a tray icon |
| AI | [Claude Code](https://claude.com/claude-code) or [Codex CLI](https://github.com/openai/codex), running under your own account |

## Install

**The easy way:** download the latest release (*Releases → Source code (zip)*), unpack it and double-click **`Install Nova.bat`**. It finds or installs Python, sets Nova up in its own folder (`%LOCALAPPDATA%\Programs\Nova`, no administrator rights), adds NVIDIA acceleration if you have the card, and creates Start-menu shortcuts and an entry in *Apps & features* to uninstall it. Run it again to update.

**By hand** (for developers): you need **Windows 10 (2004 or newer) or 11**, **Python 3.11 or newer**, and the [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) (already part of Windows 11).

```powershell
git clone https://github.com/vadym-star/nova.git
cd nova
pip install -r requirements.txt
python -m nova            # or double-click Nova.bat to start without a console
```

The first start needs the internet once, to download the speech models (about 550 MB) to `%APPDATA%\Nova\models`. With Python from the Microsoft Store, Windows keeps that folder under `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python…\LocalCache\Roaming\Nova`. Nova then appears in the tray. Right-click it and choose **Settings**.

Without an NVIDIA card Nova runs on the processor: it works, just slower (about 2.5 s per phrase instead of 0.2 s on a laptop Ryzen; pick the **Fast** recognition model in Settings to speed it up).

**NVIDIA graphics card (recommended):** recognition is about 10x faster.
```powershell
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*
```

**AI (optional).** In Settings → Intelligence, pick Claude or ChatGPT. Nova shows whether the app is installed and signed in, and has **Install**, **Sign in** and **Test** buttons. To do it by hand:
```powershell
# Claude (Pro or Max plan)
irm https://claude.ai/install.ps1 | iex
claude              # sign in once

# ChatGPT (Plus or Pro plan)
npm install -g @openai/codex
codex login
```

## Speed and resources

Measured on a laptop RTX 3060 with [`tools/voice_bench.py`](tools/voice_bench.py):

| | |
|---|---|
| Hearing its name | ~50 ms |
| Recognising a phrase | ~0.2 s |
| Instant command, end to end | well under 1 s |
| Claude Haiku answer through a subscription | ~3 s |
| Idle CPU | < 1 % |
| Video memory | ~530 MB |
| Memory | ~0.8 GB (speech models), ~0.1 GB (interface) |

Voice test: 84/84 commands understood, 0/40 false wake-ups on everyday phrases, clean and with background noise.

## Safety and privacy

Nova controls your PC, so it is built on one rule: **the AI never acts directly, and anything that can't be undone waits for your “yes”.** The full threat model is in [SECURITY.md](SECURITY.md). In short:

- The AI can only ask for Nova's skills, and Nova checks every step. Claude runs with all of its own tools switched off (no shell, no files, no internet); Codex runs in its read-only sandbox, so it can read files but not change anything.
- Shutting down, sending messages to a guessed chat, typing into a terminal, command-like text, and buttons that delete, pay, buy, send or publish all need a spoken “yes”, whatever the AI or a web page says.
- Text on a web page or in a chat can't quietly steer it. After Nova looks at the screen, nothing is sent or submitted without your “yes”.
- If a command arrives while your speakers are playing (a video, a stream, a voice chat saying “Nova, …”), Nova asks before doing anything.
- Password managers and Windows credential prompts are never captured.
- Screenshots are never saved. Visual memory keeps only small button crops (≤ 10 MB in total), and you can clear it in Settings.
- Data stays in `%APPDATA%\Nova`: settings, your commands, what it learned, and a log capped at 3 MB. Nothing is uploaded except AI requests (and the screenshots you asked about).

## Run as administrator (optional)

Windows doesn't let normal apps control windows that run as administrator (Task Manager, installers, some games). The shortcut works everywhere, but to press buttons or type in such windows, turn on **Settings → Behavior → Run as administrator**. Windows will then ask for permission each time Nova starts. Without it, Nova tells you when a command failed because the window runs as administrator.

## Limitations

- Windows only.
- Messages: Telegram Desktop for now.
- Always listening is in beta. It works best with a decent microphone and the name said at the start.
- The AI needs a Claude or ChatGPT subscription and the official CLI. Nova uses them as you would, under your account and their terms. Nova is not affiliated with Anthropic or OpenAI.

## Contributing

The easiest way to help is a new skill: one Python file in `nova/skills/`. See [CONTRIBUTING.md](CONTRIBUTING.md). Ideas: more messengers, Spotify, a local model through Ollama, an installer, more languages.

## License

Developed by [VadymStar](https://github.com/vadym-star). [MIT](LICENSE). Inspired by [Atticus](https://github.com/SaarthurR/atticus-mac) for macOS.
