# Glowbug

## What is it?

Glowbug is a robot (if you can call it that) that connects to your computer
via USB.

It helps AI coding agents get your attention by beeping, flashing lights,
changing color, and writing messages on its 5 OLED screens.

It doesn't have arms and legs yet so it won't run after you.

I built Glowbug for those times when my coding agent is *waiting for me* but
I didn't notice.

Glowbug fixes that.

When your agent is done, it'll let you know. Either subtly ...or loud and
annoyingly (you set the rules).

Physically, it's a machined aluminum bar about 25 cm long. Five small,
1-color OLED screens, ten RGB lights (5 on top, above the screens; 5 on the
bottom), and one clickable-knob (to access menu/settings).

The code on this page is the part that runs on your computer (think of it
like a driver). The code is simple, invisible (runs in the background), and
open-source. Glowbug does not have access to the internet or a mind of its
own; think of it like another peripheral like your monitor or mouse.

## Who is it for?

People who run a few AI coding agents at the same time -- Claude Code, Codex,
Cursor, Antigravity, whatever mix -- and keep losing track of which one is
waiting on them.

If you run one agent at a time, a notification probably does the job. And
it's Mac only for now.

## What does it do?

Every agent that's running gets its own screen with its name on it. The
light above the screen tells you what it's up to:

| light | meaning |
|---|---|
| dark | idle |
| violet, breathing | thinking |
| ember, with a chime | it asked you a question |
| pink pulse, with a chime | it's waiting for permission |
| green pulse, with a ding | it just finished |
| red blink | error |

A few things I cared about:

**It only watches.** Glowbug listens to events your tools already send out.
It never sits between you and a command, and it never gets a say in what an
agent is allowed to do. If a tool doesn't have a safe way to tell us
something, the light stays dark. It doesn't guess.

**No ghosts.** Quit an app and its screens go dark a few seconds later. The
daemon checks that the agent actually still exists.

**Every light has its own clock.** Agents that started thinking at different
times breathe at different times, so five agents look like five agents --
not one blob. The glow underneath is one lamp that echoes whatever's most
important, so you don't even have to look straight at it.

**More than five agents?** The row scrolls. Turn the knob. Click the knob for
the settings menu -- brightness, underglow, sound, orientation.

**The firmware is done.** Frozen. Everything the hardware can do is available
from the software on your Mac, so when Glowbug learns a new trick it's a
change on your computer, never a firmware update. And there's a tiny
bootloader in the first page of flash, so a bad update can't kill it.

What each tool can tell us:

| | thinking | question | permission | done | error | closed |
|---|---|---|---|---|---|---|
| **Claude Code** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Cursor** | ✓ | — | — | ✓ | ✓ | ✓ |
| **Codex** | ✓ | — | ✓ | ✓ | — | ✓ |
| **Antigravity** | ✓ | — | — | ✓ | ✓ | after a while |

The dashes are honest gaps -- those tools just don't have a safe event for
that moment. Cursor sessions close on the device when you archive the chat.
Antigravity shows up on its first tool call and clears out a while after it
goes quiet.

## Your own programs

The screens, lights and buzzer are yours too. One line:

```sh
glowbug show 3 --color green --line1 "Build OK" --sound ding --for 5
```

Screen 3 says "Build OK", its light goes green, the board dings -- and five
seconds later the screen goes back to showing your agent. That's the whole
idea. Paint what you want, say how long, walk away.

Same thing from Python (`import glowbug; glowbug.show(3, color="green",
seconds=5)`) or from any language over a local socket. Any pixel, any color,
any tone, every turn of the knob. The reference is [API.md](API.md), the
wire protocol is [PROTOCOL.md](PROTOCOL.md), and [examples/](examples/) has a
few scripts to start from: a CI light, a pomodoro timer, a thing that pings
you when a long command finishes.

## How do I set it up?

First you need a Glowbug: https://glowbug.dev.

Then the software. Pick whichever of these you like -- they all do the same
thing.

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

By hand, if you want to read every line first:

```sh
git clone https://github.com/pud/glowbug
cd glowbug && python3 glowbug.py install
```

The installer finds whichever coding agents you already have and hooks them
up. Install another one later and it hooks itself up. (Codex needs
`[features] hooks = true` in `~/.codex/config.toml` -- the installer will
tell you, and it won't touch that file itself.)

Then plug in the Glowbug and restart any agent sessions you already had
open. Hooks only attach to new ones. `glowbug status` tells you if it's
healthy. `glowbug doctor` tells you why not.

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

Each agent that's alive gets a screen, oldest on the left. Every time one
changes state, the daemon sends the device one line of text. The firmware
draws it. The daemon is the only thing that ever talks to the USB port --
your own programs talk to the daemon, and it passes things along behind the
agent display.

## What it can see

I'd want to know this too, so here it is. You don't have to take my word for
any of it:

**No network code.** Search the repo for `http`, `urllib`, `requests`.
Nothing. Data goes from your agents' local files and hooks to a USB port.
That's the whole trip.

**The hook forwards eight fields.** (`FORWARDER_SOURCE` in
[`glowbug.py`](glowbug.py) -- it's one screen of code.) Event name, session
id, session title, working directory, tool name, error type, which tool it
came from, and an idle flag. Never your prompts. Never tool arguments. Never
file contents.

The daemon also reads Claude Code's local session list (names, busy or idle)
and Cursor's local chat-title table (names and the archived flag, only for
sessions it already knows about). It doesn't read chat history.

The device itself only ever gets a session's name and one status word.

## If something goes wrong

`glowbug rescue` puts the known-good firmware back over USB. Works even if
the board has stopped talking -- hold the knob while you plug it in. You'll
need `brew install dfu-util`. Everything else, from a missing agent to a dark
board, is in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Requirements

- A Mac (Apple Silicon or Intel) with Python 3.9 or newer. The one that ships
  with macOS is fine.
- At least one of: Claude Code, Cursor 1.7+, Codex (with `features.hooks`
  on), Antigravity 2.0+
- A Glowbug

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```

Removes the daemon, the LaunchAgent and the hook entries. Every config it
touched gets backed up first.

## License

MIT -- see [LICENSE](LICENSE).
