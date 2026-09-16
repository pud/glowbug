# Glowbug

## What is it?

Glowbug is a robot that connects to your computer via USB.

It helps AI coding agents get your attention by beeping, flashing lights,
changing color, and writing messages on its 5 OLED screens.

It doesn't have arms and legs yet so it won't run after you.

I built Glowbug because, while I'm waiting for a coding AI (Claude, Codex,
Cursor, etc) to finish, I might flip over to YouTube or organize a desk in my
office or something -- then come back to my agent *to find out it's been
waiting 24 minutes for me to answer some yes/no question*.

Glowbug fixes that.

When your agent is done, you'll know. Saving valuable, frustrating minutes.

Physically it is a machined aluminum bar about 25 cm long, with five small
OLED screens, ten lights (five above the screens, five underneath as an
ambient glow) and one knob. This repository is the host side: the software
that runs on the Mac, a single Python file.

## Who is it for?

People who keep several AI coding agents running at once on a Mac and lose
track of which one is waiting on them. It works with
[Claude Code](https://claude.com/claude-code), [Cursor](https://cursor.com),
[Codex](https://developers.openai.com/codex) and [Antigravity](https://antigravity.google),
in any mix.

Not for you, honestly, if you run one agent at a time (a notification does
that job) or you're not on macOS (Linux and Windows hosts don't exist yet).

## What does it do?

Each live session gets a screen showing its name and status. The light above
it says the rest:

| light | meaning |
|---|---|
| dark | idle |
| violet, breathing | thinking |
| ember, with a chime | the agent asked you a question |
| pink pulse, with a chime | the agent is waiting for permission |
| green pulse, with a ding | a turn just finished |
| red blink | error |

Some details that make it pleasant to live with:

- **It only watches.** Glowbug subscribes to events your tools already emit.
  It is never in the path of a shell command or a tool call and never gets a
  vote on what an agent may do. Where a tool has no watch-only signal for a
  moment, the light stays dark rather than guess.
- **No ghosts.** Quit an app and its screens clear within seconds. The daemon
  checks that a session's process still exists.
- **Every light keeps its own time.** Agents that started thinking at
  different moments breathe out of phase, so five agents read as five things.
  The underglow is one ambient lamp that echoes whatever matters most.
- **More than five sessions** and the row scrolls; the knob moves it. Click
  the knob for the on-device settings: brightness, underglow, sound,
  orientation.
- **The device is finished.** Its firmware is frozen. Everything the hardware
  can do is reachable from the host software, so new behavior is a change on
  your Mac, never a firmware update, and a small bootloader in the first page
  of flash means a bad image cannot strand it.

What each tool can report:

| | thinking | question | permission | done | error | closed |
|---|---|---|---|---|---|---|
| **Claude Code** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Cursor** | ✓ | — | — | ✓ | ✓ | ✓ |
| **Codex** | ✓ | — | ✓ | ✓ | — | ✓ |
| **Antigravity** | ✓ | — | — | ✓ | ✓ | after a while |

The dashes are honest gaps: those tools have no watch-only event for that
moment. A Cursor session closes on the device when you **archive the chat**;
Antigravity's screen appears on its first tool call and clears a while after
it goes quiet.

## Your own programs

The screens, lights and buzzer are yours to use too:

```sh
glowbug show 3 --color green --line1 "Build OK" --sound ding --for 5
```

Screen 3 says "Build OK", its light goes green, the board dings, and five
seconds later the screen hands itself back to the agent display. The same
from Python (`import glowbug; glowbug.show(3, color="green", seconds=5)`) or
from any language over a local socket. Any pixel, any color, any tone, every
knob movement is available. The reference is [API.md](API.md), the wire
contract is [PROTOCOL.md](PROTOCOL.md), and [examples/](examples/) has scripts
to start from: a CI light, a pomodoro timer, a notifier for long commands.

## How do I set it up?

First, you need the hardware: https://glowbug.dev.

Then install the host software. Pick a door; they all do the same thing.

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

Install finds whichever coding agents you have and connects to each; install
another one later and it connects itself. Codex needs `[features] hooks =
true` in `~/.codex/config.toml`; install tells you if it's missing and never
edits that file.

Then plug in the Glowbug, and restart any agent sessions that were already
open: hooks attach to new sessions only. `glowbug status` is the health
check, `glowbug doctor` the verbose one.

## How does it work?

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
only thing that writes to the port; your own programs talk to the daemon,
and it relays for them behind the agent display.

## What it can see

You don't have to take anyone's word for it:

- **No network code.** Search the repo for `http`, `urllib`, `requests`;
  there is nothing. Data flows from your agents' local files and hooks to a
  USB port.
- **The hook forwards eight fields** (`FORWARDER_SOURCE` in
  [`glowbug.py`](glowbug.py), one screen of code): event name, session id,
  session title, working directory, tool name, error type, which tool, and an
  idle flag. Never prompt text, never tool arguments, never file contents.
- The daemon also reads Claude Code's local session registry for names and
  busy/idle, and Cursor's local chat-title table for the names and archived
  flag of sessions it already knows. It does not read chat history.
- The device only ever receives a session's **name and a status word**.

## If something goes wrong

`glowbug rescue` reflashes the known-good firmware over USB. It works even on
a device that has stopped talking: hold the knob while plugging in. Needs
`brew install dfu-util`. Everything else, from a missing agent to a dark
board, is in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Requirements

- macOS (Apple Silicon or Intel), Python 3.9+ (the system one is fine)
- At least one of: Claude Code, Cursor 1.7+, Codex (with `features.hooks`),
  Antigravity 2.0+
- A Glowbug

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```

Removes the daemon, LaunchAgent and hook entries. Every config it touched is
backed up first.

## License

MIT — see [LICENSE](LICENSE).
