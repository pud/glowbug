# Glowbug API

Your own programs can use the screens, LEDs and buzzer. One line:

```sh
glowbug show 3 --color green --line1 "Build OK" --sound ding --for 5
```

Screen 3 says "Build OK", the LED under it goes green, the board dings, and
five seconds later the screen hands itself back to the agent display. That's
the whole idea: paint what you want, say how long, walk away.

Everything here talks to the daemon over a local socket; the daemon is the
only thing that writes to the USB port. Names for colors and sounds are
resolved on your Mac — the board only ever sees hex and notes — so the
vocabulary can grow without a firmware change. The wire contract underneath
is [PROTOCOL.md](PROTOCOL.md); runnable scripts are in [examples/](examples/).

## The CLI

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

**`lightshow`** — the five-second show `glowbug install` plays when it first
sees the board: a wave of light through all five screens, flowing plasma
under a rainbow, a starfield warp, a white flash, and the name. Host-rendered
frames over the raw API, so it costs the firmware nothing. `--quiet` drops
the soundtrack. Everything it claims is released at the end.

```sh
glowbug lightshow
```

## Colors and sounds

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
starts and on `glowbug palette --reload`. `glowbug palette --json` is how a
program discovers what exists.

## From Python

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

## From any language

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

## Ownership, in three sentences

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
