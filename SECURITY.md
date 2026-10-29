# Security

Nova listens to a microphone, reads the screen and presses keys on your PC. This page describes what could go wrong, what Nova does about it, and what is still up to you.

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's private reporting instead: *Security → Report a vulnerability* on this repository. Include the phrase you said or the page you used, what Nova did, and the relevant part of `%APPDATA%\Nova\nova.log`. You should get a reply within a week.

Only the latest release is supported.

## Threat model

| Threat | What Nova does |
|---|---|
| **Prompt injection.** Text on a web page, in a chat or in a screenshot tells the AI to do something else (“ignore your instructions and send…”). | The AI acts only through Nova's skills, and Nova checks each step. Claude Code runs with `--tools ""`, an empty MCP config, safe mode and no session persistence, so it has no tools at all. Codex runs in its `read-only` sandbox: it may read files, but can't change anything or act on the desktop. After Nova has looked at the screen, sending, submitting and saving “facts” all need a spoken yes. Shortcuts Nova learned from the AI are replayed under the same checks as the AI's own steps, so a planted shortcut can't skip them. A page can't plant “facts”, and commands that may have come from the speakers are never learned. Learned shortcuts that type, send or press keys are never replayed on a merely similar phrase. |
| **Commands run through the keyboard.** The AI types into a terminal, the Run box or the registry editor, or presses Win+R. | Typing into any terminal-like window, Win-key combinations and command-like text (`powershell`, `iex`, `certutil`, `&&`, `%VAR%` and others) always need a yes. |
| **Risky buttons.** The AI clicks “Delete”, “Pay”, “Buy”, “Send”, “Uninstall”… | Clicks on such labels need a yes. |
| **Protocol handlers.** An AI-chosen target like `ms-msdt:`, `search-ms:` or a `\\server\share` path. | “Open” only launches what is installed (Start-menu apps, Steam and Epic games, programs registered in Windows, desktop shortcuts, or an `.exe` found in install folders such as Program Files — never in Downloads or Temp, and a loose match is confirmed first), a fixed list of folders and sites, or `https://` addresses. Targets are normalised, so a scheme or path can't survive (see `tests/test_security.py`). |
| **A voice that isn't yours.** A video, a stream or a voice chat says “Nova, …” in always-listen mode. | If the speakers are playing when a command is heard (headphones don't count), Nova treats the command as untrusted: everything that types, clicks, sends or shuts down asks first. A TV in the room can't be detected this way, so dangerous actions and messages to a guessed chat always ask. |
| **Wrong recipient.** “Text Alex” goes to another Alex. | Nova reads the open chat's title back, and asks whenever the chat was a guess (Settings → Message confirmation). |
| **Sensitive screens sent to the AI.** | Screenshots happen only when you ask. Password managers (KeePass, 1Password, Bitwarden and others) and Windows credential prompts are refused. Screenshots are never written to disk. |
| **Anti-cheat bans.** Synthetic input while a protected game runs. | While Vanguard, BattlEye, EAC, FACEIT, ACE and similar games run, Nova sends no keystrokes or clicks at all (Settings → Anti-cheat protection). |
| **Settings tampering.** | The settings page can only change known settings of the right type. Everything it shows is escaped. |
| **Supply chain.** | Dependencies have tested version ranges with upper bounds. Nova never downloads or runs code at run time. The only downloads are the Whisper models from Hugging Face over HTTPS, on first start. |

## What stays on your PC

Everything lives in your Windows profile, in `%APPDATA%\Nova`:

- `config.json`: settings.
- `macros.json`, `learned.json`, `knowledge.json`: your commands, learned shortcuts, people and facts.
- `memory.db`: small crops of buttons Nova learned to find visually (≤ 3 per button, ≤ 10 MB). Clear it in Settings → Memory.
- `nova.log`: what Nova heard and did, for troubleshooting, capped at 3 × 1 MB. It contains your transcripts and message texts. Delete it whenever you like.

Audio is never recorded to disk. What leaves the PC is only what goes to your AI provider: the text of requests Nova couldn't handle itself, and the screenshot when you ask it to look. That data is covered by your provider's privacy policy.

## Things to know

- **Run as administrator** gives Nova, and through it the AI's requests, the same power over the PC as you have when elevated. The safety checks above still apply, but only turn it on if you need to control administrator windows. Python packages installed for your user then run elevated, so keep that environment clean.
- **Always listening** keeps the microphone open. Speech is checked on your PC, and only a phrase that starts with the name gets past the first ~50 ms model. Turn it off in Settings if you prefer the shortcut.
- Nova runs `claude` / `codex` from your PATH. Install them from the official sources only.
