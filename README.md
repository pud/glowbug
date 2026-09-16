# Glowbug

A machined aluminum bar that shows your coding-agent sessions. Five small
screens, ten lights, one knob. It sits on the desk and tells you, from across
the room, which agent is thinking, which one needs you, and which one just
finished.

Works with [Claude Code](https://claude.com/claude-code), [Cursor](https://cursor.com),
[Codex](https://developers.openai.com/codex) and [Antigravity](https://antigravity.google),
in any mix. Each session gets a screen. More than five sessions and the row
scrolls; the knob moves it.

| light | meaning |
|---|---|
| dark | idle |
| violet, breathing | thinking |
| ember, with a chime | the agent asked you a question |
| pink pulse, with a chime | the agent is waiting for permission |
| green pulse, with a ding | a turn just finished |
| red blink | error |

## Why it is the way it is

- **It only watches.** Glowbug subscribes to the events your tools already
  emit. It is never in the path of a shell command or a tool call and never
  gets a vote on what an agent may do. If a tool has no watch-only signal for
  a moment, the light stays dark rather than guess.
- **No ghosts.** Quit an app and its screens clear within seconds. The daemon
  checks that a session's process still exists; it will not show an agent that
  isn't there.
- **Every light keeps its own time.** Agents that started thinking at
  different moments breathe out of phase, so five agents read as five things,
  not one blob. The underglow is one ambient lamp that echoes whatever matters
  most, so you don't have to look directly at it.
- **The device is finished.** Its firmware is frozen: everything the hardware
  can do — any pixel on any screen, any color on any light, any tone, every
  knob movement — is reachable through the host software, so new behavior is a
  change on your Mac, never a firmware update. A small bootloader in the first
  page of flash means a bad image cannot strand it.
- **Small enough to read.** The host is one Python file, standard library
  only, no network code. You can read every line before trusting it.

## Install

Pick a door; they all do the same thing.

Tell Claude Code:

```text
Install glowbug from github.com/pud/glowbug
```

Homebrew:

```sh
brew install pud-blip/tap/glowbug
glowbug install
```

pipx or uv:

```sh
pipx install glowbug && glowbug install
uvx glowbug install
```

By hand, the fully auditable path:

```sh
git clone https://github.com/pud/glowbug
cd glowbug && python3 glowbug.py install
```

Then plug in the Glowbug. New sessions appear on it. Install finds whichever
coding agents you have and connects to each; install another one later and
it connects itself. Hooks attach to new sessions only, so restart any that
are already open. `glowbug status` is the health check, `glowbug doctor` the
verbose one.

## Your own programs

The screens, lights and buzzer are yours to use too:

```sh
glowbug show 3 --color green --line1 "Build OK" --sound ding --for 5
```

Screen 3 says "Build OK", its light goes green, the board dings, and five
seconds later the screen hands itself back to the agent display. Same thing
from Python (`import glowbug; glowbug.show(3, color="green", seconds=5)`) or
from any language over a local socket. The full reference is [API.md](API.md),
the wire contract is [PROTOCOL.md](PROTOCOL.md), and [examples/](examples/)
has scripts to start from: a CI light, a pomodoro timer, a notifier for long
commands.

## Which agents, and what you'll see

| | thinking | question | permission | done | error | closed |
|---|---|---|---|---|---|---|
| **Claude Code** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Cursor** | ✓ | — | — | ✓ | ✓ | ✓ |
| **Codex** | ✓ | — | ✓ | ✓ | — | ✓ |
| **Antigravity** | ✓ | — | — | ✓ | ✓ | after a while |

The dashes are honest gaps: those tools have no watch-only event for that
moment. Two specifics: a Cursor session closes on the device when you
**archive the chat** (Cursor has no end event, but the archive flag is
visible), and Antigravity's screen appears on its first tool call and clears
a while after it goes quiet. Codex needs `[features] hooks = true` in
`~/.codex/config.toml`; install tells you if it's missing and never edits
that file.

## If it ever seems dead

`glowbug rescue` reflashes the known-good firmware over USB. It works even on
a device that has stopped talking: hold the knob while plugging in. Needs
`brew install dfu-util`. Details in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Privacy

You don't have to take anyone's word for it:

- **No network code.** Search the repo for `http`, `urllib`, `requests`;
  there is nothing. Data flows from your agents' local files and hooks to a
  USB port.
- **The hook forwards eight fields** (`FORWARDER_SOURCE` in
  [`glowbug.py`](glowbug.py), one screen of code): event name, session id,
  session title, working directory, tool name, error type, which tool, and an
  idle flag. Never prompt text, never tool arguments, never file contents.
- The daemon also reads Claude Code's local session registry for names and
  busy/idle, and Cursor's local chat-title table for names and the archived
  flag of sessions it already knows. It does not read chat history.
- The device only ever receives a session's **name and a status word**.

## How it works

```
Claude Code ──┐
Cursor ───────┤ hooks ──▶ glowbug-hook ──unix socket──▶ glowbug.py (daemon)
Codex ────────┤                                             │
Antigravity ──┘                                             │
Claude Code session registry ───────────────────────────────▶│
Cursor chat titles (local) ─────────────────────────────────▶│
                                                       USB serial, one
                                                       text line at a time
                                                                ▼
                                                            Glowbug
```

The daemon gives each live session a screen, oldest on the left, and sends
the device one line per state change. The firmware draws. The daemon is the
only thing that writes to the port; your own programs talk to the daemon.

## Requirements

- macOS (Apple Silicon or Intel), Python 3.9+ (the system one is fine)
- At least one of: Claude Code, Cursor 1.7+, Codex (with `features.hooks`),
  Antigravity 2.0+
- A Glowbug (https://glowbug.dev)

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```

Removes the daemon, LaunchAgent and hook entries. Every config it touched is
backed up first.

## License

MIT — see [LICENSE](LICENSE).
