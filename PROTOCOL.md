# Glowbug wire protocol — PROTO 4 (frozen)

**Normative.** Firmware 2.0.0 implements exactly this and is never updated in the field.
Anything a Glowbug can do is reachable through the primitives below; everything else
(names, themes, behaviors) lives in the host. Daemon 2.0.0 / socket API 2 sit on top
(see "Host socket API" at the end).

The board is a USB CDC serial device (`/dev/cu.usbmodem*` on macOS, product string
`Glowbug`). Baud rate is ignored. The daemon owns the port; third-party programs talk to
the daemon (last section). This document describes the wire itself, for the daemon and
for anyone driving a board directly with the daemon stopped.

## Framing

- ASCII lines terminated by `\n` or `\r`. Empty lines are ignored.
- A line is at most 1023 characters plus the terminator (`INFO LINE 1024`). A longer line is
  discarded up to its terminator and answered with one `ERR LINE toolong`.
- The first token is the verb. PROTO 4 verbs are matched **exactly** and are upper case.
  Legacy verbs keep their historical matching (see "Legacy verbs").
- Unknown verb: `ERR <verb> unknown` (the verb is echoed with at most 16 printable characters).
- Bad arguments: `ERR <verb> <why>`. **No command ever has a partial effect**: it is fully
  applied or fully refused.
- `OK <verb>` is sent only while `REPLY ON` is active (default OFF; reset by `HELLO`, by USB
  re-enumeration and by the host dropping DTR). Verbs whose answer is itself the
  acknowledgement never send `OK`: `ECHO`, a bare `OWN` query, and `REPLY OFF`. `ERR …` and
  `EVT …` lines are always sent.
- Replies are only sent while the host asserts DTR, and a reply that does not fit the 256 B
  USB transmit buffer is dropped whole (`INFO TXDROP` counts them). **Never use `OK` for
  flow control; use `ECHO`.**

## Verbs

| Verb | Arguments / limits | Effect |
|---|---|---|
| `HELLO` | — | Answers `EVT HELLO <fw> PROTO 4 SLOTS 32`. Also a **session reset**: releases everything, `REPLY OFF`. |
| `INFO` | — | One line of key/value pairs: `EVT INFO FW 2.0.0 PROTO 4 HW 5 LEDS 10 GLASS 5 UG 5 SCREENS 5 W 128 H 32 PAGES 4 FONTS 7,16,24 NOTES 32 TONEMAX 5000 LINE 1024 SLOTS 32 STACK <min free bytes> HEAP <bytes> TXDROP <n> UP <seconds> FAULT <hex pc> LR <hex lr> STRIKES <n> CSR <hex>`. `FAULT`/`LR` are the last HardFault's stacked PC/LR (0 = none since power-up), `STRIKES` the bootloader's consecutive-crash count, `CSR` the reset flags the bootloader saw. Parse as pairs; ignore keys you do not know. |
| `ECHO <token>` | ≤16 printable chars | Answers `EVT ECHO <token>`. An ordered barrier: every line sent before it has been parsed when the echo returns. |
| `REPLY ON` / `REPLY OFF` | — | Ack mode. `OK REPLY` only after `ON`. |
| `OWN <res> [FOR <ms>]` | see Ownership | Claim; `FOR` 1..86400000 auto-releases on the board. Bare `OWN` = query. Always answered with `EVT OWN …`. |
| `RELEASE <res>` / `RELEASE ALL` | a bare `RELEASE` means `ALL` | Release now. |
| `LED <sel> SET <color>` | | Solid color. |
| `LED <sel> FADE <color> <ms>` | ms 16..65535 | Eased fade from the LED's current color. |
| `LED <sel> PULSE <c1> <c2> <period_ms>` | 16..65535 | Eased breathe between two colors. |
| `LED <sel> BLINK <c1> <c2> <period_ms>` | 16..65535 | Hard alternate, half period each. |
| `LED <sel> OFF` | | = `SET 000000`. |
| `SOUND <name> [VOL 0-4]` | names: `blip ding soft fanfare hello bye boot` | Plays a built-in melody (≤ 2 s). |
| `TONE <hz>:<ms>[,<hz>:<ms>…] [VOL 0-4]` | ≤32 notes; hz 0 (rest) or 50..20000; ms 1..5000; total ≤5000 | Plays a custom melody. Nothing plays on any error. |
| `HUSH` | | Stops any melody. |
| `TEXT <scr\|ALL> <line1>[\|<line2>]` | ≤21 chars per line (clipped, never rejected); ASCII 32..126, others render `?`; runs to end of line | Card layout: bold 16 px line 1, 5×7 line 2. |
| `BIG <scr\|ALL> <text>` | | 24 px, centered. |
| `CLEAR <scr\|ALL>` | | Blank the glass. |
| `BLIT <scr> <page 0-3\|ALL> <base64>` | exactly one glass per line (`ALL` or a list → `ERR BLIT sel`); exactly 172 chars (one 128 B page) or 684 chars (all four pages, 512 B); RFC 4648 with `=` padding; validated before any I2C | Raw SSD1306 page bytes (one byte = one 8-pixel column, LSB = top). Any font, any graphic. |
| `CONTRAST <scr\|ALL> <0-255>` | | Panel contrast (default 0x8F). |
| `INVERT <scr\|ALL> 0\|1` | | Inverse display. |
| `SCREEN <scr\|ALL> ON\|OFF` | | Panel power. |
| `GET <key>` | | Answers `EVT SET <key> <value>`. |
| `SET <key> <value>` | numeric | Changes RAM only; `flip` and `ug_mode` take effect immediately. |
| `SAVE` | | Writes settings to flash. `ERR SAVE ratelimit <s>` if less than 10 s since the last write; a save with nothing changed is a successful no-op. |
| `RESET` | ignored for 5 s after boot (`ERR RESET early`) | Reboots the board (it re-enumerates). |

Settings keys: `brightness 0-100`, `ug_brightness 0-100`, `ug_mode 0|1|2` (off / status echo /
warm lamp), `volume 0-4`, `chime 0-2` (fanfare / ding / soft), `flip 0|1`.

Colors are `rrggbb` hex (case-insensitive) or one of the built-in names: `off white red green
blue yellow orange violet cyan magenta thinking question permission error done unread subagent
lamp`. **Colors on the wire are final** — the user's brightness setting is not applied (the
daemon applies it for you unless you ask for raw). The board's total-current limiter always
applies: a frame that would exceed 220 mA is scaled down proportionally.

Selectors:

| Selector | Meaning |
|---|---|
| `<sel>` for LEDs | `3`, a list `0,4,7`, or `ALL` / `GLASS` (0-4, above the screens) / `UG` (5-9, underglow). Indices are 0-based, left to right. |
| `<scr>` for glasses | `2`, a list `1,3`, or `ALL`. 0-based, left to right. |

In `OWN` / `RELEASE`, `ALL`, `GLASS`, `UG` mean every LED or glass of that kind. In paint
verbs they mean "every one you currently own"; an explicit list must be fully owned, otherwise
`ERR <verb> NOTOWNED` and nothing is painted.

## Ownership

Unowned LEDs, glasses, the buzzer and the encoder keep the shipped Glowbug behavior (agent
cards, curated colors, chimes, scrolling, the screensaver worm, the menu). Owning something
turns it into a raw canvas:

- `OWN LED <sel>` — the LEDs start dark and obey `LED …`. The firmware's state colors no longer
  touch them.
- `OWN GLASS <sel>` — the glasses are cleared and obey `TEXT/BIG/CLEAR/BLIT/CONTRAST/INVERT/
  SCREEN`. Agent cards, spinners and the scroll compositor skip them.
- `OWN SOUND` — mutes the firmware's own chimes. Not required to play: `SOUND`/`TONE` always
  work and outrank every firmware chime.
- `OWN ENC` — the firmware stops acting on the knob (no scrolling, no menu on click or 1 s
  hold). Encoder events are reported regardless (see Events).
- `OWN ALL` — all of the above.
- `FOR <ms>` on any claim auto-releases that resource on the board (no host timer needed).
  Re-claiming something you already own only changes its deadline — the LED animation or
  glass content stays as it is (an untimed re-claim makes it indefinite again). Only a fresh
  claim starts a LED dark / a glass cleared.
- Owning any glass or LED also **aborts the welcome animation**, keeps the screensaver off
  and keeps the board from its 1 h sleep.

Released on any of: `RELEASE`, `FOR` expiry, **15 s with no line at all from the host**,
`HELLO`, USB re-enumeration, the host dropping DTR (closing the port). A released glass is
cleared, its panel contrast/invert/power return to defaults, and the agent card that belongs
there is repainted. Every ownership change is reported once per loop pass:

```
EVT OWN LED <hex, bit i = LED i> GLASS <hex, bit g = glass g> SOUND 0|1 ENC 0|1
```

**The device menu.** A ≥3 s hold on the encoder always opens the on-device menu, even when a
host owns the encoder (a reserved gesture: the frozen firmware keeps one guaranteed way to its
own settings). Any click or 1 s hold opens it when the encoder is not owned. The menu borrows
all five glasses and LEDs 0-4 for up to 8 s:

- `EVT MENU 1` is sent before the menu paints. Glass paints and `OWN GLASS …` during the
  menu answer `ERR <verb> BUSY`; `LED …` and `OWN LED …` are accepted and take effect when
  the menu closes. (`BUSY` is also the answer for a glass paint in the sub-second window
  after enumeration before the panels are initialised.)
- `EVT MENU 0` is sent when the menu has closed. **Owned LEDs resume their animations by
  themselves; owned glasses must be redrawn by the host** (the board has no frame buffer to
  restore them from).

## Events (board → host)

| Line | When |
|---|---|
| `EVT HELLO <fw> PROTO 4 SLOTS 32` | USB enumeration, and in answer to `HELLO`. |
| `EVT INFO …` | Answer to `INFO`. |
| `EVT OWN …` | Any ownership change (claim, release, expiry, timeout, reset). |
| `EVT ENC <±n>` | Knob turned; `n` is the net number of steps since the last report (a fast spin batches). Always sent, except while the device menu is open. |
| `EVT CLICK` / `EVT HOLD` | Button released before 1 s / held 1 s. Always sent, except while the menu is open. A ≥3 s hold produces `EVT HOLD` then `EVT MENU 1`. |
| `EVT MENU 1\|0` | Device menu opened / closed. |
| `EVT SET <key> <value>` | Answer to `GET`. |
| `EVT ECHO <token>` | Answer to `ECHO`. |
| `ERR <verb> <why>` | Any refused line. `why` is one word (`unknown arg range sel color op key name notes count total len b64 page for token muted NOTOWNED BUSY early toolong`), plus `ratelimit <s>`. |
| `OK <verb>` | Only under `REPLY ON`. |

Sound note: if the user has set the desk to silent (`volume 0`), `SOUND` answers
`ERR SOUND muted` and `TONE` answers `ERR TONE muted`, even with an explicit `VOL`. The host
may `SET volume <n>` (RAM only) first if it must be heard.

## Timing and throughput

- LED frames are rendered at 125 Hz; fade/pulse/blink periods below 16 ms are refused.
- A full repaint of one glass takes about 6 ms of I2C; the board keeps servicing USB between
  the pages of a `BLIT … ALL`, so a host streaming all five glasses reaches roughly 25-30
  full-board frames per second (the conformance run records the measured figure).
- The device menu's open/close animations block the board for about 0.5 s; lines sent
  meanwhile queue in the board's 512 B USB buffer and are processed afterwards.
- `SAVE` is limited to one flash write per 10 s; the settings page is rated for 10,000 writes.
- Every `TONE` is capped at 5 s and every built-in chime at 2 s by a deadline that runs even
  if the main loop hangs.

## Legacy verbs (unchanged from firmware 1.x, byte for byte)

```
SLOT <1-32> STATE <thinking|question|permission|unread|done|error|closing|arriving|idle> NAME <t> DETAIL <t> SUB <0|1> SID <8ch>
SLOT <1-5> COLOR <rrggbb> MODE <solid|pulse|blink|glow|cycle> LINE1 <t> LINE2 <t>
PING          heartbeat (1/s from the daemon). Any line at all counts as the host being
              alive; 15 s without one = host offline (agent slots wiped, ownership released)
WAKE          the user is back at the computer
BRIGHT <0-100>            = SET brightness (RAM only)
UG MODE <off|echo|lamp> [COLOR <rrggbb>]   = SET ug_mode (RAM only)
DFU           reboot into the ROM USB-DFU bootloader (use `glowbug rescue`)
```

Frozen quirks, documented rather than fixed: `PING` and `WAKE` are matched by prefix
(`PINGX` is a `PING`); `HELLO` and `DFU` are exact matches; `SLOT` sub-fields are located by
substring search, so a name containing ` DETAIL ` is cut there.

Compatibility: daemon 1.5.0 with firmware 2.0.0 gets the full agent display (it drops the new
`EVT` lines); daemon 2.0.0 with firmware 1.4.x gets the agent display and refuses the API
with `proto_too_old`.

## Host socket API (daemon 2.0.0, api 2)

Programs never open the serial port while the daemon runs; they talk to the daemon over its
unix socket `~/Library/Application Support/Glowbug/daemon.sock` (mode 0600, same user only).
One JSON object in (terminated by `\n` or by closing your write side), one JSON line back:

```
printf '{"cmd":"show","screen":3,"color":"green","line1":"Build OK","sound":"ding","for":5}\n' \
  | nc -U "$HOME/Library/Application Support/Glowbug/daemon.sock"
```

Replies are `{"ok": true, "api": 2, …}` or `{"ok": false, "api": 2, "error": "<code>",
"message": "…"}` with codes `bad_request unknown_cmd bad_arg no_board proto_too_old busy
board_err timeout`. Commands: `report info show led text sound raw own release settings
palette events`. **Host indices are 1-based** (screens and status LEDs 1-5, underglow
`ug1`-`ug5`, groups `all`/`glass`/`ug`); the daemon translates to the 0-based wire above.
Names for colors and sounds are resolved by the daemon from its palette (`glowbug palette`,
`~/.glowbug/palette.json`, `~/.glowbug/sounds.json`) and are never sent to the board, which is
why the tables can grow without a firmware change. `events` streams the board's events as JSON
lines; the daemon also synthesizes `board` (online/offline) and `redraw` (after it restored
your owned glasses following a menu or a re-enumeration — resend animated content). Full
request/response shapes: [API.md](API.md) and the `glowbug` module docstrings.
