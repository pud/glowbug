# Glowbug

A machined aluminum bar that shows your coding-agent sessions — five OLED
screens and ten RGB LEDs that tell you, from across the room, which agent is
thinking, which one needs you, and which one just finished.

Works with [Claude Code](https://claude.com/claude-code), [Cursor](https://cursor.com),
[Codex](https://developers.openai.com/codex), and [Antigravity](https://antigravity.google)
— mix and match, one screen each.

- **dark** — session idle
- **magenta-violet breathe** — thinking (with a little star-spinner on its screen)
- **ember-orange fade + chime** — the agent asked you a question
- **pink pulse + chime** — the agent is waiting for permission to use a tool
- **green pulse + ding** — an agent just finished its turn
- **red blink** — error

Automated subagents (`claude -p` one-shots, background verification runs)
are deliberately **not shown** — they aren't sessions you control, so a
screen for them is just noise.

Every pulse runs on its own clock — agents that start thinking at different
moments breathe out of phase, so each agent reads as its own independent
light rather than one synchronized blob.

The underglow acts as one ambient lamp echoing the most important thing
happening on the board, so you don't even need to look directly at it.

This repository is the **host software**: everything that runs on your Mac.
It's deliberately tiny — **one Python file, standard library only, zero
network code** — so you can read every line before trusting it.

## Install

Pick a door (they all do the same thing):

**Tell Claude Code** (easiest — you already have it):

```text
Install glowbug from github.com/pud/glowbug
```

**One-liner:**

```sh
curl -fsSL https://glowbug.dev/install | sh
```

**Homebrew:**

```sh
brew install pud-blip/tap/glowbug
glowbug install
```

**pipx:**

```sh
pipx install glowbug && glowbug install
```

**uv:**

```sh
uvx glowbug install
```

**By hand** (the fully-auditable path):

```sh
git clone https://github.com/pud/glowbug
cd glowbug && python3 glowbug.py install
```

Then plug in your Glowbug. New sessions appear on the device.
(`glowbug status` for a health check, `glowbug doctor` when something's not
showing up; `glowbug.py uninstall` removes everything, including the hook
entries, with a backup of every config it touched.)

Install finds whichever coding agents you already have and connects to each.
**Install a new one later and it connects itself** — the daemon checks every
few minutes. Hooks only ever attach to *new* sessions, so restart any that
are already open.

## API

Your own programs can use the screens, LEDs and buzzer too. One line:

```sh
glowbug show 3 --color green --line1 "Build OK" --sound ding --for 5
```

Screen 3 says "Build OK", the LED under it goes green, the board dings, and
five seconds later the screen hands itself back to the agent display. That's
the whole idea: paint what you want, say how long, walk away.

### The CLI

Screens and their status LEDs are **1-5, left to right**; the underglow LEDs
are **ug1-ug5**; the groups `all`, `glass` (the five status LEDs) and `ug`
(the five underglow LEDs) name a set. Lists work everywhere: `1,3`, `2,ug4`.
Every command below takes `--json` (print the daemon's reply) and `--help`.

**`show SCREEN`** — the one-stop call. A color goes on the screen's own LED,
text on the glass, a sound on the buzzer; give any mix.

```sh
glowbug show 1 --color amber --mode pulse --line1 "Deploying" --line2 "prod-eu" --for 120
glowbug show all --big "LUNCH" --color sky --sound coin --for 10
```

Flags: `--color`/`-c`, `--mode solid|fade|pulse|blink|off`, `--to COLOR`
(the pulse/blink partner color; pulse defaults to 30 % of the color, blink
to off), `--period MS` (pulse/blink period, or the fade time with
`--mode fade`; 16..65535), `--line1`, `--line2`, `--big` (one 24 px centered
line instead of line1/line2), `--sound`/`-s`, `--volume 0-4`, `--no-led`
(text only, leave the LED alone), `--raw` (send the color as-is instead of
scaled by the board's brightness setting), `--for SECONDS` (0.05..86400).

**`led SEL COLOR`** — any LED, any mode.

```sh
glowbug led ug amber --mode pulse --for 300
glowbug led 1,3,ug5 "#0f0" --mode fade --fade-ms 250
glowbug led all off
```

Same mode flags as `show`, plus `--fade-ms MS` (default 700).

**`text SCREEN LINE1 [LINE2]`** — words on a screen. 21 characters per line
(longer is clipped and the reply says `truncated`); printable ASCII only,
anything else shows as `?`; empty text clears the screen.

```sh
glowbug text 2 "Tests" "42 passed, 0 failed" --for 60
glowbug text 4 "SHIPPED" --big --for 30
```

**`sound SOUND`** — a name, or notes as `hz:ms,hz:ms,...` (hz 0 = a rest;
up to 32 notes and 5 s). `off` stops whatever is playing. Sounds need no
ownership and outrank the board's own chimes.

```sh
glowbug sound fanfare
glowbug sound 440:200,0:50,880:200 --volume 2
```

**`own RES...`** / **`release [RES...]`** — hold things without painting yet:
`led:<sel>`, `glass:<sel>`, `sound` (mutes the board's own chimes), `enc`
(the knob stops scrolling and opening the menu; its turns arrive as events
instead), `all`. `release` with nothing hands back everything.

```sh
glowbug own glass:3 led:3 --for 600
glowbug release glass:3 enc
```

**`events`** — the board, live, one JSON object per line. Kinds: `enc`
`click` `hold` `menu` `own` `err` `board` `hello` `set` `info` `redraw`
`evt` (anything the daemon doesn't recognise, raw). Ctrl-C ends it.

```sh
glowbug events --filter enc,click,hold
{"event": "enc", "delta": 2}
{"event": "click"}
```

**`info`** — daemon version, socket path, the board's firmware / PROTO /
capabilities, what is currently owned, the board's settings.

**`palette`** — every color and sound name the daemon knows; `--reload`
re-reads your own files (next section).

**`settings get|set|save [KEY] [VALUE]`** — the board's user settings.
`set` changes RAM and takes effect now; `save` writes them to flash (the
board allows one save per 10 s). Keys: `brightness` 0-100, `ug_brightness`
0-100, `ug_mode` 0|1|2 (off / status echo / warm lamp), `volume` 0-4,
`chime` 0-2 (fanfare / ding / soft), `flip` 0|1 (`on`/`off` also work).

```sh
glowbug settings get brightness
glowbug settings set volume 2 && glowbug settings save
```

**`raw LINE...`** — wire lines straight through, for anything the commands
above don't cover (see [PROTOCOL.md](PROTOCOL.md); indices there are
0-based). `--confirm` waits until the board has parsed them and fails with
its `ERR` lines; `-` reads lines from stdin. `DFU` and `REPLY` are never
relayed.

```sh
glowbug raw "LED 7 SET FF00FF" --confirm
```

Exit codes: `0` ok · `1` the daemon refused (the reason on stderr, the
`error` code in `--json`) · `2` usage · `3` no daemon.

### Colors and sounds

A color is a name, `#RGB`, `#RRGGBB` or `RRGGBB`. Built-in names: `off
white red green blue yellow orange violet cyan magenta`, the board's own
tints `thinking question permission error done unread subagent lamp`, and
`warm pink ember amber gold lime mint teal sky indigo purple rose coral
cool grey gray dim`. Colors are scaled by the board's brightness setting
before they're sent, unless you pass `--raw`.

A sound is a name — `fanfare ding soft blip hello bye boot` (the board's own
chimes) plus `tick flutter snap beep double rise fall warn fail alarm coin
knock sos` — or notes as `hz:ms,hz:ms,...`.

Your own names go in two files that Glowbug reads but never creates.
`~/.glowbug/palette.json` maps a name to `RRGGBB`, `#RRGGBB`, `#RGB` or
another name:

```json
{"ok": "00C853", "brand": "#ff6a00", "warn": "amber"}
```

`~/.glowbug/sounds.json` maps a name to `[[hz, ms], ...]`, `"hz:ms,hz:ms"`
or another name:

```json
{"ship": [[1568, 60], [2093, 60], [3136, 200]], "nudge": "2700:40,0:40,2700:40"}
```

Names are letters, digits, `_` and `-`, start with a letter, at most 24
characters, case-insensitive. **Yours win** over the built-ins (you can
redefine `green`). A bad entry is skipped and reported by `glowbug palette`
as a warning; the rest still load. The files are read when the daemon
starts and on `glowbug palette --reload`. Names are resolved on your Mac —
the board only ever sees hex and notes — so `glowbug palette --json` is how
a program discovers what exists.

### From Python

```python
import glowbug

glowbug.show(3, color="green", line1="Build OK", sound="ding", seconds=5)
glowbug.led("ug", "amber", mode="pulse", seconds=300)
glowbug.text(5, "24:59", "focus", seconds=5)
glowbug.sound("fanfare")
for ev in glowbug.events(["enc", "click"]):
    print(ev)
```

Every helper is one round trip to the daemon and returns the reply dict; a
refusal raises `glowbug.GlowbugError` with `.code` (`no_daemon`, `no_board`,
`bad_arg`, …) and `.message`. The full set: `show led text sound raw own
release events info palette settings_get settings_set settings_save`;
`seconds=` is the CLI's `--for`. Signatures are in the docstrings
(`python3 -c "import glowbug; help(glowbug.show)"`).

Getting the module: `pip install glowbug` or `uv add glowbug` in your
project's venv. (`pipx install glowbug` gives you the CLI only — pipx keeps
its venv private, so `import glowbug` won't resolve from your project.)
Zero-install alternative: `glowbug install` copies the file to
`~/.glowbug/glowbug.py`, so this always works:

```python
import os, sys
sys.path.insert(0, os.path.expanduser("~/.glowbug"))
import glowbug
```

### From any language

The daemon listens on a unix socket, `~/Library/Application
Support/Glowbug/daemon.sock` (mode 0600, same user only; `$GLOWBUG_SOCK`
overrides the path). Send one JSON object and a newline, read one JSON
line back:

```sh
printf '{"cmd":"show","screen":3,"color":"green","line1":"Build OK","sound":"ding","for":5}\n' \
  | nc -U "$HOME/Library/Application Support/Glowbug/daemon.sock"
{"ok": true, "api": 2, "lines": ["OWN GLASS 2 FOR 5000", "OWN LED 2 FOR 5000", "LED 2 SET 00FF00", "TEXT 2 Build OK", "TONE 2637:70,0:20,3951:260"], "held_until": 1757980805.2, "truncated": false}
```

Request fields are the CLI's flags with the dashes dropped (`for`, `line1`,
`no_led`, `fade_ms`, …). A refusal is `{"ok": false, "api": 2, "error":
"<code>", "message": "…"}` with `error` one of `bad_request unknown_cmd
bad_arg no_board proto_too_old busy board_err timeout`. `lines` are the
exact wire lines the daemon queued for the board — "queued" is the promise,
not "drawn"; `raw` with `"confirm": true` and `settings` are the calls that
wait for the board. `{"cmd":"events","filter":["enc"]}` answers one header
line and then streams.

### Ownership, in three sentences

Whatever you paint, the daemon claims for you first (the `OWN` lines in the
reply) and the agent display leaves it alone until it's released — `--for`
releases it on the board's own timer, so a crashed script never leaves a
stale light. Ownership is one pool shared by every program on this Mac, so
the last writer wins, and it ends without you when the daemon goes quiet
for 15 s (Mac asleep, daemon restarting or upgrading), when the board is
re-plugged, or when anything calls `release`; the on-device menu (a knob
hold) borrows all five screens for up to 8 s and the daemon repaints yours
when it closes. `glowbug events` reports every one of these (`own`, `menu`,
`board`, `redraw`), and `glowbug info` shows what is owned right now.

Full wire contract: [PROTOCOL.md](PROTOCOL.md). Runnable scripts:
[examples/](examples/).

## Which agents, and what you'll see

| | thinking | question | permission | done | error | closed |
|---|---|---|---|---|---|---|
| **Claude Code** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Cursor** | ✓ | — | — | ✓ | ✓ | ✓ |
| **Codex** | ✓ | — | ✓ | ✓ | — | ✓ |
| **Antigravity** | ✓ | — | — | ✓ | ✓ | after a while |

The dashes are honest gaps, not bugs: those tools don't expose an
*observational* event for that moment. Glowbug only ever subscribes to events
it can watch without being able to interfere — it will never sit in the path
of a shell command or a tool call, and never gets a vote on whether your agent
is allowed to do something. Two consequences worth knowing:

- Cursor has no watch-only signal for its approval dialog, so no pink light
  there. And a freshly-opened chat pane doesn't get a screen until the agent
  first does something — Cursor announces empty panes as sessions, and
  Glowbug won't show a screen for a conversation that doesn't exist yet.
  To *close* a Cursor session on the device, **archive the chat** in Cursor
  (Cursor has no close/end event, but the archive flag is visible) — the
  screen plays its red farewell and frees up within a couple of seconds.
- Antigravity has no session-start or session-end event, so its screen appears
  on the first tool call and clears a while after the session goes quiet.

One thing that is never a gap: **ghost agents.** Quit (or force-quit) any of
these apps and their screens clear within seconds — the daemon checks that a
session's app is still running, so Glowbug never shows an agent that no
longer exists.

**Codex needs one setting** turned on for hooks to fire — add to `~/.codex/config.toml`:

```toml
[features]
hooks = true
```

Glowbug prints this during install if it's missing. It doesn't edit that file
— it's yours.

## If it ever seems dead

`glowbug rescue` reflashes a known-good firmware image over USB — it works
even if a bad update left the device unable to talk (hold the knob while
plugging in → the screen shows RESCUE MODE). Full walkthrough in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). Needs `brew install dfu-util`.

## Privacy — what Glowbug can and cannot see

The point of open-sourcing this is that you don't have to take our word:

- **No network code.** Search the repo for `http`, `urllib`, `requests` —
  there's nothing. Data flows from your agents' local files/hooks to a USB
  serial port. That's the entire graph.
- **The hook forwards eight fields** and nothing else — search
  [`glowbug.py`](glowbug.py) for `FORWARDER_SOURCE`, it's one screen of code:
  `hook_event_name`, `session_id`, `session_title`, `cwd`, `tool_name`,
  `error_type`, plus `source` (which tool it came from) and `idle` (a
  true/false). **Never prompt text, never tool arguments, never file
  contents.**
- **Things the tools offer that we deliberately drop:** transcript paths,
  model names, your email, turn ids, free-text error messages, file diffs,
  shell commands. The whitelist is in the forwarder as an `ALIASES` table —
  anything not named there never leaves that process.
- The daemon also reads `~/.claude/sessions/*.json` (Claude Code's local
  session registry) for session names and busy/idle status. Cursor hooks
  never include a chat title, so the daemon looks up **names and the
  archived flag only** from
  Cursor's local `composerHeaders` table (`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`)
  for sessions it already learned about from hooks — it does not scan your
  chat history. Codex and Antigravity have no such store, so for them the
  hooks are all Glowbug knows.
- The device itself only ever receives a session's **name and a status word**.

## How it works

```
Claude Code ──┐
Cursor ───────┤ hooks ──▶ glowbug-hook ──unix socket──▶ glowbug.py (daemon)
Codex ────────┤                                             │
Antigravity ──┘                                             │
Claude Code session registry (~/.claude/sessions) ──────────▶│
Cursor chat titles + archived flag (local composerHeaders) ─▶│
                                                       USB serial (newline
                                                         text protocol)
                                                                ▼
                                                            Glowbug 🐛✨
```

The daemon gives each live session a screen (oldest on the left, newest on the
right), merges hook events with Claude Code's session registry (and Cursor
chat titles from its local DB), and streams
semantic states over a simple text protocol:

```
SLOT 3 STATE question NAME my-project DETAIL Bash SUB 0 SID 8aada7ae
```

The device firmware owns all rendering — colors, animations, chimes, the
on-device settings menu (brightness, underglow, sound, chime choice).

The daemon is the only thing that ever writes to the USB port; your own
programs (the [API](#api) above) talk to the daemon over its socket, and it
relays for them behind the agent display.

## Requirements

- macOS (Apple Silicon or Intel), Python 3.9+ (the system one is fine)
- At least one of: Claude Code, Cursor 1.7+, Codex (with `features.hooks`),
  Antigravity 2.0+
- A Glowbug device (hardware docs coming later — glowbug.dev)

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```

Removes the daemon, LaunchAgent, and hook entries. Your
`~/.claude/settings.json` is backed up before every change.

## License

MIT — see [LICENSE](LICENSE). https://glowbug.dev
