# Glowbug — Troubleshooting

## One of my coding agents isn't showing up

Run `glowbug doctor` — it prints a line per agent, and that line tells you
which of these you've hit:

**"not installed"** — Glowbug can't find it. It looks for `~/.claude`,
`~/.cursor`, `$CODEX_HOME` (default `~/.codex`), and Antigravity's config
under `~/.gemini`. If the tool keeps its config somewhere else, that's the
gap — open an issue with the path and we'll add it.

**"installed but not connected yet"** — run `glowbug install`. (Normally the
daemon does this for you within a few minutes of you installing a new tool.
It won't, on purpose, if you previously deleted Glowbug's hook by hand.)

**"connected — hooks only attach to NEW sessions"** — the wiring is in place
but that tool hasn't sent an event yet. **Start a new session.** Hooks never
apply retroactively to a session that was already open. Then watch the line
change to "last event Ns ago".

**Still nothing after a new session:**

- **Codex** needs hooks switched on — add to `~/.codex/config.toml`:
  ```toml
  [features]
  hooks = true
  ```
  Glowbug never edits that file, so this one is always yours to do.
- **Cursor CLI** (`cursor-agent`) fires fewer events than the Cursor app, and
  older versions may not read the global `~/.cursor/hooks.json` at all. The
  app is the reliable one today.
- **Antigravity** has no session-start event, so nothing appears until the
  agent's *first tool call* — a pure-chat reply may never light a screen.
- **Cursor showing a hex id** (`8aada7ae`) instead of the chat name — Cursor's
  hooks don't include a title. Glowbug 1.4.11+ reads the name from Cursor's
  local DB for sessions it already knows about. `glowbug install` from a
  current tree, then wait ~1.5s (renames follow the same way).
- Check the agent's own hook config actually points at
  `~/.glowbug/glowbug-hook.py`, and that the file is executable.
- `tail -f ~/Library/Logs/glowbug.log` shows every event as it arrives.

**A state I expected never lights up** — some are genuinely unavailable; see
the support matrix in the README. Cursor has no watch-only approval event
(no pink light), and Codex has no failure event (no red).

---

## Using the API (`glowbug show`, `import glowbug`, the socket)

The CLI prints the daemon's reason on stderr and exits 1; with `--json` the
reply carries an `error` code. These are the codes and what they mean.

**`proto_too_old`** — the board's firmware is older than 2.0.0 (it speaks
PROTO 3; the API needs PROTO 4). Your agent display keeps working, the API
doesn't. `glowbug info` shows the firmware and PROTO. Fix: `glowbug rescue`
flashes the bundled image (needs `brew install dfu-util`) — see Rescue Mode
below.

**`no_board`** — the daemon is running but sees no Glowbug. Is it plugged
in? `glowbug status` says what the Mac sees; a charge-only cable is the
usual cause (next section). "the board went away" mid-request is the same
thing: the port vanished while your lines were queued.

**`no_daemon`** (exit 3) — nothing answered on the socket. The daemon isn't
running: `glowbug install` starts it (and installs the LaunchAgent that
keeps it running at login). If the message says **"the running daemon
predates the API"**, an old 1.5.0 daemon answered — run `glowbug install`
from the new tree; it replaces and restarts it. If you set `GLOWBUG_SOCK`,
the daemon and your program must agree on it.

**My text / light vanished** — five things end ownership; the reply's
`held_until` and `glowbug info` (the `owned` line) tell you which:

- `--for` ran out. That's the point of it.
- **15 s of silence.** The board releases everything if it hears nothing
  from the host for 15 s — the daemon pings it every second, so that means
  the daemon stopped, restarted, or was upgraded, or the Mac slept. A
  restarted daemon also starts the board with a clean session, so nothing
  you painted before survives it: paint again.
- **The board re-enumerated while the daemon kept running** (sleep/wake,
  re-plug). The daemon replays what it still has — your claims with their
  remaining time and the last color / text it sent to each LED and screen —
  and sends a `redraw` event (`"reason": "hello"`). Streamed content (raw
  `BLIT` frames, anything animated from the host) must be re-sent by you.
- **The device menu** (a knob hold) borrows all five screens for up to 8 s.
  When it closes the daemon repaints your screens (`redraw`, `"reason":
  "menu"`); owned LEDs resume by themselves.
- Something called `glowbug release` — with no arguments it releases
  *everything*, from every program.

**Another program keeps overwriting my LED** — ownership is one pool for
every program on this Mac, not per program, so the last writer wins.
Give each job its own screen, pass `--for` so signals expire instead of
squatting, and watch `glowbug events --filter own` to see who claims what.

**`busy`** — the daemon is protecting the board: its outgoing queue is full
(the board isn't keeping up — ~25-30 full-board frames per second is the
ceiling), 64 connections are already open, 16 event streams are already
open, or your `events` reader let 256 events pile up (the stream ends with
a `busy` line). Wait a moment and retry; slow your frame rate.

**`timeout` / `board_err`** — `raw --confirm` and `settings` wait for the
board to acknowledge. `timeout`: no answer within 1 s (the board is mid-blit
or wedged — `glowbug info` shows `txdrop` and `up`). `board_err`: the board
refused a line; the reply's `errors` list quotes its `ERR` replies (see
PROTOCOL.md for the one-word reasons — `NOTOWNED`, `BUSY`, `range`, …).

**My sound didn't play** — sounds are fire-and-forget, so a refusal shows
up only as an event. Run `glowbug events --filter err` while you retry.
`ERR SOUND muted` means the desk is set to silent: fix it with
`glowbug settings set volume 2` (then `glowbug settings save` if you want
it kept).

**`glowbug events` is the debugging tool.** Run it in a second terminal
while you drive the board: every claim and release (`own`), every refusal
the board sends back (`err`), the knob (`enc`, `click`, `hold`), the menu
opening and closing (`menu`), the board coming and going (`board`), and the
daemon's own repaints (`redraw`). `glowbug events --filter err,own` is the
usual pair.

---

## My Glowbug is dark and my Mac doesn't see it

**First, the boring checks:**

- Try a different USB-C cable. Charge-only cables are extremely common and carry
  no data — the Glowbug will look completely dead on one.
- Try a different port, and plug directly into the Mac rather than through a hub.
- Run `glowbug status`. If it says `board not found`, the Mac genuinely isn't
  seeing the device.

If none of that helps — and **especially if it worked fine until you updated the
firmware** — use Rescue Mode below.

---

## Rescue Mode

Your Glowbug has a built-in escape hatch. It doesn't matter how badly the
firmware is broken: as long as the device gets power, this works.

**1.** Unplug the Glowbug.

**2.** Press and hold the knob (push straight down, like clicking it) —
   and keep holding.

**3.** While still holding the knob, plug the USB-C cable back in.

**4.** Keep holding for two more seconds, then let go.

The middle screen will read:

```
   RESCUE MODE
   Ready for update
```

No welcome animation, no lights — just that. It means the Glowbug is waiting
for new firmware. (If the screens stay completely blank, see the last section.)

**5.** With it still plugged in, run:

```sh
glowbug rescue
```

That reinstalls the last known-good firmware. About ten seconds later, the
welcome animation plays and you're back to normal.

Since firmware 2.0.0 the first 2 KB of the chip (page 0) hold a small
resident bootloader, and the app lives right after it. The image `glowbug
rescue` carries is the **production image** — bootloader plus app — written
in one pass from the start of flash. That is what makes rescue work on every
board: a fresh one from the factory, a 1.4.x board that has no bootloader
yet, and a 2.0.0 board (where it simply rewrites the identical, frozen
bootloader). Before it touches the board it checks the image: a file that
isn't a Glowbug production image (no `GLWB` bootloader id at offset `0xC0`,
no `GLWA` app manifest at `0x8C0`, an app-only image, or an app reset vector
outside the app region) is refused with `Refusing to flash …`, and nothing
is written. The old whole-flash 1.4.x `glowbug.bin` is refused for the same
reason; the message includes the `curl` line that fetches the current image
into `~/.glowbug/firmware.bin`.

---

## Why does this exist?

Firmware updates normally happen over the USB cable, with the Glowbug's own
software cooperating (that's the "UPDATING / do not unplug" screen you see
during a normal update): your Mac says "time to update," the Glowbug steps aside,
and new firmware is written.

That works perfectly — **as long as the firmware on the device is healthy enough
to listen.** If an update ever goes wrong in the specific way that leaves the
device unable to talk over USB, your Mac can't reach it anymore, so the normal
update can't rescue it either. Without an escape hatch, a $0.02 software mistake
would mean opening the case.

Rescue Mode skips the firmware entirely. Holding the knob at power-up talks to a
tiny program burned into the processor at the factory that cannot be erased,
overwritten, or broken by anything we ship. It's the same idea as holding a
button while powering on a phone to reach its recovery screen.

You will probably never need it. It's a seatbelt.

---

## Rescue Mode didn't work either

If you don't get the RESCUE MODE screen, the device isn't reaching that code at
all — which usually means it isn't getting power (bad cable/port) rather than a
firmware problem. Recheck the cable first.

If you've confirmed a known-good data cable and it's still unreachable, get in
touch — that one needs the case opened, and we'd rather do it than have you
do it.
