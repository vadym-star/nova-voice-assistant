# Changelog

## 0.1.0 — first public release

**Listening**
- Push-to-talk shortcut (hold or toggle) and always-listen mode with a name of your choice.
- Offline speech recognition in any language (faster-whisper `small`, int8). A `tiny` model checks for the name in ~50 ms, so chatter never reaches the main model.
- Interface and replies in English, Russian and Chinese. The default name follows the language.

**Doing**
- Instant skills without the AI: apps, sites and folders, windows, buttons by name, keys, typing, scrolling, volume, media, screenshots, search, time and date, lock and power.
- Screen understanding on request (“look here”), visual clicks that are remembered with their place in the window (found even when a button glows or pulses), background mapping of app screens, and “wrong one” to correct them.
- Finds what's installed anywhere: Start menu, Steam and Epic games, programs registered in Windows, desktop shortcuts, and install folders on every disk; any grammatical case (“доту” → Dota 2). When a step fails, the AI tries another way.
- Messages in any messenger: Telegram with automatic chat search and nickname memory, other apps step by step through their window; always a confirmation when the chat is a guess.
- Your own voice macros. Learned shortcuts for requests the AI solved once.
- Claude (Claude Code) or ChatGPT (Codex CLI) through your subscription, with install, sign-in and test buttons in Settings.
- Background actions that put your window back. No panel over full-screen games.

**Safety**
- The AI has no tools of its own; risky steps need a spoken “yes” (terminals, command-like text, Win-key combos, delete/pay/send buttons, screen-steered sending).
- Learned shortcuts replay under the same checks as the AI.
- Commands heard while the speakers play are treated as untrusted.
- No keystrokes or clicks while an anti-cheat game runs.
- Password managers and credential prompts are never captured.
- Optional run-as-administrator mode, and a clear message when an administrator window blocks a command.
- The shortcut is a Windows hotkey, not a keyboard hook: no key of the user's is ever delayed (Ctrl/Alt in games), and it works over administrator windows.
- Protected settings input.

**Install**
- `Install Nova.bat`: installs for the current user without administrator rights (own Python environment, NVIDIA acceleration when available, Start-menu shortcuts, optional desktop shortcut and start with Windows, an *Apps & features* entry). Run again to update; *Uninstall Nova* asks before deleting settings and models.

**Interface**
- Liquid-glass panel at the top or bottom, full or orb-only. Settings save instantly.
- Quiet sound cues; “cancel”, Esc or ✕ stops anything; hides after silence.
