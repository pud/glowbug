# Installing Glowbug (instructions for AI coding agents)

A user has asked you to install Glowbug — the desk device that shows their
Claude Code sessions. This takes about a minute.

## Steps

1. Clone and install:
   ```sh
   git clone https://github.com/pud/glowbug /tmp/glowbug-install
   python3 /tmp/glowbug-install/glowbug.py install
   ```
2. The installer prints a self-check: the daemon, the board, and one line per
   coding agent it found. Success looks like:
   ```
   ✓ daemon running
   ✓ board connected (/dev/cu.usbmodemXXXX)
   ✓ Claude Code    connected
   ✓ Cursor         connected
   — Codex          not installed
   ```
3. Report the result to the user, including:
   - If the board line shows ✗: ask them to plug the Glowbug in (any USB-C
     port) — the daemon will find it automatically within seconds, no rerun
     needed.
   - **Hooks only attach to NEW sessions** — sessions already running
     (including yours) show thinking/idle only until restarted. For Claude
     Code, idle/thinking works immediately either way (it has a session
     registry the daemon can read); the other tools are hook-only.
   - If a line shows `! Codex needs one setting turned on`, relay those two
     TOML lines to the user. Glowbug never edits `config.toml` itself.
   - Tools installed *later* connect themselves — the daemon re-checks every
     few minutes. Nothing to re-run.

## What the installer does (so you can explain it)

- Copies itself to `~/.glowbug/glowbug.py` and writes the hook
  (`~/.glowbug/glowbug-hook.py`, embedded in the source as `FORWARDER_SOURCE`)
- Adds hook entries to whichever of these exist — always an additive JSON
  merge, always with a `.glowbug-backup` alongside, idempotent on re-run, and
  it refuses to touch a config file that doesn't parse:
  `~/.claude/settings.json`, `~/.cursor/hooks.json`, `$CODEX_HOME/hooks.json`,
  and Antigravity's `hooks.json` (one top-level `"glowbug"` key)
- Never creates a config directory for a tool that isn't installed, and never
  writes TOML (Codex's one `features.hooks` line is printed, not written)
- Installs + starts a LaunchAgent (`dev.glowbug.daemon`) so the daemon runs
  at login, and records what it wired in `~/.glowbug/state.json` — delete a
  Glowbug hook by hand and it stays deleted

## Privacy notes you can relay if asked

Local-only: no network code anywhere in the file. The hook forwards only
eight metadata fields (event name, session id, title, cwd, tool name, error
type, which tool it came from, and an idle flag) — never prompt text, tool
arguments, or file contents. Transcript paths, model names, and free-text
error strings are deliberately dropped. The device receives only session
names + status words.

Glowbug also subscribes only to events it can *watch*: it never registers a
hook that could deny or delay a tool call, and the forwarder always exits 0.

## API for agents

Once Glowbug is installed, `glowbug` is on the PATH (or `python3
~/.glowbug/glowbug.py …`). You can use it from any project to signal the
user — a screen, its LED, a sound — without hiding their agent display for
longer than you mean to. Screens and status LEDs are 1-5 left to right,
underglow ug1-ug5. **Always pass `--for`** on a transient signal: the board
releases the screen by itself when the time is up, even if you are gone.

```sh
glowbug show 3 --color green --line1 "Tests passed" --line2 "412 ok" --sound ding --for 30
glowbug show 3 --color red --mode blink --line1 "Build failed" --line2 "exit 1" --sound fail --for 60
glowbug show 2 --color amber --mode pulse --line1 "Need input" --line2 "see terminal" --sound soft --for 120
glowbug led ug violet --mode pulse --for 600          # underglow while a long job runs
glowbug text 5 "Deploying" "step 3 of 7" --for 20
glowbug sound coin
glowbug release                                        # hand everything back early
```

- `--json` on any of these prints the daemon's reply
  (`{"ok": true, "api": 2, "lines": [...], "held_until": …}`).
  `glowbug palette --json` lists every color and sound name — users add
  their own in `~/.glowbug/palette.json` / `sounds.json`, so ask rather
  than hard-coding a list.
- Exit codes: `0` ok · `1` the daemon refused (reason on stderr; the
  `error` code in `--json` is one of `bad_arg no_board proto_too_old busy
  board_err timeout bad_request unknown_cmd`) · `2` usage · `3` no daemon.
- **`proto_too_old`**: the board's firmware predates PROTO 4 (older than
  2.0.0). The agent display still works; the API does not. The fix is
  `glowbug rescue` (flashes the bundled image; needs `brew install
  dfu-util`) — ask the user before reflashing their board.
- **`no_daemon`** (exit 3): the daemon isn't running, or a pre-API (1.5.0)
  daemon still is — `glowbug install` starts or updates it. **`no_board`**:
  the daemon is up but nothing is plugged in.
- From Python: `import glowbug; glowbug.show(3, color="green",
  line1="Done", seconds=30)`. `pip install glowbug` in the project venv, or
  `sys.path.insert(0, os.path.expanduser("~/.glowbug"))` for the installed
  copy. Refusals raise `glowbug.GlowbugError` (`.code`, `.message`).
- Runnable examples: `examples/ci_light.sh`, `examples/notify_when_done.sh`,
  `examples/pomodoro.py`.

Etiquette:

- Every transient signal carries `--for`. An untimed claim stays until
  something releases it, and hides the user's agent session on that screen.
- Never send `glowbug raw DFU` (the daemon refuses it anyway — it's the
  firmware-update trigger; `glowbug rescue` is the door for that).
- Don't `own enc` (the knob) unless your program reads `glowbug events` and
  acts on `enc` / `click` / `hold` — an owned knob does nothing for the user
  (a ≥3 s hold still opens the device menu).
- Ownership is one pool for every program on the Mac; last writer wins.
  Pick one screen per job and reuse it. `glowbug info` shows what is owned
  right now; `glowbug release` hands everything back.

## Rescue / reflash

`python3 glowbug.py rescue` (or `glowbug rescue`) reflashes the bundled
known-good image in `firmware/` (glowbug.bin + VERSION + SHA256SUMS,
sha256-verified). Works from a running board (sends the in-band DFU command)
or a bricked one (user holds the knob while plugging in → ROM bootloader →
"RESCUE MODE" on the middle screen). Requires dfu-util (`brew install
dfu-util`). The command never touches the network — if the image is missing
it prints a curl command for the user to run. Since firmware 2.0.0 it
writes the boot+app production image from the start of flash (page 0 holds
a resident bootloader; rewriting it with the identical frozen copy is safe,
and it is what upgrades a 1.4.x board) and refuses any file that isn't a
Glowbug production image (no `GLWB` id at offset `0xC0` / no `GLWA` manifest
at `0x8C0`), including the old whole-flash 1.4.x `glowbug.bin`.

**When firmware is updated:** rebuild in the (private) firmware tree, then
refresh all three files here — `cp firmware.bin firmware/glowbug.bin`, update
`firmware/VERSION`, regenerate `firmware/SHA256SUMS` (`shasum -a 256
glowbug.bin > SHA256SUMS` from inside `firmware/`). They are the rescue image.

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```
