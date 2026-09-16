#!/usr/bin/env python3
"""Glowbug — a machined aluminum bar that shows your coding-agent sessions.

    glowbug.py              run the daemon (LaunchAgent does this for you)
    glowbug.py install      set everything up (daemon, hooks, autostart)
    glowbug.py uninstall    remove everything cleanly
    glowbug.py rescue       reflash firmware (works even on a "bricked" board)
    glowbug.py status       one-line health check
    glowbug.py doctor       verbose health check (paths, per-tool wiring)
    glowbug.py --version

    glowbug.py show 3 --color green --line1 "Build OK" --sound ding --for 5
    glowbug.py led / text / sound / raw / own / release / events / info /
               palette / settings      (--help on any of them; PROTOCOL.md)

Everything Glowbug knows stays on this Mac. There is no network code in this
file — it reads your coding agents' local hook events (Claude Code's
session registry, and Cursor chat titles from its local DB), and writes to
the Glowbug device over USB serial. Your own programs talk to the daemon
over its unix socket (or `import glowbug`); the daemon is the only writer
to the port. That's it. Read it and see: it's one file, standard library
only.

https://glowbug.dev · https://github.com/pud/glowbug · MIT license
"""

import argparse
import collections
import glob
import json
import os
import queue
import re
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import termios
import threading
import time
import traceback

VERSION = "2.0.0"
NUM_SLOTS = 5                        # pre-negotiation window (old boards show 5)
BOARD_SLOTS_MAX = 32                 # protocol v3 cap: fw 1.4.0+ advertises
                                     # "EVT HELLO <fw> PROTO 3 SLOTS 32"; the
                                     # window widens only after that is seen
                                     # (an old board silently DROPS SLOT n>5,
                                     # so pushing 32 blind would show it the
                                     # five OLDEST agents — never widen blind)
SESSION_STALE_S = 12 * 3600          # silent sessions free their slot
PING_INTERVAL_S = 1.0
HID_POLL_S = 5.0                     # user-presence check cadence
AWAY_S = 600.0                       # idle this long = "the user left"
REGISTRY_POLL_S = 1.5

# Codex and Antigravity have no session registry — we only know what their
# hooks tell us. Cursor is the same for liveness, but its hooks omit the
# chat title, so we poll names (only) from Cursor's local composerHeaders.
# A killed IDE never sends its "stop"/"session end" event, so these two
# timeouts are what stop a dead session from owning a screen forever.
WORK_STALE_S = 300                   # silent "working" session -> idle
HOOK_SESSION_TTL_S = 2 * 3600        # silent session -> closed
DONE_S = 30.0                        # green "just finished" celebration window.
                                     # Host-owned (2026-08-16) so it SHIFTS with
                                     # the agent when the ticker compacts —
                                     # firmware used to synthesize it per-slot
                                     # and the green got left behind / cleared.
                                     # Matches firmware DONE_FLASH_MS (30s).
AUTOWIRE_POLL_S = 300                # look for newly-installed coding agents

# Ghost-buster (user directive 2026-08-16: NEVER show agents that don't
# exist). A hard-closed IDE never sends its sessionEnd hook, and before this
# its sessions squatted a screen until the 2h TTL. So we ask the OS: if a
# source's application has no running process AT ALL, every session it owns
# is provably dead — reaped within seconds instead of hours. (An app that's
# still open with a quietly-abandoned session inside is a different case;
# that one still falls to the idle/TTL timers above.)
APP_PROBE_S = 3.0                    # how often to scan the process table
APP_GONE_S = 10.0                    # app unseen this long -> sessions dead

# Appearance debounce for hook-only sources (user idea 2026-08-16): a
# session gets a screen only once it has been alive HOOK_APPEAR_S; one
# that dies younger than that was a micro-burst (tools fire sub-second
# thought/response/stop flurries on auxiliary conversations) and is never
# shown — no flash, no red farewell. Race-free because it's timestamp
# arithmetic recomputed on every push, not a stateful flag: visible =
# age >= HOOK_APPEAR_S; farewell = died AFTER having become visible.
# Claude Code is exempt (registry-born, no burst class, appears instantly).
HOOK_APPEAR_S = 2.0

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, ".glowbug")
SOCK_PATH = (os.environ.get("GLOWBUG_SOCK")
             or os.path.join(HOME, "Library", "Application Support", "Glowbug", "daemon.sock"))
SESS_STATE = os.path.join(HOME, "Library", "Application Support", "Glowbug", "sessions.json")
LOG_PATH = os.path.join(HOME, "Library", "Logs", "glowbug.log")
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", "dev.glowbug.daemon.plist")
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")
# Cursor stores chat titles here; its hooks never send a session_title.
CURSOR_STATE_DB = os.path.join(
    HOME, "Library", "Application Support", "Cursor",
    "User", "globalStorage", "state.vscdb")

# ------------------------------------------------------------- the host API
# The board speaks PROTO 4 (PROTOCOL.md) and that protocol is FROZEN: the
# shipped units never get a firmware upgrade, so everything a program could
# want is reachable through raw commands, and NAMES are resolved here on
# the host into raw values before anything is sent. The board never sees a
# name it doesn't already know, so these tables can grow forever.
SOCKET_API = 2          # daemon.sock reply shape: {"ok", "api": 2, ...}
PROTOCOL_MIN = 4        # oldest board PROTO the API will drive. A PROTO 3
                        # board still gets the agent display; API calls
                        # answer proto_too_old -> `glowbug rescue`.

# TONE limits the board enforces (its INFO line reports the same numbers).
NOTES_MAX = 32          # notes per sequence
TONE_HZ_MIN = 50        # TIM1's period register is 16-bit: nothing lower
TONE_HZ_MAX = 20000
TONE_MS_MAX = 5000      # per note AND for the whole sequence

# Named colors, name -> "RRGGBB" (uppercase). The first two groups are
# also baked into the firmware (so a bare terminal can type `LED 0 SET
# red`); the product tints are the board's own state colors at full
# brightness, read at the MID-POINT of each eased channel from fw 1.4.17
# state_render — line numbers are pcb/pud76-coderdong/v5/firmware/src/
# main.cpp in the pudtronics repo. Everything after that is host-only
# sugar: add your own in ~/.glowbug/palette.json, no firmware involved.
# Tip for fades/pulses: channels at 0 or high counts scale cleanly; a low
# count (0x20 at 30 % brightness = 0x0A) steps visibly.
PALETTE = {
    "off":        "000000",
    "white":      "FFFFFF",
    "red":        "FF0000",
    "green":      "00FF00",
    "blue":       "0000FF",
    "yellow":     "FFFF00",
    "orange":     "FF8000",
    "violet":     "8000FF",
    "cyan":       "00FFFF",
    "magenta":    "FF00FF",
    # product tints
    "thinking":   "5400FF",   # SS_THINKING :781 — R eases 0x18..0x90, B full
    "question":   "FF3800",   # SS_QUESTION :808 — R full, G eases 0x18..0x58
    "permission": "FF1C50",   # SS_PERMISSION :795-798 — R full, G 0x08..0x30,
                              # B pinned 0x50
    "error":      "C00000",   # SS_ERROR :824-825 — 0xC0 red / off, 500 ms
                              # blink: a hard blink has no mid-point, this
                              # is its ON phase
    "done":       "008800",   # SS_DONE :819 — G eases 0x50..0xC0
    "unread":     "00C000",   # SS_UNREAD :822-823 — solid
    "subagent":   "303030",   # subagent_pulse :764 — white 0x10..0x50
    "lamp":       "FF6412",   # LAMP_WARM_WHITE :714 — R255 G100 B18
    # host extras
    "warm":       "FF6412",   # = lamp
    "pink":       "FF40A0",
    "ember":      "FF3000",
    "amber":      "FFA000",
    "gold":       "FFC800",
    "lime":       "80FF00",
    "mint":       "40FFA0",
    "teal":       "00C0A0",
    "sky":        "40A0FF",
    "indigo":     "4000FF",
    "purple":     "A000FF",
    "rose":       "FF2080",
    "coral":      "FF6040",
    "cool":       "C0E0FF",
    "grey":       "404040",
    "gray":       "404040",
    "dim":        "101010",
}

# Named sounds, name -> [(hz, ms), ...], hz 0 = rest. The first seven are
# the board's own chimes, copied note-for-note from fw 1.4.17 main.cpp
# :173-191 (MELODY_CLICK/DING/SOFT/BLIP/HELLO/BYE/BOOT), so `SOUND ding`
# on the wire and a TONE of these notes are the same thing. The buzzer is
# loudest near its 2.7 kHz resonance. Extras are host-only; add your own
# in ~/.glowbug/sounds.json.
SOUNDS = {
    "fanfare": [(1568, 55), (2093, 55), (2637, 55), (3136, 85),   # G6 C7 E7 G7,
                (0, 30), (3136, 55), (4186, 200)],                 # rest, G7 grace, C8
    "ding":    [(2637, 70), (0, 20), (3951, 260)],                 # E7 -> B7 doorbell
    "soft":    [(3136, 90), (2637, 90), (2093, 200)],              # G7 E7 C7 descend
    "blip":    [(3136, 45)],                                       # G7 confirm
    "hello":   [(2637, 25), (3520, 40)],                           # E7 -> A7 birth chirp
    "bye":     [(2637, 70), (2093, 70), (1568, 150)],              # E7 C7 G6 farewell
    "boot":    [(2093, 50), (2637, 50), (3136, 50), (4186, 150)],  # C7 E7 G7 C8 sparkle
    # host extras
    "tick":    [(4000, 12)],
    "flutter": [(3136, 30), (3520, 30), (3951, 30), (3520, 30), (3136, 30)],
    "snap":    [(4186, 20), (0, 10), (2093, 30)],
    "beep":    [(2700, 120)],
    "double":  [(2700, 80), (0, 60), (2700, 80)],
    "rise":    [(1568, 60), (2093, 60), (2637, 60), (3136, 120)],
    "fall":    [(3136, 60), (2637, 60), (2093, 60), (1568, 120)],
    "warn":    [(2093, 150), (0, 80), (2093, 150)],
    "fail":    [(1568, 120), (0, 30), (1319, 120), (0, 30), (1047, 240)],  # G6 E6 C6
    "alarm":   [(3136, 120), (2093, 120)] * 3,
    "coin":    [(3951, 60), (5274, 300)],                          # B7 -> E8
    "knock":   [(1047, 30), (0, 80), (1047, 30)],
    # ... --- ...   dot 100 / dash 300 / gap 100 / letter gap 300 = 3.0 s
    "sos":     [(2700, 100), (0, 100), (2700, 100), (0, 100), (2700, 100), (0, 300),
                (2700, 300), (0, 100), (2700, 300), (0, 100), (2700, 300), (0, 300),
                (2700, 100), (0, 100), (2700, 100), (0, 100), (2700, 100), (0, 300)],
}

# The user's own tables. Read by load_tables(); NEVER created by Glowbug.
PALETTE_PATH = os.path.join(APP_DIR, "palette.json")   # {"name": "RRGGBB"}
SOUNDS_PATH = os.path.join(APP_DIR, "sounds.json")     # {"name": [[hz, ms], ...]
                                                       #  or "hz:ms,hz:ms"}

NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,23}")   # color/sound names (lowercased)
_HEX3_RE = re.compile(r"#[0-9a-fA-F]{3}")
_HEX6_RE = re.compile(r"#?[0-9a-fA-F]{6}")


def parse_color(s, colors=None):
    """'#0f0' / '#00ff00' / '00FF00' / 'green' -> 'RRGGBB' (uppercase).
    Names are case-insensitive, looked up in `colors` (default: the
    built-in PALETTE; pass load_tables()[0] for the user-merged one). Hex
    wins over names, so a name can never be six hex digits."""
    if not isinstance(s, str):
        raise ValueError("unknown color %r (glowbug palette lists them)" % (s,))
    t = s.strip()
    if _HEX3_RE.fullmatch(t):
        return "".join(c + c for c in t[1:]).upper()
    if _HEX6_RE.fullmatch(t):
        return t.lstrip("#").upper()
    key = t.lower()
    table = PALETTE if colors is None else colors
    if NAME_RE.fullmatch(key) and key in table:
        return table[key]
    raise ValueError("unknown color '%s' (glowbug palette lists them)" % s)


def check_notes(notes):
    """Validate [(hz, ms), ...] against the board's TONE limits; returns
    the list. Errors name the offending note (1-based)."""
    if not notes:
        raise ValueError("no notes")
    if len(notes) > NOTES_MAX:
        raise ValueError("%d notes (max %d)" % (len(notes), NOTES_MAX))
    total = 0
    for i, (hz, ms) in enumerate(notes, 1):
        if not (hz == 0 or TONE_HZ_MIN <= hz <= TONE_HZ_MAX):
            raise ValueError("note %d (%d:%d): hz must be 0 or %d..%d"
                             % (i, hz, ms, TONE_HZ_MIN, TONE_HZ_MAX))
        if not 1 <= ms <= TONE_MS_MAX:
            raise ValueError("note %d (%d:%d): ms must be 1..%d"
                             % (i, hz, ms, TONE_MS_MAX))
        total += ms
    if total > TONE_MS_MAX:
        raise ValueError("total %d ms (max %d)" % (total, TONE_MS_MAX))
    return notes


def _note(i, it):
    """One (hz, ms) from either grammar; `i` is 1-based for the error."""
    if isinstance(it, str):
        hz, sep, ms = it.partition(":")
        try:
            if sep:
                return int(hz.strip()), int(ms.strip())
        except ValueError:
            pass
        raise ValueError("note %d (%r): want hz:ms" % (i, it.strip()))
    try:
        hz, ms = it
    except (TypeError, ValueError):
        hz = ms = None
    if isinstance(hz, int) and isinstance(ms, int) \
            and not isinstance(hz, bool) and not isinstance(ms, bool):
        return hz, ms
    raise ValueError("note %d (%r): want [hz, ms] integers" % (i, it))


def parse_notes(spec, sounds=None):
    """A sound name, a 'hz:ms,hz:ms,...' string, or a list of (hz, ms)
    pairs -> validated [(hz, ms), ...] (a fresh list). Names are case-
    insensitive, looked up in `sounds` (default: the built-in SOUNDS)."""
    if isinstance(spec, str):
        t = spec.strip()
        if ":" not in t:
            key = t.lower()
            table = SOUNDS if sounds is None else sounds
            if NAME_RE.fullmatch(key) and key in table:
                return list(table[key])
            raise ValueError("unknown sound '%s' (glowbug sounds lists them)" % spec)
        items = t.split(",")
    else:
        try:
            items = list(spec)
        except TypeError:
            raise ValueError("notes must be a name, 'hz:ms,...' or a list of pairs")
    return check_notes([_note(i, it) for i, it in enumerate(items, 1)])


def scale_color(color, pct):
    """Dim a color to pct % (0..100) per channel, rounding half-up the way
    the board does when it folds brightness in ((c * br + 50) / 100 —
    state_render :782-784) so host and board agree to the count. Accepts
    anything parse_color does; returns 'RRGGBB'."""
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        raise ValueError("brightness must be 0..100, not %r" % (pct,))
    if not 0 <= pct <= 100:
        raise ValueError("brightness must be 0..100, not %d" % pct)
    c = parse_color(color)
    return "".join("%02X" % ((int(c[i:i + 2], 16) * pct + 50) // 100)
                   for i in (0, 2, 4))


def notes_to_wire(notes):
    """[(hz, ms), ...] -> 'hz:ms,hz:ms' — the TONE argument, exactly."""
    return ",".join("%d:%d" % (hz, ms) for hz, ms in notes)


def _user_table(path, warnings):
    """One user JSON table as a dict, or {}. Missing is normal; anything
    else wrong becomes a warning, never an exception — a typo in
    palette.json must not take the daemon down."""
    fname = os.path.basename(path)
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        warnings.append("%s: unreadable (%s) — ignored" % (fname, e))
        return {}
    if not isinstance(data, dict):
        warnings.append("%s: expected a JSON object {name: value} — ignored"
                        % fname)
        return {}
    return data


def load_tables(palette_path=None, sounds_path=None):
    """(colors, sounds, warnings): the built-in PALETTE/SOUNDS with the
    user's ~/.glowbug/palette.json and sounds.json merged on top. User
    entries win and may name something already defined (built-in, or
    earlier in the same file). A bad entry is skipped and described in
    `warnings`; the built-in tables are never mutated and the files are
    never created."""
    colors, sounds, warnings = dict(PALETTE), dict(SOUNDS), []
    ppath = PALETTE_PATH if palette_path is None else palette_path
    spath = SOUNDS_PATH if sounds_path is None else sounds_path
    for path, table, parse in ((ppath, colors, parse_color),
                               (spath, sounds, parse_notes)):
        fname = os.path.basename(path)
        for name, val in _user_table(path, warnings).items():
            key = str(name).lower()
            if not NAME_RE.fullmatch(key):
                warnings.append("%s: %r skipped — a name is [a-z][a-z0-9_-]{0,23}"
                                % (fname, name))
                continue
            if table is colors and _HEX6_RE.fullmatch(key):
                warnings.append("%s: %r skipped — six hex digits always read "
                                "as a color, never as a name" % (fname, name))
                continue
            try:
                table[key] = parse(val, table)
            except ValueError as e:
                warnings.append("%s: %r skipped — %s" % (fname, name, e))
    return colors, sounds, warnings


# A typo in the tables above must fail here, at import, not on somebody's
# first `glowbug sound` — the daemon is the only writer to the board.
for _n, _v in SOUNDS.items():
    check_notes(_v)
for _n, _v in PALETTE.items():
    if not (NAME_RE.fullmatch(_n) and len(_v) == 6 and _v == _v.upper()
            and _HEX6_RE.fullmatch(_v)):
        raise ValueError("PALETTE[%r] = %r is not RRGGBB" % (_n, _v))
del _n, _v

# Board geometry the compose functions assume (the board's INFO line
# confirms it: LEDS 10 GLASS 5 UG 5 SCREENS 5). Host indices are 1-based —
# screens and status LEDs 1-5 left to right, underglow ug1-ug5 — and the
# wire is 0-based: screen n -> n-1, ugN -> 4+N.
NUM_GLASS = 5
NUM_LEDS = 10
TEXT_MAX = 21           # chars per OLED text line (the board clips, we warn)
LINE_MAX = 1023         # wire line length, excluding the terminator
FOR_MIN_S = 0.05        # `for` bounds in seconds: the board caps FOR at 24 h
FOR_MAX_S = 86400
PERIOD_MIN_MS = 16      # FADE / PULSE / BLINK period bounds on the wire
PERIOD_MAX_MS = 65535
FADE_MS = 700           # `--mode fade` default
PULSE_MS = 2000         # `--mode pulse` default period; `to` defaults to 30 %
PULSE_TO_PCT = 30
BLINK_MS = 1000         # `--mode blink` default period; `to` defaults to off
SETTINGS_KEYS = {       # GET/SET keys and their ranges (numeric on the wire)
    "brightness": (0, 100), "ug_brightness": (0, 100), "ug_mode": (0, 2),
    "volume": (0, 4), "chime": (0, 2), "flip": (0, 1),
}

# Daemon-side limits. The socket reply is "validated and queued", never
# "delivered": raw lines wait in tx_queue until the serial thread pulls
# them behind the agent-display push (only it ever writes to the port).
TX_API_LOWWATER = 8 * 1024       # serial thread stops pulling when its
                                 # unsent tail is this long (board mid-blit)
TX_API_MAX_BYTES = 512 * 1024    # queue caps -> synchronous `busy`
TX_API_MAX_ENTRIES = 4096
TX_REQUEST_MAX = 32 * 1024       # one request's blob (keeps the serial
                                 # thread's 64 KiB drop-whole cap unreachable)
RAW_MAX_LINES = 256              # lines per `raw` request
CONFIRM_TIMEOUT_S = 1.0          # ECHO barrier wait -> `timeout`
CACHE_GRACE_S = 0.5              # an EVT OWN older than a just-sent OWN
                                 # must not prune the replay cache
MAX_CONNS = 64                   # concurrent socket connections -> `busy`
MAX_SUBSCRIBERS = 16             # concurrent `events` streams -> `busy`
SUB_QUEUE = 256                  # events buffered per subscriber, then dropped
SOCK_READ_TIMEOUT_S = 2.0
SOCK_REQUEST_MAX = 1 << 20       # one request line, bytes
RAW_FORBIDDEN = ("DFU", "REPLY")  # verbs `raw` will not relay

ERROR_CODES = ("bad_request", "unknown_cmd", "bad_arg", "no_board",
               "proto_too_old", "busy", "board_err", "timeout", "no_daemon")


class GlowbugError(Exception):
    """An API refusal. `code` is one of ERROR_CODES; `message` is for
    humans. Raised by the Python helpers when the daemon says ok:false
    (or can't be reached: code "no_daemon"), and used inside the daemon to
    turn a refusal into the {"ok": false, "error", "message"} reply."""

    def __init__(self, code, message=""):
        Exception.__init__(self, message or code)
        self.code = code
        self.message = message or code

    def __str__(self):
        return self.message


def _ok(**kw):
    d = {"ok": True, "api": SOCKET_API}
    d.update(kw)
    return d


def _fail(code, message, **kw):
    d = {"ok": False, "api": SOCKET_API, "error": code, "message": message}
    d.update(kw)
    return d


# ------------------------------------------------- selectors + compose
# Pure functions: a request dict in, the exact wire lines out. They know
# nothing about sockets or the serial port, so every line the daemon can
# send is testable byte-for-byte without hardware. The daemon only adds
# queueing, the brightness setting, and the replay cache on top.

def _sel_tokens(v):
    """Selector input -> lowercase tokens: 3 / "3" / "1,3,ug2" / [1, "ug2"]."""
    if v is None or isinstance(v, bool):
        raise ValueError("missing selector")
    if isinstance(v, int):
        items = [str(v)]
    elif isinstance(v, str):
        items = v.split(",")
    elif isinstance(v, (list, tuple)):
        items = []
        for x in v:
            if isinstance(x, bool) or not isinstance(x, (int, str)):
                raise ValueError("bad selector %r" % (x,))
            items.extend(str(x).split(","))
    else:
        raise ValueError("bad selector %r" % (v,))
    toks = [t.strip().lower() for t in items]
    if not toks or any(not t for t in toks):
        raise ValueError("bad selector %r" % (v,))
    return toks


def parse_screen_sel(v):
    """Host screen selector -> wire tokens. Screens are 1-5 left to right
    -> "0".."4"; "all" -> ["ALL"]; lists "1,3" -> ["0", "2"] (one wire
    line per screen)."""
    out = []
    for t in _sel_tokens(v):
        if t == "all":
            tok = "ALL"
        elif t.isdigit() and 1 <= int(t) <= NUM_GLASS:
            tok = str(int(t) - 1)
        else:
            raise ValueError("bad screen %r: want 1-%d or all" % (t, NUM_GLASS))
        if tok not in out:
            out.append(tok)
    return ["ALL"] if "ALL" in out else out


def parse_led_sel(v):
    """Host LED selector -> wire tokens. Status LEDs 1-5 (under screens
    1-5) -> "0".."4"; underglow ug1-ug5 -> "5".."9"; the groups all /
    glass / ug -> ALL / GLASS / UG. Lists give one wire line per LED."""
    out = []
    n_ug = NUM_LEDS - NUM_GLASS
    for t in _sel_tokens(v):
        if t in ("all", "glass", "ug"):
            tok = t.upper()
        elif t.isdigit() and 1 <= int(t) <= NUM_GLASS:
            tok = str(int(t) - 1)
        elif t.startswith("ug") and t[2:].isdigit() and 1 <= int(t[2:]) <= n_ug:
            tok = str(NUM_GLASS + int(t[2:]) - 1)
        else:
            raise ValueError("bad led %r: want 1-%d, ug1-ug%d, all, glass or ug"
                             % (t, NUM_GLASS, n_ug))
        if tok not in out:
            out.append(tok)
    return ["ALL"] if "ALL" in out else out


def led_name(i):
    """Wire LED index -> host name: 0-4 -> "1".."5", 5-9 -> "ug1".."ug5"."""
    return str(i + 1) if i < NUM_GLASS else "ug%d" % (i - NUM_GLASS + 1)


def sel_indices(kind, sel):
    """Wire selector token ("2", "ALL", "GLASS", "UG", "1,3") -> the set of
    wire indices it names, for `kind` "led" or "glass". Unknown -> empty."""
    n = NUM_LEDS if kind == "led" else NUM_GLASS
    if sel == "ALL":
        return set(range(n))
    if kind == "led" and sel == "GLASS":
        return set(range(NUM_GLASS))
    if kind == "led" and sel == "UG":
        return set(range(NUM_GLASS, NUM_LEDS))
    out = set()
    for t in sel.split(","):
        if not (t.isdigit() and int(t) < n):
            return set()
        out.add(int(t))
    return out


def parse_for(v):
    """`for` in seconds -> milliseconds for `FOR <ms>`, or None for an
    indefinite claim. 0 / None = indefinite; otherwise 0.05..86400 s (the
    board caps FOR at 24 h)."""
    if v is None or v is False:
        return None
    if isinstance(v, bool):
        raise ValueError("for: want seconds, not %r" % (v,))
    try:
        s = float(v)
    except (TypeError, ValueError):
        raise ValueError("for: want seconds, not %r" % (v,))
    if s == 0:
        return None
    if not FOR_MIN_S <= s <= FOR_MAX_S:
        raise ValueError("for: %g s is outside %g..%g" % (s, FOR_MIN_S, FOR_MAX_S))
    return int(round(s * 1000))


def _int_arg(name, v, lo, hi, default=None):
    if v is None:
        return default
    if isinstance(v, bool):
        raise ValueError("%s: want an integer, not %r" % (name, v))
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("%s: want an integer, not %r" % (name, v))
    if not lo <= n <= hi:
        raise ValueError("%s: %d is outside %d..%d" % (name, n, lo, hi))
    return n


def sanitize_text(s):
    """OLED text: one line of printable ASCII (the fonts cover 32..126,
    anything else -> "?"), whitespace collapsed, "|" (the TEXT line
    separator) -> "/", clipped to 21. Returns (text, truncated)."""
    if s is None:
        return "", False
    if not isinstance(s, str):
        s = str(s)
    t = " ".join(s.split())
    t = "".join(c if 32 <= ord(c) <= 126 else "?" for c in t).replace("|", "/")
    return t[:TEXT_MAX], len(t) > TEXT_MAX


LED_MODES = ("solid", "fade", "pulse", "blink", "off")


def compose_led_lines(tokens, req, colors=None, brightness=100):
    """LED paint lines for wire selector tokens. mode solid -> SET, fade ->
    FADE <hex> <fade_ms>, pulse -> PULSE <hex> <to> <period>, blink ->
    BLINK <hex> <to> <period>, off -> OFF. Colors are scaled by
    `brightness` (the board's user setting) unless req["raw"]."""
    mode = req.get("mode")
    mode = "solid" if mode is None else mode
    if not isinstance(mode, str) or mode.lower() not in LED_MODES:
        raise ValueError("mode: want one of %s" % ", ".join(LED_MODES))
    mode = mode.lower()
    if mode == "off":
        return ["LED %s OFF" % t for t in tokens]
    color = req.get("color")
    if color is None:
        raise ValueError("color: required (glowbug palette lists the names)")
    raw = bool(req.get("raw"))

    def resolve(c):
        h = parse_color(c, colors)
        return h if raw else scale_color(h, brightness)

    hexv = resolve(color)
    if mode == "solid":
        arg = "SET %s" % hexv
    elif mode == "fade":
        ms = req.get("fade_ms")
        ms = req.get("period") if ms is None else ms
        arg = "FADE %s %d" % (hexv, _int_arg("fade_ms", ms, PERIOD_MIN_MS,
                                             PERIOD_MAX_MS, FADE_MS))
    else:
        to = req.get("to")
        if to is not None:
            to_hex = resolve(to)
        elif mode == "pulse":
            to_hex = scale_color(hexv, PULSE_TO_PCT)
        else:
            to_hex = "000000"
        period = _int_arg("period", req.get("period"), PERIOD_MIN_MS, PERIOD_MAX_MS,
                          PULSE_MS if mode == "pulse" else BLINK_MS)
        arg = "%s %s %s %d" % (mode.upper(), hexv, to_hex, period)
    return ["LED %s %s" % (t, arg) for t in tokens]


def compose_text_lines(tokens, req):
    """TEXT / BIG / CLEAR lines for wire screen tokens. `big` alone -> BIG;
    line1/line2 -> TEXT <l1>[|<l2>]; nothing left after sanitising ->
    CLEAR. Returns (lines, truncated)."""
    big, l1, l2 = req.get("big"), req.get("line1"), req.get("line2")
    if big is not None and (l1 is not None or l2 is not None):
        raise ValueError("big: cannot be combined with line1/line2")
    # The verb+screen prefix is formatted FIRST and the user text appended
    # afterwards: user text may legitimately contain '%' ("42%"), which must
    # never reach a %-format.
    if big is not None:
        text, truncated = sanitize_text(big)
        verb, tail = ("BIG", text) if text else ("CLEAR", None)
    else:
        t1, tr1 = sanitize_text(l1)
        t2, tr2 = sanitize_text(l2)
        truncated = tr1 or tr2
        if not t1 and not t2:
            verb, tail = "CLEAR", None
        elif t2:
            verb, tail = "TEXT", t1 + "|" + t2
        else:
            verb, tail = "TEXT", t1
    lines = []
    for t in tokens:
        lines.append(verb + " " + t if tail is None else verb + " " + t + " " + tail)
    return lines, truncated


def compose_sound(req, sounds=None):
    """`sound` -> (["TONE hz:ms,...[ VOL n]"], {"duration_ms"}). A name is
    resolved here — the board only ever sees notes. "off"/"hush" -> HUSH."""
    spec = req.get("sound")
    if spec is None:
        raise ValueError("sound: required (a name or hz:ms,...)")
    if isinstance(spec, str) and spec.strip().lower() in ("off", "hush", "stop"):
        return ["HUSH"], {"duration_ms": 0}
    notes = parse_notes(spec, sounds)
    vol = _int_arg("volume", req.get("volume"), 0, 4)
    line = "TONE " + notes_to_wire(notes) + ("" if vol is None else " VOL %d" % vol)
    return [line], {"duration_ms": sum(ms for _, ms in notes)}


def compose_show(req, colors=None, sounds=None, brightness=100):
    """`show` -> (lines, meta). OWN lines first (GLASS only with text, LED
    only with a color and not no_led), then the LED paint, the text, the
    sound; `for` seconds -> FOR <ms> on every OWN. The status LED for
    screen n is LED n; screen "all" claims LED GLASS. meta: for_ms,
    truncated."""
    scr = parse_screen_sel(req.get("screen"))
    has_text = any(req.get(k) is not None for k in ("big", "line1", "line2"))
    mode = req.get("mode")
    has_led = (req.get("color") is not None
               or (isinstance(mode, str) and mode.lower() == "off")) \
        and not req.get("no_led")
    has_sound = req.get("sound") is not None
    if not (has_text or has_led or has_sound):
        raise ValueError("nothing to show: give a color, text or sound")
    ms = parse_for(req.get("for"))
    suffix = " FOR %d" % ms if ms else ""
    leds = ["GLASS"] if scr == ["ALL"] else scr
    lines = []
    if has_text:
        lines += ["OWN GLASS %s%s" % (t, suffix) for t in scr]
    if has_led:
        lines += ["OWN LED %s%s" % (t, suffix) for t in leds]
        lines += compose_led_lines(leds, req, colors, brightness)
    truncated = False
    if has_text:
        tl, truncated = compose_text_lines(scr, req)
        lines += tl
    if has_sound:
        lines += compose_sound(req, sounds)[0]
    return lines, {"for_ms": ms, "truncated": truncated}


def compose_led(req, colors=None, brightness=100):
    """`led` -> (["OWN LED i[ FOR ms]", ..., "LED i ..."], {"for_ms"})."""
    sel = parse_led_sel(req.get("sel"))
    ms = parse_for(req.get("for"))
    suffix = " FOR %d" % ms if ms else ""
    lines = ["OWN LED %s%s" % (t, suffix) for t in sel]
    lines += compose_led_lines(sel, req, colors, brightness)
    return lines, {"for_ms": ms}


def compose_text(req):
    """`text` -> (["OWN GLASS g[ FOR ms]", ..., "TEXT g ..."], {"for_ms",
    "truncated"})."""
    scr = parse_screen_sel(req.get("screen"))
    ms = parse_for(req.get("for"))
    suffix = " FOR %d" % ms if ms else ""
    lines = ["OWN GLASS %s%s" % (t, suffix) for t in scr]
    tl, truncated = compose_text_lines(scr, req)
    return lines + tl, {"for_ms": ms, "truncated": truncated}


def parse_resources(v):
    """["led:1,ug2", "glass:all", "sound", "enc", "all"] -> wire resources
    ["LED 0", "LED 6", "GLASS ALL", "SOUND", "ENC", "ALL"]. A bare "led" /
    "glass" means all of that kind."""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)) or not v:
        raise ValueError("resources: want a list like [\"led:1\", \"glass:3\", \"sound\"]")
    out = []
    for r in v:
        if not isinstance(r, str):
            raise ValueError("bad resource %r" % (r,))
        kind, sep, sel = r.strip().lower().partition(":")
        if kind == "all" and not sep:
            toks = ["ALL"]
        elif kind in ("sound", "enc") and not sep:
            toks = [kind.upper()]
        elif kind == "led":
            toks = ["LED " + t for t in parse_led_sel(sel or "all")]
        elif kind == "glass":
            toks = ["GLASS " + t for t in parse_screen_sel(sel or "all")]
        else:
            raise ValueError("bad resource %r: want led:<sel>, glass:<sel>, "
                             "sound, enc or all" % r)
        for t in toks:
            if t not in out:
                out.append(t)
    return out


def compose_own(req):
    """`own` -> (["OWN <res>[ FOR ms]", ...], {"for_ms"})."""
    res = parse_resources(req.get("resources"))
    ms = parse_for(req.get("for"))
    suffix = " FOR %d" % ms if ms else ""
    return ["OWN %s%s" % (r, suffix) for r in res], {"for_ms": ms}


def compose_release(req):
    """`release` -> (["RELEASE <res>", ...], {}); no resources = ALL."""
    v = req.get("resources")
    res = ["ALL"] if not v else parse_resources(v)
    return ["RELEASE %s" % r for r in res], {}


def _setting_value(key, v, lo, hi):
    if isinstance(v, str):
        t = v.strip().lower()
        if key == "flip" and t in ("on", "true", "yes"):
            return 1
        if key == "flip" and t in ("off", "false", "no"):
            return 0
        v = t
    elif isinstance(v, bool):
        if key != "flip":
            raise ValueError("%s: want %d..%d, not %r" % (key, lo, hi, v))
        return int(v)
    return _int_arg(key, v, lo, hi)


def compose_settings(req):
    """`settings` -> (lines, {"op", "keys"}). get -> GET per key (all six
    without a key); set -> SET + a GET read-back; save -> SAVE."""
    op = req.get("op")
    if op not in ("get", "set", "save"):
        raise ValueError("op: want get, set or save")
    if op == "save":
        return ["SAVE"], {"op": "save", "keys": []}
    key = req.get("key")
    if key is not None:
        if not isinstance(key, str) or key.strip().lower() not in SETTINGS_KEYS:
            raise ValueError("key: want one of %s" % ", ".join(SETTINGS_KEYS))
        keys = [key.strip().lower()]
    elif op == "get":
        keys = list(SETTINGS_KEYS)
    else:
        raise ValueError("key: required for set")
    if op == "get":
        return ["GET %s" % k for k in keys], {"op": "get", "keys": keys}
    lo, hi = SETTINGS_KEYS[keys[0]]
    if "value" not in req:
        raise ValueError("value: required for set")
    n = _setting_value(keys[0], req.get("value"), lo, hi)
    return ["SET %s %d" % (keys[0], n), "GET %s" % keys[0]], {"op": "set", "keys": keys}


def check_raw_lines(lines):
    """Validate `raw` lines: non-empty printable ASCII, <= 1023 chars, at
    most 256 per request, and never DFU or REPLY (the update trigger and
    the ack mode belong to the daemon). Returns the stripped lines."""
    if isinstance(lines, str):
        lines = [lines]
    if not isinstance(lines, (list, tuple)) or not lines:
        raise ValueError("lines: want a non-empty list of strings")
    if len(lines) > RAW_MAX_LINES:
        raise ValueError("lines: %d (max %d per request)" % (len(lines), RAW_MAX_LINES))
    out, total = [], 0
    for i, line in enumerate(lines, 1):
        if not isinstance(line, str):
            raise ValueError("line %d: not a string" % i)
        t = line.strip()
        if not t:
            raise ValueError("line %d: empty" % i)
        if len(t) > LINE_MAX:
            raise ValueError("line %d: %d chars (max %d)" % (i, len(t), LINE_MAX))
        if any(not 32 <= ord(c) <= 126 for c in t):
            raise ValueError("line %d: printable ASCII only" % i)
        if t.split()[0].upper() in RAW_FORBIDDEN:
            raise ValueError("line %d: %s is not relayed (use `glowbug rescue` "
                             "for updates; replies stay off)" % (i, t.split()[0]))
        out.append(t)
        total += len(t) + 1
    if total > TX_REQUEST_MAX:
        raise ValueError("request too large (%d bytes, max %d)" % (total, TX_REQUEST_MAX))
    return out

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PermissionRequest",
               "PostToolUse", "Stop", "StopFailure", "SessionEnd"]

# Where each tool keeps its config. We only ever write into a directory that
# already exists — Glowbug never creates config for a tool you don't have.
CURSOR_HOOKS = os.path.join(HOME, ".cursor", "hooks.json")
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
CODEX_HOOKS = os.path.join(CODEX_HOME, "hooks.json")
CODEX_CONFIG = os.path.join(CODEX_HOME, "config.toml")
# Antigravity's global config dir has moved around; we write into whichever
# one already exists and never create one.
ANTIGRAVITY_DIRS = [os.path.join(HOME, ".gemini", "config"),
                    os.path.join(HOME, ".gemini", "antigravity-cli"),
                    os.path.join(HOME, ".gemini", "antigravity")]

# Which of each tool's events we subscribe to. ONLY observational ones: several
# tools let a hook veto the action it is reporting, and Glowbug must never be
# able to block your agent, so the "before/pre" families are deliberately
# absent. The cost is that "thinking" starts at the first tool call rather
# than at prompt submit.
CURSOR_EVENTS = ["sessionStart", "afterAgentThought", "postToolUse",
                 "afterShellExecution", "afterFileEdit", "afterAgentResponse",
                 "stop", "sessionEnd"]
# Codex runs these in the background (async), so they never sit in the way of
# a tool call. PermissionRequest is what lights the "waiting on you" screen.
CODEX_EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse",
                "PermissionRequest", "Stop", "SessionEnd"]
# Antigravity: only its "after the fact" events. PreToolUse/PreInvocation are
# decision points that can deny a tool call — we don't go near them.
ANTIGRAVITY_EVENTS = ["PostToolUse", "PostInvocation", "Stop"]

# Every source drives the same tiny state machine; only the event names differ.
# An event that isn't listed here still counts as activity (keeps the session
# "thinking"), so a tool adding new events can't break us.
EVENT_MAPS = {
    "cursor": {
        "permission": (),                    # no observational approval event
        "stop": ("stop",),
        "end": ("sessionEnd",),
        # A bare sessionStart may NOT create a session: Cursor fires one for
        # a freshly-opened chat pane that has no conversation yet (bench
        # 2026-08-16: sid "empty-state…", no further events, no sessionEnd —
        # a permanent ghost screen). A real session births on its first
        # actual agent event, seconds later.
        "no_birth": ("sessionStart",),
    },
    "codex": {
        "permission": ("PermissionRequest",),
        "stop": ("Stop", "agent-turn-complete"),   # hooks, and legacy notify
        "end": ("SessionEnd",),
    },
    "antigravity": {
        "permission": (),                    # no local approval event
        "stop": ("Stop",),                   # carries fullyIdle -> our "idle"
        "end": (),                           # no session-end event: TTL only
    },
}

ERRORISH = ("error", "failed", "failure", "crash")

# What proves each hook-only source's app is actually running, for the
# ghost-buster. Matched against `ps -axo args=` two ways: an .app-bundle
# substring (the IDE) or the exact basename of the command (its CLI).
# A source with NO entry here cannot be ghost-reaped (only the TTL saves
# it) — always add a probe when adding a source.
SOURCE_PROBES = {
    "cursor":      {"substr": ("Cursor.app",),      "basenames": ("cursor", "cursor-agent")},
    "codex":       {"substr": (),                    "basenames": ("codex",)},
    "antigravity": {"substr": ("Antigravity.app",),  "basenames": ("antigravity",)},
}


def running_sources():
    """The set of hook-only sources whose app/CLI is running right now.
    Returns None when the probe itself failed — callers must treat that as
    'no evidence either way' and reap nothing (fail open, never fabricate
    a death)."""
    try:
        out = subprocess.run(["ps", "-axo", "args="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        first = os.path.basename(line.split()[0])
        for src, probe in SOURCE_PROBES.items():
            if src not in seen and (first in probe["basenames"]
                                    or any(sub in line for sub in probe["substr"])):
                seen.add(src)
    return seen

# The hook script your coding agents run on session events — written to
# ~/.glowbug/glowbug-hook.py by `install`. Embedded here so the whole host
# software is genuinely ONE file (and pipx/uvx installs work). Read it: it is
# the entire trust surface, and it is one screen of code.
FORWARDER_SOURCE = '''#!/usr/bin/env python3
# glowbug-hook -- the entire trust surface.
#
# Your coding agent runs this on session events. It reads the tool's JSON from
# stdin, keeps ONLY the handful of fields Glowbug uses -- never prompt text,
# never tool arguments, never file contents -- and forwards them to the local
# Glowbug daemon over a unix socket. There is no network code here.
#
# It can never interfere with your agent: every failure path is swallowed, it
# gives up on the socket after 250ms, and it always exits 0 (some tools treat
# a non-zero exit as "block this action").
#
# Usage: glowbug-hook.py [--source NAME] [--event NAME] [--argv-json]
#        no arguments  ==  --source claude   (keeps older installs working)
import json
import os
import socket
import sys

SOCK_PATH = os.path.expanduser(
    "~/Library/Application Support/Glowbug/daemon.sock")

# canonical field  ->  the names the different tools use for it.
# Anything not listed here NEVER leaves this process: prompt text, tool
# arguments, file contents, transcript paths, model names, free-text errors.
ALIASES = (
    ("session_id",      ("session_id", "conversation_id", "conversationId",
                         "sessionId", "thread-id", "threadId")),
    ("session_title",   ("session_title", "title", "conversationTitle")),
    ("cwd",             ("cwd", "workspace_roots", "workspacePaths", "workspace_root")),
    ("tool_name",       ("tool_name", "toolName", "tool")),
    ("error_type",      ("error_type", "status", "terminationReason", "termination_reason")),
    ("hook_event_name", ("hook_event_name", "hookEventName", "eventName")),
)


def clean(v, limit):
    """One short, single-line string, or nothing at all."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    if not isinstance(v, str):
        return ""
    return " ".join(v.split())[:limit]


def normalize(ev, source, event):
    slim = {"source": source}
    for canon, keys in ALIASES:
        for k in keys:
            if k in ev:
                val = clean(ev[k], 64 if canon == "error_type" else 256)
                if val:
                    slim[canon] = val
                break
    if not slim.get("hook_event_name") and event:
        slim["hook_event_name"] = event
    if isinstance(ev.get("fullyIdle"), bool):
        slim["idle"] = ev["fullyIdle"]     # Antigravity's turn-is-over flag
    return slim


ARGS = sys.argv[1:]


def arg(flag, default=""):
    for i, a in enumerate(ARGS):
        if a == flag and i + 1 < len(ARGS):
            return ARGS[i + 1]
    return default


SOURCE = arg("--source", "claude")


def main():
    args = ARGS
    source, event = SOURCE, arg("--event")
    argv_json = "--argv-json" in args
    raw = b""
    if not argv_json:                      # --argv-json: stdin may be a tty
        raw = sys.stdin.buffer.read(65536)
    if not raw and args and args[-1].lstrip().startswith("{"):
        raw = args[-1].encode()            # some tools pass JSON as an argument
    if not raw:
        return
    ev = json.loads(raw.decode(errors="replace"))
    if not isinstance(ev, dict):
        return
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.25)
    s.connect(SOCK_PATH)
    s.sendall(json.dumps(normalize(ev, source, event)).encode())
    s.close()


try:
    main()
except BaseException:
    pass          # never, for any reason, interfere with the agent
if SOURCE != "claude":
    # An empty object means "no opinion" to the tools that read hook output.
    # Written even if main() blew up, so a bug in here can't look like a
    # malformed response. Claude Code gets silence, exactly as it always has.
    sys.stdout.write("{}")
sys.exit(0)
'''

# Old CoderDong install locations (pre-rename) — migrated away by `install`.
OLD_DIR = os.path.join(HOME, ".coderdong")
OLD_PLIST = os.path.join(HOME, "Library", "LaunchAgents", "com.pudtronics.coderdong.plist")


def hid_idle_s():
    """Seconds since the user last touched keyboard/mouse (system-wide via
    IOHIDSystem — resets on input at the login screen too). None if the
    ioreg read fails. Powers the WAKE signal: the board sleeps through a
    display-off/login-screen stretch (the USB bus never suspends there, so
    the firmware can't tell), and 'the human came back' is only visible
    host-side (user report 2026-08-16: board stayed dark after login until
    a hook event happened to fire)."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"],
                             capture_output=True, timeout=2).stdout.decode()
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        if m:
            return int(m.group(1)) / 1e9
    except Exception:
        pass
    return None


def log(msg):
    line = "%s %s\n" % (time.strftime("%H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        pass
    # under launchd, stderr IS the log file (StandardErrorPath) — writing
    # both doubled every line; stderr is only for a foreground terminal
    if sys.stderr.isatty():
        sys.stderr.write(line)


# ---------------------------------------------------------------- serial port
def find_port():
    """Find the Glowbug's serial port by USB product name via ioreg.

    Never 'the first /dev/cu.usbmodem*' — debug probes (ST-LINK & friends)
    also enumerate modem ports and the names are not distinguishable.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOSerialBSDClient", "-r", "-t", "-l", "-w0"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    product = None
    for line in out.splitlines():
        m = re.search(r'"USB Product Name" = "([^"]+)"', line)
        if m:
            product = m.group(1)
            continue
        m = re.search(r'"IOCalloutDevice" = "([^"]+)"', line)
        if m and product in ("Glowbug", "CoderDong"):
            return m.group(1)
    return None


def open_serial(port):
    """Open the Glowbug's CDC port raw and non-blocking. Raw = no line
    discipline at all (iflag/oflag/lflag cleared: no echo, no CR/LF
    translation, no signals), 8 data bits, modem lines ignored. Non-
    blocking because the daemon's serial thread must never stall on a
    board mid-blit, and `rescue` only fires one line into it. Raises
    OSError exactly like os.open — callers own the reconnect/give-up
    policy. Shared by serial_loop and rescue so they can never drift."""
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = attrs[1] = attrs[3] = 0          # raw
    attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


# ------------------------------------------------------------------- sessions
class Session:
    def __init__(self, sid, source="claude"):
        self.sid = sid
        self.source = source         # which coding agent this session belongs to
        self.key = (source, sid)     # session ids are only unique per source
        self.activity_at = time.time()   # last hook event of any kind
        self.created = time.time()   # stable order key (registry startedAt wins)
        self.name = ""
        self.cwd = ""
        self.is_subagent = False     # hidden from the device: a subagent
                                      # (entrypoint "sdk-cli" — Agent tool /
                                      # `claude -p`) or a background job /
                                      # bg-spare (kind "bg"). Tracked, but
                                      # never given a screen.
        self.busy = False            # registry status == "busy"
        self.reg_waiting = False     # registry status == "waiting" (dialog open)
        self.hook_state = "idle"     # idle | working | waiting | error
        self.waiting_at = 0.0        # when a hook last raised "waiting"
        self.done_at = 0.0           # when the agent last finished a turn —
                                     # drives the green DONE_S celebration
        self.detail = ""
        self.last_seen = time.time()
        self.alive = True
        self.born_at = time.time()   # arrival flutter window
        self.died_at = 0.0           # set when alive flips False

    def appear_at(self):
        """When this session may first occupy a screen (HOOK_APPEAR_S)."""
        return self.born_at + (0.0 if self.source == "claude"
                               else HOOK_APPEAR_S)

    def display_state(self):
        """Merge hook state machine + registry into a protocol-v2 state.
        Registry-only sessions (hooks not installed) still get thinking/idle.
        (No unread state — user simplification 2026-08-12: a finished session
        just goes dark.)"""
        if not self.alive:
            return "closing"         # device: red 1s, fade 1s, then off
        if self.hook_state == "error":
            return "error"
        # "Claude is waiting on you" — the REGISTRY is authoritative here.
        # No hook fires when the user dismisses a dialog or interrupts
        # (docs: Stop "doesn't fire on user interrupts"), but Claude Code's
        # session file flips status busy -> waiting -> idle, so escaping
        # clears within one poll. The hook still gives instant onset (and the
        # tool name) before the registry catches up. Bench-proven 2026-08-12.
        # question (AskUserQuestion dialog) vs permission (a gated tool) —
        # the PermissionRequest hook's tool_name is the discriminator.
        if self.reg_waiting or (self.hook_state == "waiting"
                                and time.time() - self.waiting_at < 3.0):
            if self.detail and self.detail != "AskUserQuestion":
                return "permission"
            return "question"
        if 0 <= time.time() - self.appear_at() < 1.5:
            return "arriving"        # device: firefly flutter-in + hello chirp
                                     # (anchored to when it APPEARS, so the
                                     # debounce doesn't swallow the flutter)
        if self.busy:
            return "thinking"
        # Sources without a session registry (everything but Claude Code) have
        # only their hooks to go on: a turn is "working" from the first event
        # until the tool says it stopped. reap_stale() is the safety net for a
        # session that dies without ever sending that stop event.
        if self.source != "claude" and self.hook_state == "working" \
                and time.time() - self.activity_at < WORK_STALE_S:
            return "thinking"
        if time.time() - self.done_at < DONE_S:
            return "done"            # green celebration — travels with the
                                     # session across ticker shifts
        return "idle"


def read_registry():
    """~/.claude/sessions/*.json — Claude Code's live session registry:
    names (incl. renames), busy/idle, cwd, pid liveness."""
    out = {}
    for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(p))
            pid = d.get("pid")
            os.kill(pid, 0)                       # process alive?
            sid = d["sessionId"]
            out[sid] = {
                "name": d.get("name") or os.path.basename(d.get("cwd", "")) or sid[:8],
                "cwd": d.get("cwd", ""),
                # Hidden-from-device sessions (tracked, never shown):
                #   entrypoint "sdk-cli" = launched via the Agent tool /
                #   `claude -p`, not a session the user opened by hand;
                #   kind "bg" = a background job or the pre-warmed
                #   `claude bg-spare` — no terminal window exists for
                #   these, so a screen would show a phantom agent
                #   (user directive 2026-08-18: background jobs
                #   shouldn't show). kind "interactive"/absent = a real
                #   session the user can see.
                "is_subagent": (d.get("entrypoint") == "sdk-cli"
                                or d.get("kind") == "bg"),
                "busy": d.get("status") == "busy",
                "waiting": d.get("status") == "waiting",
                "created": d.get("startedAt", 0) / 1000.0,   # ms epoch -> s
            }
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out


def read_cursor_titles(sids):
    """Chat names + archived flag from Cursor's composerHeaders table.

    Cursor hooks have no title field (confirmed against the payload:
    session_id / conversation_id only) and no close/archive event either.
    We look up sessions we already know about from hooks — never invent
    sessions from the DB, never read subtitle / transcript / anything but
    `name` and `isArchived`. Archiving a chat in Cursor is the user's
    "close" gesture, so it retires the session on the device
    (bench-verified 2026-08-16: isArchived flips true on archive).
    Returns {sid: (name, archived)}.
    """
    if not sids or not os.path.isfile(CURSOR_STATE_DB):
        return {}
    try:
        uri = "file:%s?mode=ro" % CURSOR_STATE_DB
        con = sqlite3.connect(uri, uri=True, timeout=0.2)
        try:
            q = "SELECT composerId, value FROM composerHeaders WHERE composerId IN (%s)" % (
                ",".join("?" * len(sids)))
            rows = con.execute(q, tuple(sids)).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError):
        return {}
    out = {}
    for cid, raw in rows:
        try:
            d = json.loads(raw)
            name = d.get("name") or ""
            archived = bool(d.get("isArchived"))
        except (ValueError, TypeError, AttributeError):
            continue
        if not isinstance(name, str):
            continue
        name = " ".join(name.split())[:256]
        if name:
            out[cid] = (name, archived)
    return out


class Daemon:
    def __init__(self):
        self.sessions = {}                 # (source, sid) -> Session
        self.board_slots = NUM_SLOTS       # effective window: 5 until the board
                                           # answers HELLO with PROTO>=3 SLOTS n
        self.slots = [None] * NUM_SLOTS    # first-come, stable (user spec)
        self.lock = threading.Lock()
        self.dirty = True
        self.last_event_at = {}            # source -> when we last heard from it
        self.app_seen_at = {}              # source -> ghost-buster last saw its app
        self.last_probe = 0.0              # last process-table scan
        self.last_wire_check = 0.0         # auto-wire timer (see maybe_wire)
        self.last_reg = {}                 # last Claude registry snapshot —
                                           # lets hook-births know a sid is a
                                           # subagent BEFORE the next poll
        self._persist_cache = None         # last sessions.json blob written
        self.pending_meta = {}             # (source, sid) -> cwd/title stashed
                                           # from a no_birth event (see
                                           # _hook_generic), used at birth
        # ---- the host API (socket api 2 / board PROTO 4). All of it lives
        # under self.lock; nothing below is ever held across socket or
        # serial I/O.
        self.board = None                  # {"online", "port", "fw", "proto",
                                           #  "slots"} once the board said HELLO
        self.board_info = {}               # EVT INFO pairs, keys lowercased
        self.owned = {"led": 0, "glass": 0, "sound": 0, "enc": 0}   # EVT OWN
        self.settings = {}                 # EVT SET key -> int (brightness...)
        self.port = None                   # serial port path while open
        self.tx_queue = collections.deque()  # byte blobs, one per request,
                                           # pulled by the serial thread
        self.tx_bytes = 0                  # bytes waiting in tx_queue
        self.subscribers = []              # live `events` streams
        self.waiters = collections.OrderedDict()   # ECHO token -> waiter
        self.confirm_lock = threading.Lock()  # one open barrier at a time,
                                           # so ERR attribution is exact
        self.cache = {}                    # replay cache (see _cache_lines)
        self.stop_event = threading.Event()   # socket_loop exit (tests)
        self.conn_sem = threading.BoundedSemaphore(MAX_CONNS)
        self._seq = 0                      # ECHO token counter
        self.colors, self.sounds, self.table_warnings = load_tables()
        self.load_sessions()               # reattach to agents that were live
                                           # when the previous daemon exited

    # ---- slot policy (user spec 2026-08-12): a chronological ticker.
    # Sessions line up left->right by start time; a NEW session appears at
    # the RIGHT; with more than 5, the row shifts LEFT (oldest falls off).
    def assign_slots(self):
        now = time.time()
        # dead sessions hold their slot ~2.2s so the device can play the
        # red "Closing..." farewell before the ticker compacts.
        # Subagents (Agent tool / `claude -p` verification runs) are tracked
        # but never get a screen — the user has no control over them, so
        # showing them is noise (user decision 2026-08-16, reversing the
        # earlier soft-white-pulse treatment).
        live = [s for s in self.sessions.values()
                if not s.is_subagent
                and ((s.alive and now >= s.appear_at())        # debounced in
                     or (not s.alive and s.died_at >= s.appear_at()
                         and now - s.died_at < 2.2))           # farewell only
                                                               # if ever shown
                and now - s.last_seen <= SESSION_STALE_S]
        live.sort(key=lambda s: (s.created, s.source, s.sid))
        self.slots = [s.key for s in live[-self.board_slots:]]
        while len(self.slots) < self.board_slots:
            self.slots.append(None)

    # ---- called on every poll tick: refresh Claude, retire the stale ----
    def tick(self):
        self.poll_claude_registry()
        self.poll_cursor_titles()
        self.reap_stale()
        self.maybe_wire()
        self.persist_sessions()

    # ---- hook-only sessions survive daemon restarts ----
    # Claude Code rebuilds from its live registry, but Cursor/Codex/
    # Antigravity exist only in this process's memory — so a daemon restart
    # (an UPDATE!) made a mid-turn agent invisible until its next hook event,
    # possibly forever if its turn ended during the restart window (user
    # report 2026-08-16). The session table is mirrored to disk and reloaded
    # at startup; the ghost-buster and idle/TTL timers then re-verify
    # everything restored, so a stale restore self-corrects in seconds.
    def persist_sessions(self):
        with self.lock:
            data = [{"source": s.source, "sid": s.sid, "name": s.name,
                     "cwd": s.cwd, "hook_state": s.hook_state,
                     "waiting_at": s.waiting_at, "detail": s.detail,
                     "created": s.created, "activity_at": s.activity_at,
                     "done_at": s.done_at}
                    for s in self.sessions.values()
                    if s.alive and s.source != "claude"]
        blob = json.dumps(data, sort_keys=True)
        if blob == self._persist_cache:
            return
        self._persist_cache = blob
        try:
            os.makedirs(os.path.dirname(SESS_STATE), exist_ok=True)
            tmp = SESS_STATE + ".tmp"
            with open(tmp, "w") as f:
                f.write(blob)
            os.replace(tmp, SESS_STATE)
        except OSError:
            pass                        # persistence is best-effort

    def load_sessions(self):
        try:
            saved = json.load(open(SESS_STATE))
        except (OSError, ValueError):
            return
        now = time.time()
        n = 0
        for d in saved:
            try:
                if d["source"] == "claude" or \
                        now - d["activity_at"] > HOOK_SESSION_TTL_S:
                    continue
                s = Session(d["sid"], d["source"])
                s.name = d.get("name") or d["sid"][:8]
                s.cwd = d.get("cwd", "")
                s.hook_state = d.get("hook_state", "idle")
                s.waiting_at = d.get("waiting_at", 0.0)
                s.done_at = d.get("done_at", 0.0)
                s.detail = d.get("detail", "")
                s.created = d.get("created", now)
                s.activity_at = s.last_seen = d["activity_at"]
                s.born_at = 0.0         # no arrival-flutter replay on restore
                self.sessions[s.key] = s
                n += 1
            except (KeyError, TypeError):
                continue
        if n:
            log("restored %d hook session(s) from %s" % (n, SESS_STATE))
            self.assign_slots()
            self.dirty = True

    def maybe_wire(self):
        """Install a coding agent after Glowbug and it connects itself.
        Only ever adds hooks to a tool that is actually installed, only once
        per tool (delete our hook and it stays deleted), and can never be
        fatal to the daemon."""
        now = time.time()
        if now - self.last_wire_check < AUTOWIRE_POLL_S:
            return
        self.last_wire_check = now
        try:
            for name, label, ok, added, note in wire_sources():
                if ok and added:
                    log("wired %s (%s) — new sessions will appear"
                        % (label, ", ".join(added)))
        except Exception as e:
            log("auto-wire skipped: %s" % e)

    # ---- Cursor: names only (hooks never include a title) ----
    def poll_cursor_titles(self):
        with self.lock:
            sids = [s.sid for s in self.sessions.values()
                    if s.source == "cursor" and s.alive]
        titles = read_cursor_titles(sids)
        if not titles:
            return
        with self.lock:
            changed = False
            for sid, (name, archived) in titles.items():
                s = self.sessions.get(("cursor", sid))
                if s is None:
                    continue
                if s.name != name:
                    s.name = name
                    changed = True
                if archived and s.alive:
                    # archiving IS Cursor's close gesture — red farewell,
                    # slot freed (user request 2026-08-16)
                    log("cursor: '%s' archived — closing" % s.name)
                    s.alive = False
                    s.died_at = time.time()
                    changed = True
            if changed:
                self.assign_slots()
                self.dirty = True

    # ---- Claude Code: live session registry ----
    def poll_claude_registry(self):
        reg = read_registry()
        with self.lock:
            self.last_reg = reg
            changed = False
            for sid, info in reg.items():
                s = self.sessions.get(("claude", sid))
                if s is None:
                    s = self.sessions[("claude", sid)] = Session(sid, "claude")
                    changed = True
                if (s.name, s.cwd, s.busy, s.reg_waiting, s.alive, s.is_subagent) != (
                        info["name"], info["cwd"], info["busy"], info["waiting"], True,
                        info["is_subagent"]):
                    changed = True
                # "agent got back": busy (or waiting-on-you) -> plain idle
                # starts the green celebration window
                if (s.busy or s.reg_waiting) and \
                        not info["busy"] and not info["waiting"]:
                    s.done_at = time.time()
                s.name, s.cwd, s.busy, s.alive = info["name"], info["cwd"], info["busy"], True
                s.reg_waiting = info["waiting"]
                s.is_subagent = info["is_subagent"]
                if s.hook_state == "waiting" and not info["waiting"] and \
                        time.time() - s.waiting_at >= 3.0:
                    s.hook_state = "idle"        # dialog gone: dismissed or answered
                    s.detail = ""
                    changed = True
                if info["created"]:
                    s.created = info["created"]
                s.last_seen = time.time()
            now2 = time.time()
            purge = []
            for key, s in self.sessions.items():
                # "gone from the registry" only means dead for Claude Code —
                # every other source is hook-driven and owns its own liveness
                # (reap_stale below). Without this guard, sessions from other
                # tools would be killed 1.5s after they appeared.
                if s.source == "claude" and s.sid not in reg and s.alive:
                    s.alive = False
                    s.died_at = now2
                    changed = True
                # keep pushing while any farewell/arrival window is open (so
                # transient states resolve without another event). This window
                # must comfortably outlast the 2.2s farewell slot-hold in
                # assign_slots AND the 1.5s poll spacing — at the old 3.0s,
                # a poll only landed in the (2.2, 3.0) gap about half the
                # time, so the ticker often didn't compact until some
                # unrelated state change forced a push (user report
                # 2026-08-16: agents stayed put after a middle one closed).
                if not s.alive and now2 - s.died_at < 6.0:
                    changed = True
                if s.alive and now2 - s.born_at < 5.0:
                    changed = True    # covers the HOOK_APPEAR_S debounce
                                      # crossing + the arrival flutter, so
                                      # both happen without another event
                if s.hook_state == "waiting" and now2 - s.waiting_at < 4.0:
                    changed = True        # keep pushing across the handoff
                if s.done_at and now2 - s.done_at < DONE_S + 3.0:
                    changed = True        # keep pushing through the green
                                          # window AND its expiry back to idle
                # and eventually forget the dead entirely
                if not s.alive and now2 - s.died_at > 30.0:
                    purge.append(key)
            for key in purge:
                del self.sessions[key]
            if changed:
                self.assign_slots()
                self.dirty = True

    # ---- hook-only sources: retire what has gone quiet ----
    def reap_stale(self):
        """A killed IDE never sends its stop event. Without this a dead
        session would hold a screen forever, thinking away. Two layers:
        the ghost-buster (app process gone -> sessions dead in ~10s) and
        the idle/TTL timers (app open but the session went quiet)."""
        now = time.time()
        changed = False
        # birth metadata that never got used (pane opened, never chatted)
        for k in [k for k, v in self.pending_meta.items()
                  if now - v["at"] > 600]:
            del self.pending_meta[k]
        if now - self.last_probe >= APP_PROBE_S:
            self.last_probe = now
            seen = running_sources()
            if seen is not None:
                for src in seen:
                    self.app_seen_at[src] = now
        with self.lock:
            for s in self.sessions.values():
                if s.source == "claude" or not s.alive:
                    continue           # Claude's liveness comes from the registry
                if s.source in SOURCE_PROBES:
                    # freshest proof-of-life: the probe saw the app, or a
                    # hook arrived (a hook can only come from a live app)
                    evidence = max(self.app_seen_at.get(s.source, 0.0),
                                   self.last_event_at.get(s.source, 0.0))
                    if evidence and now - evidence > APP_GONE_S:
                        log("reap: %s app gone — closing '%s'" % (s.source, s.name))
                        s.alive = False
                        s.died_at = now
                        changed = True
                        continue
                if s.hook_state == "working" and now - s.activity_at > WORK_STALE_S:
                    s.hook_state = "idle"
                    s.detail = ""
                    changed = True
                if now - s.activity_at > HOOK_SESSION_TTL_S:
                    s.alive = False
                    s.died_at = now
                    changed = True
            if changed:
                self.assign_slots()
                self.dirty = True

    # ---- hook events, from any source ----
    def handle_hook(self, ev):
        source = ev.get("source") or "claude"
        name = ev.get("hook_event_name", "")
        sid = ev.get("session_id", "")
        extras = ",".join(k for k in ("cwd", "session_title") if ev.get(k))
        log("hook[%s]: %s sid=%s tool=%s%s" % (
            source, name, sid[:8], ev.get("tool_name", "-"),
            " +" + extras if extras else ""))
        self.last_event_at[source] = time.time()
        if not sid:
            return
        if source == "claude":
            self._hook_claude(ev, name, sid)
        elif source in EVENT_MAPS:
            self._hook_generic(ev, source, name, sid)

    def _hook_generic(self, ev, source, name, sid):
        """Hook-only sources: no registry to consult, so the events ARE the
        state. Anything unrecognised counts as activity, never as an error."""
        m = EVENT_MAPS[source]
        # Placeholder ids are not sessions (ghost rule, user directive
        # 2026-08-16: never show agents that don't exist). Cursor's empty
        # chat pane announces itself as sid "empty-state…"; treat any
        # obviously-non-conversation id the same way, from any source.
        low = sid.lower()
        if low.startswith("empty") or low in ("unknown", "none", "null",
                                              "undefined", "new"):
            return
        now = time.time()
        with self.lock:
            s = self.sessions.get((source, sid))
            if s is None:
                if name in m.get("no_birth", ()):
                    # No session yet — but don't discard the metadata:
                    # Cursor puts workspace_roots on sessionStart while its
                    # activity events (which birth the session) may lack it,
                    # so this stash is the only way the slot gets a project
                    # name instead of the raw hex conversation id
                    # (bench 2026-08-16).
                    if ev.get("cwd") or ev.get("session_title"):
                        self.pending_meta[(source, sid)] = {
                            "cwd": ev.get("cwd", ""),
                            "title": ev.get("session_title", ""),
                            "at": now}
                    return           # birth only on real agent activity
                s = self.sessions[(source, sid)] = Session(sid, source)
                meta = self.pending_meta.pop((source, sid), {})
                s.cwd = ev.get("cwd") or meta.get("cwd", "")
                s.name = (ev.get("session_title") or meta.get("title")
                          or os.path.basename(s.cwd) or sid[:8])
            elif not s.alive and name not in m["end"]:
                s.alive = True          # it's back: flutter in again
                s.born_at = now
            if ev.get("session_title"):
                s.name = ev["session_title"]
            elif ev.get("cwd") and (not s.name or s.name == s.sid[:8]):
                # Cursor never sends a chat title (its hook payloads have no
                # title field at all — docs 2026-08-16), and workspace_roots
                # only rides along on SOME events; upgrade a hex-id name to
                # the project folder as soon as any event carries it.
                s.name = os.path.basename(ev["cwd"]) or s.name
            if ev.get("cwd") and not s.cwd:
                s.cwd = ev["cwd"]
            s.last_seen = s.activity_at = now

            err = ev.get("error_type", "")
            if name in m["end"]:
                s.alive = False
                s.died_at = now
            elif name in m["permission"]:
                s.hook_state = "waiting"
                s.waiting_at = now
                s.detail = ev.get("tool_name", "")
            elif name in m["stop"]:
                if ev.get("idle") is False:
                    s.hook_state = "working"        # turn isn't over yet
                elif any(w in err.lower() for w in ERRORISH):
                    s.hook_state = "error"
                    s.detail = err
                else:
                    if s.hook_state in ("working", "waiting"):
                        s.done_at = now         # turn over -> green celebration
                    s.hook_state = "idle"
                    s.detail = ""
            else:
                s.hook_state = "working"            # any activity = a live turn
                s.detail = ""
            self.assign_slots()
            self.dirty = True

    def _hook_claude(self, ev, name, sid):
        with self.lock:
            s = self.sessions.get(("claude", sid))
            if s is None:
                # Cross-reference against Claude's registry before believing
                # a claude-tagged hook (bench 2026-08-16): Cursor's Claude-
                # compat layer re-fires events through ~/.claude/settings.json
                # hooks with no --source tag, so a CURSOR conversation id
                # arrives labeled "claude", births a phantom session, and the
                # next registry poll kills it — a 1.5s red flash on the
                # device. Real Claude sessions always have a registry entry
                # (the poll even births them itself), so dropping unknown
                # sids costs nothing.
                if sid not in self.last_reg:
                    log("drop[claude]: sid=%s not in registry (compat echo?)"
                        % sid[:8])
                    return
                s = self.sessions[("claude", sid)] = Session(sid, "claude")
                s.name = ev.get("session_title") or os.path.basename(ev.get("cwd", "")) or sid[:8]
                s.cwd = ev.get("cwd", "")
                # a `claude -p` subagent's hooks can arrive before the next
                # registry poll — consult the last snapshot so it never
                # flashes onto a screen for the poll-lag window
                s.is_subagent = bool(
                    self.last_reg.get(sid, {}).get("is_subagent"))
            s.last_seen = s.activity_at = time.time()
            if name == "UserPromptSubmit":
                s.hook_state = "working"
            elif name == "PermissionRequest":
                s.hook_state = "waiting"
                s.waiting_at = time.time()
                s.detail = ev.get("tool_name", "")
            elif name == "PostToolUse":
                if s.hook_state == "waiting":
                    s.hook_state = "working"
            elif name == "Stop":
                s.hook_state = "idle"
                s.detail = ""
            elif name == "StopFailure":
                s.hook_state = "error"
                s.detail = ev.get("error_type", "")
            elif name == "SessionEnd":
                s.alive = False
                s.died_at = time.time()
            self.assign_slots()
            self.dirty = True

    # ---- board serial ----
    def push_state(self):
        """Build one full slot push as bytes. The serial loop queues it on
        its TX buffer and drains non-blockingly — a board mid-blit (30-45ms
        scroll frames on fw 1.4.0) backpressures the CDC pipe, and a
        blocking write here would stall PING/registry polling."""
        debug = os.environ.get("GLOWBUG_DEBUG_SLOTS")
        out = []
        with self.lock:
            for i in range(len(self.slots)):
                key = self.slots[i]
                s = self.sessions.get(key) if key else None
                if s:
                    st = s.display_state()
                    detail = s.detail if st in ("permission", "error") else ""
                    # SID = which session occupies the slot, so firmware can
                    # tell a ticker shift (different agent moved in) from a
                    # state change of the same agent and skip transition
                    # effects (Done! ding, chimes) on shifts.
                    sid8 = (s.sid or "-").replace(" ", "")[:8] or "-"
                    line = "SLOT %d STATE %s NAME %s DETAIL %s SUB %d SID %s" % (
                        i + 1, st, s.name[:21], detail[:21],
                        1 if s.is_subagent else 0, sid8)
                else:
                    line = "SLOT %d STATE idle NAME - DETAIL  SUB 0 SID -" % (i + 1)
                if debug:
                    log("slot: %s" % line)   # bench testing without a device
                out.append(line)
            self.dirty = False
        return ("\n".join(out) + "\n").encode() if out else b""

    def report(self):
        """What `glowbug status` / `doctor` ask the daemon over the socket."""
        now = time.time()
        with self.lock:
            sess = [{"source": s.source, "name": s.name,
                     "state": s.display_state()}
                    for k in self.slots if k for s in [self.sessions.get(k)] if s]
        return {"version": VERSION, "sessions": sess,
                "last_event_at": {k: round(now - v, 1)
                                  for k, v in self.last_event_at.items()}}

    # ---- board -> host lines: a dispatcher (PROTO 4) ----
    # Only EVT HELLO touches the agent display (the slot window). Every
    # other line becomes an event for `glowbug events` subscribers and
    # updates the daemon's picture of the board (INFO, OWN, SET), resolves
    # an ECHO barrier, or is passed through untouched — a board newer than
    # this daemon is never an error.
    _EVT_HANDLERS = {"HELLO": "_evt_hello", "INFO": "_evt_info", "ENC": "_evt_enc",
                     "CLICK": "_evt_click", "HOLD": "_evt_hold", "MENU": "_evt_menu",
                     "OWN": "_evt_own", "SET": "_evt_set", "ECHO": "_evt_echo"}

    def handle_board_line(self, line):
        text = line.strip()
        parts = text.split()
        if not parts:
            return
        if parts[0] == "EVT":
            kind = parts[1] if len(parts) > 1 else ""
            handler = self._EVT_HANDLERS.get(kind)
            if handler is None:
                self.emit({"event": "evt", "raw": text})
            else:
                getattr(self, handler)(parts[2:], text)
        elif parts[0] == "ERR":
            self._on_err(parts, text)
        elif parts[0] == "OK":
            return                       # REPLY is never turned on
        elif os.environ.get("GLOWBUG_DEBUG"):
            log("board: %s" % text)

    def _evt_hello(self, args, text):
        parts = ["EVT", "HELLO"] + args
        log("board: hello %s" % " ".join(args))
        # Protocol v3 negotiation: "EVT HELLO <fw> PROTO 3 SLOTS 32".
        # PROTO<3 (or unparseable) keeps the safe 5-slot window — an old
        # board drops SLOT n>5 silently, which would otherwise leave it
        # showing the five OLDEST agents.
        slots = NUM_SLOTS
        try:
            if "PROTO" in parts and int(parts[parts.index("PROTO") + 1]) >= 3 \
                    and "SLOTS" in parts:
                n = int(parts[parts.index("SLOTS") + 1])
                slots = max(NUM_SLOTS, min(BOARD_SLOTS_MAX, n))
        except (ValueError, IndexError):
            slots = NUM_SLOTS
        proto = 0
        try:
            if "PROTO" in parts:
                proto = int(parts[parts.index("PROTO") + 1])
        except (ValueError, IndexError):
            proto = 0
        fw = args[0] if args and args[0] not in ("PROTO", "SLOTS") else "?"
        with self.lock:
            if slots != self.board_slots:
                log("board: slot window %d -> %d" % (self.board_slots, slots))
                self.board_slots = slots
            self.assign_slots()
            self.board = {"online": True, "port": self.port, "fw": fw,
                          "proto": proto, "slots": slots}
            # HELLO is the board's session reset (RELEASE ALL + REPLY OFF)
            self.owned = {"led": 0, "glass": 0, "sound": 0, "enc": 0}
            if proto >= PROTOCOL_MIN:
                # capabilities + the brightness `show` scales colors by
                try:
                    self._enqueue(b"INFO\nGET brightness\n")
                except GlowbugError as e:
                    log("board: INFO not queued: %s" % e)
        self.dirty = True
        self.emit({"event": "hello", "fw": fw, "proto": proto, "slots": slots})
        self.emit({"event": "board", "online": True, "fw": fw, "proto": proto})
        if proto >= PROTOCOL_MIN:
            self._replay("hello")        # the board forgot everything

    def _evt_info(self, args, text):
        info = {}
        for i in range(0, len(args) - 1, 2):
            v = args[i + 1]
            info[args[i].lower()] = int(v) if re.fullmatch(r"-?\d+", v) else v
        with self.lock:
            self.board_info = info
            known = self.board is not None
        if known:
            log("board: %s" % text)
        self.emit({"event": "info", "info": info})

    def _evt_enc(self, args, text):
        try:
            delta = int(args[0])
        except (IndexError, ValueError):
            self.emit({"event": "evt", "raw": text})
            return
        self.emit({"event": "enc", "delta": delta})

    def _evt_click(self, args, text):
        self.emit({"event": "click"})

    def _evt_hold(self, args, text):
        self.emit({"event": "hold"})

    def _evt_menu(self, args, text):
        opened = bool(args) and args[0] == "1"
        self.emit({"event": "menu", "open": opened})
        if not opened:
            # the menu borrowed every glass; owned LED animators come back
            # by themselves, owned glasses are ours to repaint — and the
            # user may just have changed the brightness setting
            with self.lock:
                try:
                    self._enqueue(b"GET brightness\n")
                except GlowbugError:
                    pass
            self._replay("menu")

    def _evt_own(self, args, text):
        owned = {}
        for i in range(0, len(args) - 1, 2):
            k, v = args[i].lower(), args[i + 1]
            try:
                owned[k] = int(v, 16) if k in ("led", "glass") else int(v)
            except ValueError:
                self.emit({"event": "evt", "raw": text})
                return
        now = time.time()
        with self.lock:
            for k in self.owned:
                if k in owned:
                    self.owned[k] = owned[k]
            self._cache_prune(now)
        self.emit(dict(self._owned_view(), event="own"))

    def _evt_set(self, args, text):
        if len(args) < 2:
            self.emit({"event": "evt", "raw": text})
            return
        key, v = args[0].lower(), args[1]
        val = int(v) if re.fullmatch(r"-?\d+", v) else v
        with self.lock:
            self.settings[key] = val
        self.emit({"event": "set", "key": key, "value": val})

    def _evt_echo(self, args, text):
        with self.lock:
            w = self.waiters.get(args[0]) if args else None
            if w is not None:
                w["event"].set()
        if w is None:                        # not ours: somebody's raw ECHO
            self.emit({"event": "evt", "raw": text})

    def _on_err(self, parts, text):
        with self.lock:
            known = self.board is not None
            for w in self.waiters.values():   # the oldest open barrier owns it
                w["errs"].append(text)
                break
        if known:
            log("board: %s" % text)
        self.emit({"event": "err", "verb": parts[1] if len(parts) > 1 else "",
                   "text": text})

    def _board_gone(self):
        """close_fd: the port is gone. Forget the board (API -> no_board
        until the next HELLO), drop queued API traffic (a reopened port
        re-negotiates first), fail any open barrier."""
        with self.lock:
            was = self.board is not None
            self.board = None
            self.port = None
            self.board_info = {}
            self.owned = {"led": 0, "glass": 0, "sound": 0, "enc": 0}
            self.tx_queue.clear()
            self.tx_bytes = 0
            for w in self.waiters.values():
                w["gone"] = True
                w["event"].set()
        if was:
            self.emit({"event": "board", "online": False})

    # ---- events: fan-out to `glowbug events` streams ----
    def subscribe(self, kinds=None):
        """A new subscriber (its queue is drained by a socket thread), or
        None when MAX_SUBSCRIBERS streams are already open."""
        sub = _Subscriber(kinds)
        with self.lock:
            if len(self.subscribers) >= MAX_SUBSCRIBERS:
                return None
            self.subscribers.append(sub)
        return sub

    def unsubscribe(self, sub):
        with self.lock:
            if sub in self.subscribers:
                self.subscribers.remove(sub)

    def emit(self, ev):
        """Fan one event dict out. A reader that lets SUB_QUEUE events pile
        up is dropped (its stream ends with a `busy` line) — the serial
        thread must never wait on a socket."""
        with self.lock:
            subs = list(self.subscribers)
        for sub in subs:
            if sub.kinds is not None and ev.get("event") not in sub.kinds:
                continue
            try:
                sub.q.put_nowait(ev)
            except queue.Full:
                sub.dropped = True
                self.unsubscribe(sub)

    # ---- host -> board: the TX hand-off ----
    # Connection threads never touch the port. They append byte blobs to
    # tx_queue (under self.lock); the serial thread pulls them behind its
    # own agent-display push while its unsent tail is short, and tx_drain
    # is the one place os.write happens.
    def _enqueue(self, blob):
        """Caller holds self.lock. Raises busy at the queue caps."""
        if len(self.tx_queue) >= TX_API_MAX_ENTRIES \
                or self.tx_bytes + len(blob) > TX_API_MAX_BYTES:
            raise GlowbugError("busy", "the board is not keeping up (%d bytes "
                               "queued) — try again in a moment" % self.tx_bytes)
        self.tx_queue.append(blob)
        self.tx_bytes += len(blob)

    def _tx_take(self, txlen):
        """Serial thread: queued blobs to append to its TX buffer, whole
        blobs only, while the buffer is under TX_API_LOWWATER."""
        out = []
        with self.lock:
            while self.tx_queue and txlen < TX_API_LOWWATER:
                blob = self.tx_queue.popleft()
                self.tx_bytes -= len(blob)
                out.append(blob)
                txlen += len(blob)
        return b"".join(out)

    def _send_lines(self, lines):
        """Queue wire lines as one contiguous blob (a `show` burst never
        interleaves with another client's) and remember them for replay.
        Raises busy / no_board."""
        blob = ("\n".join(lines) + "\n").encode()
        now = time.time()
        with self.lock:
            if self.board is None:
                raise GlowbugError("no_board", "the board went away")
            self._enqueue(blob)
            self._cache_lines(lines, now)

    def _barrier(self, lines, timeout=CONFIRM_TIMEOUT_S):
        """Queue `lines` + `ECHO <token>` and wait for the board to echo the
        token: everything before it has then been parsed. Returns the ERR
        lines the board emitted meanwhile (attributed to this barrier —
        confirm_lock keeps one open at a time). Raises busy / no_board /
        timeout."""
        with self.confirm_lock:
            self._seq += 1
            token = "h%x" % self._seq
            w = {"event": threading.Event(), "errs": [], "gone": False}
            now = time.time()
            with self.lock:
                if self.board is None:
                    raise GlowbugError("no_board", "the board went away")
                self._enqueue(("\n".join(lines + ["ECHO " + token]) + "\n").encode())
                self._cache_lines(lines, now)
                self.waiters[token] = w
            done = w["event"].wait(timeout)
            with self.lock:
                self.waiters.pop(token, None)
            if w["gone"]:
                raise GlowbugError("no_board", "the board went away")
            if not done:
                raise GlowbugError("timeout", "no ECHO from the board within %.1f s"
                                   % timeout)
            return w["errs"]

    # ---- replay cache ----
    # The board has no framebuffer and forgets everything on re-enumeration
    # (its own EVT HELLO) and repaints every glass after its device menu
    # (EVT MENU 0). So the daemon keeps, per LED / glass selector, the last
    # OWN line (with its deadline) and the last paint lines it relayed, and
    # re-sends them: OWN + LED + text after a HELLO, glass content after the
    # menu. Entries die at their FOR deadline, on RELEASE, and when an EVT
    # OWN shows the board no longer owns them.
    def _cache_entry(self, key, now):
        e = self.cache.get(key)
        if e is None:
            e = self.cache[key] = {"own": None, "deadline": None, "content": [],
                                   "at": now}
        return e

    def _cache_lines(self, lines, now):
        """Caller holds self.lock. Parses relayed lines just enough to key
        them: OWN / RELEASE / LED / TEXT / BIG / CLEAR / BLIT."""
        for line in lines:
            p = line.split()
            if len(p) < 2:
                continue
            verb = p[0]
            if verb == "OWN":
                ms = None
                if len(p) >= 4 and p[-2] == "FOR" and p[-1].isdigit():
                    ms = int(p[-1])
                if p[1] in ("LED", "GLASS") and len(p) >= 3:
                    keys = [(p[1].lower(), p[2])]
                elif p[1] == "ALL":
                    keys = [("led", "ALL"), ("glass", "ALL")]
                else:
                    continue
                for key in keys:
                    e = self._cache_entry(key, now)
                    e["own"] = "OWN %s %s" % (key[0].upper(), key[1])
                    e["deadline"] = now + ms / 1000.0 if ms else None
                    e["at"] = now
            elif verb == "RELEASE":
                if p[1] == "ALL":
                    self.cache.clear()
                elif p[1] in ("LED", "GLASS") and len(p) >= 3:
                    self._cache_drop(p[1].lower(), p[2])
            elif verb == "LED" and len(p) >= 3:
                e = self._cache_entry(("led", p[1]), now)
                e["content"] = [line]
                e["at"] = now
            elif verb in ("TEXT", "BIG", "CLEAR"):
                e = self._cache_entry(("glass", p[1]), now)
                e["content"] = [line]
                e["at"] = now
            elif verb == "BLIT" and len(p) >= 4:
                e = self._cache_entry(("glass", p[1]), now)
                if p[2] == "ALL":
                    e["content"] = [line]
                else:
                    e["content"] = [c for c in e["content"]
                                    if not (c.startswith("BLIT ")
                                            and c.split()[2] == p[2])] + [line]
                e["at"] = now

    def _cache_drop(self, kind, sel):
        """Caller holds self.lock. Drop every entry of `kind` that shares an
        index with `sel` (releasing LED 2 also voids an `ALL` entry)."""
        idx = sel_indices(kind, sel)
        if not idx:
            return                       # unparsable: the board releases nothing
        for key in list(self.cache):
            if key[0] == kind and sel_indices(*key) & idx:
                del self.cache[key]

    def _cache_prune(self, now):
        """Caller holds self.lock, after an EVT OWN: drop expired entries
        and entries the board no longer owns — except ones asserted within
        CACHE_GRACE_S, whose EVT OWN is still in flight."""
        for key, e in list(self.cache.items()):
            if e["deadline"] is not None and e["deadline"] <= now:
                del self.cache[key]
            elif now - e["at"] >= CACHE_GRACE_S:
                idx = sel_indices(*key)
                mask = self.owned.get(key[0], 0)
                if not idx or any(not (mask >> i) & 1 for i in idx):
                    del self.cache[key]

    def _replay(self, reason):
        """Re-send cached state: after "hello" every unexpired OWN (with the
        remaining FOR), LED and glass line; after "menu" glass content only.
        Then a synthetic `redraw` event so animating apps resend their
        frame. Returns the lines queued."""
        now = time.time()
        owns, content = [], []
        with self.lock:
            for key, e in list(self.cache.items()):
                if e["deadline"] is not None and e["deadline"] <= now:
                    del self.cache[key]
                    continue
                if reason == "hello":
                    if e["own"]:
                        line = e["own"]
                        if e["deadline"] is not None:
                            line += " FOR %d" % max(1, int(round((e["deadline"] - now) * 1000)))
                        owns.append(line)
                        e["at"] = now
                    content += e["content"]
                elif key[0] == "glass":
                    content += e["content"]
            lines = owns + content
            if lines:
                try:
                    self._enqueue(("\n".join(lines) + "\n").encode())
                except GlowbugError as err:
                    log("replay (%s) skipped: %s" % (reason, err))
                    lines = []
        self.emit({"event": "redraw", "reason": reason, "lines": len(lines)})
        return lines

    # ---- the socket API: one request dict -> one reply dict ----
    COMMANDS = ("report", "info", "show", "led", "text", "sound", "raw", "own",
                "release", "settings", "palette", "events")

    def handle_request(self, msg):
        """Never raises. `events` is the one streaming command and is
        served by the connection thread instead (it needs the socket)."""
        if not isinstance(msg, dict):
            return _fail("bad_request", "want a JSON object")
        api = msg.get("api", SOCKET_API)
        if isinstance(api, bool) or not isinstance(api, int) or not 1 <= api <= SOCKET_API:
            return _fail("bad_request", "api %r: this daemon speaks api %d"
                         % (api, SOCKET_API))
        cmd = msg.get("cmd")
        if not isinstance(cmd, str) or cmd not in self.COMMANDS:
            return _fail("unknown_cmd", "unknown cmd %r (glowbug --help lists them)" % (cmd,))
        if cmd == "events":
            return _fail("bad_request", "events streams: keep the socket open")
        try:
            return getattr(self, "cmd_" + cmd)(msg)
        except GlowbugError as e:
            return _fail(e.code, e.message)
        except ValueError as e:
            return _fail("bad_arg", str(e))
        except Exception as e:                # a bug must not kill the daemon
            log("api: %s crashed: %s: %s\n%s" % (cmd, type(e).__name__, e,
                                                  traceback.format_exc()))
            return _fail("bad_request", "internal error in %s: %s" % (cmd, e))

    def _require_board(self, min_proto=PROTOCOL_MIN):
        with self.lock:
            b = self.board
        if b is None:
            raise GlowbugError("no_board", "no Glowbug connected — is it plugged in? "
                               "(glowbug status)")
        if b["proto"] < min_proto:
            raise GlowbugError("proto_too_old",
                               "board firmware %s speaks PROTO %d; the API needs "
                               "PROTO %d — run `glowbug rescue` to flash the "
                               "bundled image" % (b["fw"], b["proto"], min_proto))
        return b

    def _board_view(self):
        with self.lock:
            if self.board is None:
                return {"online": False}
            v = dict(self.board_info)
            v.update(self.board)
            return v

    def _owned_view(self):
        with self.lock:
            o = dict(self.owned)
        return {"led": [led_name(i) for i in range(NUM_LEDS) if (o["led"] >> i) & 1],
                "glass": [i + 1 for i in range(NUM_GLASS) if (o["glass"] >> i) & 1],
                "sound": bool(o["sound"]), "enc": bool(o["enc"])}

    def _brightness(self):
        with self.lock:
            b = self.settings.get("brightness", 100)
        return b if isinstance(b, int) and 0 <= b <= 100 else 100

    def _held(self, meta):
        ms = meta.get("for_ms")
        return time.time() + ms / 1000.0 if ms else None

    def reload_tables(self):
        colors, sounds, warnings = load_tables()
        with self.lock:
            self.colors, self.sounds, self.table_warnings = colors, sounds, warnings
        return warnings

    def cmd_report(self, msg):
        r = self.report()                    # the 1.5.0 keys, untouched
        r.update(_ok(board=self._board_view()))
        return r

    def cmd_info(self, msg):
        with self.lock:
            b = self.board
        if msg.get("fresh") and b is not None and b["proto"] >= PROTOCOL_MIN:
            self._barrier(["INFO"])
        with self.lock:
            settings = dict(self.settings)
            counts = {"colors": len(self.colors), "sounds": len(self.sounds)}
        return _ok(daemon=VERSION, socket=SOCK_PATH, board=self._board_view(),
                   owned=self._owned_view(), settings=settings, palette=counts)

    def cmd_show(self, msg):
        self._require_board()
        lines, meta = compose_show(msg, self.colors, self.sounds, self._brightness())
        self._send_lines(lines)
        return _ok(lines=lines, held_until=self._held(meta), truncated=meta["truncated"])

    def cmd_led(self, msg):
        self._require_board()
        lines, meta = compose_led(msg, self.colors, self._brightness())
        self._send_lines(lines)
        return _ok(lines=lines, held_until=self._held(meta))

    def cmd_text(self, msg):
        self._require_board()
        lines, meta = compose_text(msg)
        self._send_lines(lines)
        return _ok(lines=lines, held_until=self._held(meta), truncated=meta["truncated"])

    def cmd_sound(self, msg):
        self._require_board()
        lines, meta = compose_sound(msg, self.sounds)
        self._send_lines(lines)
        return _ok(lines=lines, duration_ms=meta["duration_ms"])

    def cmd_own(self, msg):
        self._require_board()
        lines, meta = compose_own(msg)
        self._send_lines(lines)
        return _ok(lines=lines, held_until=self._held(meta))

    def cmd_release(self, msg):
        self._require_board()
        lines, _ = compose_release(msg)
        self._send_lines(lines)
        return _ok(lines=lines)

    def cmd_raw(self, msg):
        self._require_board()
        lines = check_raw_lines(msg["lines"] if "lines" in msg else msg.get("line"))
        if msg.get("confirm"):
            errs = self._barrier(lines)
            if errs:
                return _fail("board_err", "; ".join(errs), lines=lines, errors=errs)
            return _ok(lines=lines, queued=len(lines), confirmed=True)
        self._send_lines(lines)
        return _ok(lines=lines, queued=len(lines))

    def cmd_settings(self, msg):
        self._require_board()
        lines, meta = compose_settings(msg)
        errs = self._barrier(lines)
        if errs:
            return _fail("board_err", "; ".join(errs), errors=errs)
        if meta["op"] == "save":
            return _ok(saved=True)
        with self.lock:
            vals = {k: self.settings[k] for k in meta["keys"] if k in self.settings}
        missing = [k for k in meta["keys"] if k not in vals]
        if missing:
            return _fail("board_err", "the board sent no EVT SET for %s" % ", ".join(missing))
        return _ok(settings=vals)

    def cmd_palette(self, msg):
        if msg.get("reload"):
            self.reload_tables()
        with self.lock:
            colors, sounds, warnings = dict(self.colors), self.sounds, list(self.table_warnings)
        return _ok(colors=colors,
                   sounds={k: [[hz, ms] for hz, ms in v] for k, v in sounds.items()},
                   files={"palette": PALETTE_PATH, "sounds": SOUNDS_PATH},
                   warnings=warnings)

    def serial_loop(self):
        # macOS sleep/wake gotcha (found 2026-08-16): after a lid-close the
        # CDC port re-enumerates while the OLD fd is left silently dead —
        # os.write()/os.read() on it don't raise, they just go nowhere, so
        # the `except OSError` reconnect below never fires and the board
        # sits in "Offline" until a manual replug. Worse (bench-observed
        # same day): the port usually comes back under the SAME /dev path,
        # so comparing paths alone doesn't catch it either. Three
        # detections, all needed:
        #   1. path change  — find_port() re-run every PORT_RECHECK_S,
        #      reconnect when it disagrees with the fd we hold;
        #   2. node identity — same path, but the device node was torn down
        #      and recreated: its inode changes, so os.stat(port) vs
        #      os.fstat(fd) disagree;
        #   3. time jump    — this loop runs every 50ms; an iteration gap
        #      >5s means the Mac slept OR this process was starved of CPU
        #      (bench 2026-08-18: a KeyShot render at load ~180 starved
        #      this thread 5-13s at a time, over and over). A gap alone
        #      does NOT invalidate the fd, so never reconnect on it
        #      unconditionally — the old behavior turned every starvation
        #      gap into a port drop, flapping the board between "Offline"
        #      and the agents for the length of the render. Instead a gap
        #      forces detections 1+2 to run THIS iteration (they are what
        #      actually catch a slept-through re-enumeration) and forces
        #      an immediate PING + full state re-push so a board that hit
        #      its own no-PING timeout recovers right away.
        PORT_RECHECK_S = 2.0
        SLEEP_GAP_S = 5.0
        buf = b""
        txbuf = b""                  # unsent tail (non-blocking writes can
                                     # short-write while the board is mid-blit)
        fd = None
        port = None
        last_ping = 0.0
        last_poll = 0.0
        last_hid = 0.0
        prev_idle = None             # last HID idle reading (user-presence edge)
        last_recheck = 0.0
        last_loop = 0.0

        def close_fd():
            nonlocal fd, buf, txbuf
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            fd = None
            buf = b""
            txbuf = b""
            # a board we can no longer see must be re-negotiated on reconnect
            with self.lock:
                if self.board_slots != NUM_SLOTS:
                    log("serial: window back to %d until next HELLO" % NUM_SLOTS)
                    self.board_slots = NUM_SLOTS
                    self.assign_slots()
            self._board_gone()          # API: no_board, queue dropped, event

        def tx_drain():
            # Drain as much of txbuf as the pipe accepts; never block. A
            # persistent OSError propagates to the caller's reconnect path.
            nonlocal txbuf
            while txbuf:
                try:
                    n = os.write(fd, txbuf)
                except BlockingIOError:
                    return
                if n <= 0:
                    return
                txbuf = txbuf[n:]

        while True:
            now = time.time()
            if last_loop and now - last_loop > SLEEP_GAP_S:
                gap = now - last_loop
                if gap > 60.0:
                    # a minute+ gap is a real sleep, not scheduler starvation
                    # (worst starvation ever benched: 13s at load ~180).
                    # Reconnect unconditionally — insurance for the one case
                    # detections 1+2 can't see: a dead fd whose node was
                    # never torn down. One reconnect per wake is harmless.
                    log("serial: %.0fs gap (slept), reconnecting" % gap)
                    close_fd()
                else:
                    log("serial: %.0fs gap (sleep or CPU starvation) — verifying port"
                        % gap)
                last_recheck = 0.0          # run detections 1+2 right now
                last_ping = 0.0             # PING immediately, not next second
                self.dirty = True           # re-push: un-"Offline" the board
            last_loop = now
            if now - last_recheck >= PORT_RECHECK_S:
                last_recheck = now
                current = find_port()
                if fd is not None and current != port:
                    log("serial: port changed (%s -> %s), reconnecting" % (port, current))
                    close_fd()
                elif fd is not None and current is not None:
                    try:                    # same path — but same NODE?
                        if os.stat(current).st_ino != os.fstat(fd).st_ino:
                            log("serial: device node recreated, reconnecting")
                            close_fd()
                    except OSError:
                        close_fd()
                port = current

            if fd is None:
                if port is None:
                    time.sleep(0.2)
                    # keep session state fresh even while unplugged
                    if time.time() - last_poll >= REGISTRY_POLL_S:
                        self.tick()
                        last_poll = time.time()
                    continue
                try:
                    fd = open_serial(port)
                    self.port = port
                    log("serial: opened %s" % port)
                    # Solicit the board's capabilities: its own EVT HELLO
                    # fires only at USB enumeration, which a (re)started
                    # daemon has usually missed. New fw answers; old fw
                    # ignores the line and the window stays at 5.
                    txbuf += b"HELLO\n"
                    self.dirty = True
                except OSError:
                    fd = None
                    time.sleep(0.2)
                    continue
            try:
                if now - last_poll >= REGISTRY_POLL_S:
                    self.tick()
                    last_poll = now
                if now - last_ping >= PING_INTERVAL_S:
                    txbuf += b"PING\n"
                    last_ping = now
                if now - last_hid >= HID_POLL_S:
                    last_hid = now
                    idle = hid_idle_s()
                    if idle is not None:
                        # falling edge after a long absence = the user is back
                        # (mouse jiggle / login keystroke): wake the board
                        if prev_idle is not None and prev_idle > AWAY_S \
                                and idle < HID_POLL_S + 1:
                            txbuf += b"WAKE\n"
                            log("user returned (idle %.0fs -> %.0fs): WAKE"
                                % (prev_idle, idle))
                        prev_idle = idle
                if self.dirty:
                    txbuf += self.push_state()
                # API traffic rides behind the display push, whole blobs,
                # only while the unsent tail is short (board mid-blit)
                txbuf += self._tx_take(len(txbuf))
                if len(txbuf) > 65536:
                    # pipe dead-but-undetected: drop the backlog whole (never
                    # mid-line) and re-push once it drains again
                    log("serial: TX backlog dropped (%dB unsent)" % len(txbuf))
                    txbuf = b""
                    self.dirty = True
                tx_drain()
                try:
                    chunk = os.read(fd, 256)
                    if chunk:
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self.handle_board_line(line.decode(errors="replace"))
                except BlockingIOError:
                    pass
                time.sleep(0.05)
            except OSError:
                log("serial: lost connection, rescanning")
                close_fd()

    # ---- the unix socket: hooks in, API requests in, replies/events out ----
    # Framing: a client sends ONE JSON object ended by "\n" or by closing
    # its write side (the hook forwarder and 1.5.0 `ask_daemon` do the
    # latter); the reply is one JSON line, then the connection closes —
    # except `events`, which keeps streaming one JSON line per event. A
    # hook payload is recognised by the ABSENCE of "cmd".
    def socket_loop(self):
        os.makedirs(os.path.dirname(SOCK_PATH), exist_ok=True)
        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        srv.listen(MAX_CONNS)
        srv.settimeout(0.5)                  # lets stop_event be noticed
        self.srv = srv
        log("glowbug %s listening on %s (api %d)" % (VERSION, SOCK_PATH, SOCKET_API))
        try:
            while not self.stop_event.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError as e:
                    log("socket: accept failed: %s" % e)
                    time.sleep(0.1)
                    continue
                if not self.conn_sem.acquire(blocking=False):
                    try:
                        conn.sendall((json.dumps(_fail(
                            "busy", "%d connections open — try again" % MAX_CONNS))
                            + "\n").encode())
                    except OSError:
                        pass
                    conn.close()
                    continue
                try:
                    threading.Thread(target=self.serve_conn, args=(conn,),
                                     daemon=True).start()
                except RuntimeError as e:    # can't start a thread
                    log("socket: %s" % e)
                    self.conn_sem.release()
                    conn.close()
        finally:
            srv.close()

    @staticmethod
    def _read_request(conn):
        """Bytes up to the first newline, or everything up to EOF."""
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return data.strip()
            data += chunk
            head = data.lstrip()
            if b"\n" in head:
                return head.split(b"\n", 1)[0].strip()
            if len(data) > SOCK_REQUEST_MAX:
                raise ValueError("request too large")

    def serve_conn(self, conn):
        """One connection, on its own thread. Every socket error ends this
        connection only — a client that hangs up before reading its reply
        (BrokenPipe on sendall) used to take the whole daemon down."""
        try:
            conn.settimeout(SOCK_READ_TIMEOUT_S)
            data = self._read_request(conn)
            if not data:
                return
            msg = json.loads(data.decode(errors="replace"))
            if isinstance(msg, dict) and "cmd" in msg:
                if msg.get("cmd") == "events":
                    self._serve_events(conn, msg)
                else:
                    conn.sendall((json.dumps(self.handle_request(msg)) + "\n").encode())
            elif isinstance(msg, dict):
                self.handle_hook(msg)
            else:
                log("bad hook payload: not a JSON object")
        except ValueError as e:               # JSONDecodeError is a ValueError
            log("bad hook payload: %s" % e)
        except OSError as e:                  # incl. socket.timeout, EPIPE
            log("socket: client dropped (%s)" % e)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.conn_sem.release()

    def _serve_events(self, conn, msg):
        kinds = msg.get("filter")
        if isinstance(kinds, str):
            kinds = [k for k in kinds.split(",") if k]
        if kinds is not None and not (isinstance(kinds, list)
                                      and all(isinstance(k, str) for k in kinds)):
            conn.sendall((json.dumps(_fail("bad_arg", "filter: want a list of event "
                                                      "kinds")) + "\n").encode())
            return
        sub = self.subscribe(set(kinds) if kinds is not None else None)
        if sub is None:
            conn.sendall((json.dumps(_fail("busy", "%d event streams already open"
                                           % MAX_SUBSCRIBERS)) + "\n").encode())
            return
        try:
            conn.sendall((json.dumps(_ok(cmd="events", board=self._board_view()))
                          + "\n").encode())
            while True:
                try:
                    ev = sub.q.get(timeout=0.5)
                except queue.Empty:
                    ev = None
                if sub.dropped:
                    conn.sendall((json.dumps(_fail("busy", "events dropped: reader "
                                                           "too slow")) + "\n").encode())
                    return
                if ev is not None:
                    conn.sendall((json.dumps(ev) + "\n").encode())
                r, _, _ = select.select([conn], [], [], 0)
                if r and not conn.recv(4096):
                    return                   # the client hung up
        finally:
            self.unsubscribe(sub)


class _Subscriber:
    __slots__ = ("q", "kinds", "dropped")

    def __init__(self, kinds):
        self.q = queue.Queue(SUB_QUEUE)
        self.kinds = kinds
        self.dropped = False


def run_daemon():
    d = Daemon()
    for w in d.table_warnings:
        log("palette: %s" % w)
    t = threading.Thread(target=d.serial_loop, daemon=True)
    t.start()
    d.socket_loop()


# ------------------------------------------------------------ install / admin
PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.glowbug.daemon</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>{app}/glowbug.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- Interactive = launchd never background-throttles the daemon. Without
       this, a saturated Mac (bench: load ~180 under a KeyShot render)
       starves the serial thread for 5-13s at a stretch, the board misses
       its PINGs, and the display flaps "Offline". -->
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def merge_hooks(settings_path, hook_cmd):
    """Idempotently add Glowbug's hook entries to Claude Code settings.
    Backup kept; aborts loudly on malformed JSON (never guesses)."""
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings = {}
    if os.path.exists(settings_path):
        shutil.copy(settings_path, settings_path + ".glowbug-backup")
        with open(settings_path) as f:
            settings = json.load(f)          # malformed JSON = loud abort
    hooks = settings.setdefault("hooks", {})
    added = []
    for ev in HOOK_EVENTS:
        entries = hooks.setdefault(ev, [])
        if any(hook_cmd in json.dumps(e) for e in entries):
            continue
        entries.append({
            "matcher": "*",
            "hooks": [{"type": "command", "command": hook_cmd, "async": True}],
        })
        added.append(ev)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    return added


def unmerge_hooks(settings_path, needle):
    """Remove any hook entry whose command mentions `needle`."""
    if not os.path.exists(settings_path):
        return 0
    with open(settings_path) as f:
        settings = json.load(f)
    removed = 0
    hooks = settings.get("hooks", {})
    for ev in list(hooks):
        before = len(hooks[ev])
        hooks[ev] = [e for e in hooks[ev] if needle not in json.dumps(e)]
        removed += before - len(hooks[ev])
        if not hooks[ev]:
            del hooks[ev]
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    return removed


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


# ------------------------------------------------- wiring up the other tools
# Rules, in order of importance:
#   1. only ever ADD entries; never rewrite or reorder what's already there
#   2. back up before every write
#   3. if a config file doesn't parse, leave it alone and say so — never guess
#   4. never create a config directory for a tool that isn't installed
#   5. wire each tool once; if you delete our hook, it stays deleted

def hook_cmd(source, event=""):
    """How a tool should invoke our forwarder. Explicit interpreter: apps
    launched from the Finder have a minimal PATH, so a shebang is a coin flip."""
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    cmd = '%s -S -E "%s" --source %s' % (
        py, os.path.join(APP_DIR, "glowbug-hook.py"), source)
    return cmd + (" --event %s" % event if event else "")


def read_json_config(path):
    """{} if absent, the parsed config if readable, None if we must not touch it."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json_config(path, data):
    if os.path.exists(path):
        shutil.copy(path, path + ".glowbug-backup")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_state():
    try:
        with open(os.path.join(APP_DIR, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(os.path.join(APP_DIR, "state.json"), "w") as f:
            json.dump(st, f, indent=2)
    except OSError:
        pass


# ---- Claude Code ----
def has_claude():
    return True          # always wired: settings.json is created if absent,
                         # so installing Claude Code later just works


def install_claude():
    added = merge_hooks(CLAUDE_SETTINGS, os.path.join(APP_DIR, "glowbug-hook.py"))
    return True, added, ""


def uninstall_claude():
    return unmerge_hooks(CLAUDE_SETTINGS, "glowbug")


# ---- Cursor ----
def has_cursor():
    return (os.path.isdir(os.path.join(HOME, ".cursor"))
            or os.path.exists("/Applications/Cursor.app")
            or bool(shutil.which("cursor-agent")))


def install_cursor():
    if not os.path.isdir(os.path.dirname(CURSOR_HOOKS)):
        return False, [], "no ~/.cursor directory — is Cursor installed?"
    cfg = read_json_config(CURSOR_HOOKS)
    if cfg is None:
        return False, [], "%s isn't valid JSON — left untouched" % CURSOR_HOOKS
    cfg.setdefault("version", 1)
    hooks = cfg.setdefault("hooks", {})
    added = []
    for ev in CURSOR_EVENTS:
        entries = hooks.setdefault(ev, [])
        if any("glowbug" in json.dumps(e) for e in entries):
            continue
        entries.append({"command": hook_cmd("cursor", ev), "timeout": 5})
        added.append(ev)
    if added:
        write_json_config(CURSOR_HOOKS, cfg)
    return True, added, ""


def uninstall_cursor():
    cfg = read_json_config(CURSOR_HOOKS)
    if not cfg:
        return 0
    hooks = cfg.get("hooks", {})
    removed = 0
    for ev in list(hooks):
        before = len(hooks[ev])
        hooks[ev] = [e for e in hooks[ev] if "glowbug" not in json.dumps(e)]
        removed += before - len(hooks[ev])
        if not hooks[ev]:
            del hooks[ev]
    if removed:
        write_json_config(CURSOR_HOOKS, cfg)
    return removed


# ---- Codex ----
CODEX_TOML_NOTE = (
    "Codex needs one setting turned on. Add these two lines to %s:\n"
    "        [features]\n"
    "        hooks = true\n"
    "      (Glowbug doesn't edit that file — it's yours.)" % CODEX_CONFIG)


def has_codex():
    return os.path.isdir(CODEX_HOME) or bool(shutil.which("codex"))


def codex_hooks_enabled():
    """Read-only peek at config.toml — we never write to it."""
    try:
        with open(CODEX_CONFIG) as f:
            txt = f.read()
    except OSError:
        return False
    return bool(re.search(r"^\s*hooks\s*=\s*true", txt, re.M) or
                re.search(r"^\s*features\s*\.\s*hooks\s*=\s*true", txt, re.M))


def install_codex():
    if not os.path.isdir(CODEX_HOME):
        return False, [], "no %s directory — is Codex installed?" % CODEX_HOME
    cfg = read_json_config(CODEX_HOOKS)
    if cfg is None:
        return False, [], "%s isn't valid JSON — left untouched" % CODEX_HOOKS
    hooks = cfg.setdefault("hooks", {})
    added = []
    for ev in CODEX_EVENTS:
        entries = hooks.setdefault(ev, [])
        if any("glowbug" in json.dumps(e) for e in entries):
            continue
        entries.append({"hooks": [{"type": "command",
                                   "command": hook_cmd("codex", ev),
                                   "timeout": 5, "async": True}]})
        added.append(ev)
    if added:
        write_json_config(CODEX_HOOKS, cfg)
    return True, added, ("" if codex_hooks_enabled() else CODEX_TOML_NOTE)


def uninstall_codex():
    cfg = read_json_config(CODEX_HOOKS)
    if not cfg:
        return 0
    hooks = cfg.get("hooks", {})
    removed = 0
    for ev in list(hooks):
        before = len(hooks[ev])
        hooks[ev] = [e for e in hooks[ev] if "glowbug" not in json.dumps(e)]
        removed += before - len(hooks[ev])
        if not hooks[ev]:
            del hooks[ev]
    if removed:
        write_json_config(CODEX_HOOKS, cfg)
    return removed


# ---- Antigravity ----
def antigravity_dir():
    for d in ANTIGRAVITY_DIRS:
        if os.path.isdir(d):
            return d
    return None


def has_antigravity():
    return (antigravity_dir() is not None
            or os.path.exists("/Applications/Antigravity.app")
            or bool(shutil.which("agy")))


def install_antigravity():
    d = antigravity_dir()
    if d is None:
        return False, [], ("no Antigravity config directory found (looked in %s)"
                           % ", ".join(ANTIGRAVITY_DIRS))
    path = os.path.join(d, "hooks.json")
    cfg = read_json_config(path)
    if cfg is None:
        return False, [], "%s isn't valid JSON — left untouched" % path
    entry = {"enabled": True}
    for ev in ANTIGRAVITY_EVENTS:
        entry[ev] = [{"matcher": "*",
                      "hooks": [{"type": "command",
                                 "command": hook_cmd("antigravity", ev),
                                 "timeout": 5}]}]
    if cfg.get("glowbug") == entry:
        return True, [], ""
    cfg["glowbug"] = entry             # one key of ours; everything else untouched
    write_json_config(path, cfg)
    return True, list(ANTIGRAVITY_EVENTS), ""


def uninstall_antigravity():
    removed = 0
    for d in ANTIGRAVITY_DIRS:
        path = os.path.join(d, "hooks.json")
        cfg = read_json_config(path)
        if cfg and "glowbug" in cfg:
            del cfg["glowbug"]
            write_json_config(path, cfg)
            removed += 1
    return removed


SOURCES = [
    ("claude",      "Claude Code", has_claude,      install_claude,      uninstall_claude),
    ("cursor",      "Cursor",      has_cursor,      install_cursor,      uninstall_cursor),
    ("codex",       "Codex",       has_codex,       install_codex,       uninstall_codex),
    ("antigravity", "Antigravity", has_antigravity, install_antigravity, uninstall_antigravity),
]


def wire_sources(explicit=False):
    """Register hooks with every detected tool. Returns a list of
    (name, label, ok, added, note) for the caller to print or log."""
    out = []
    st = load_state()
    wired = st.setdefault("wired", {})
    for name, label, detect, do_install, _ in SOURCES:
        try:
            if not detect():
                out.append((name, label, None, [], "not detected"))
                continue
            if not explicit and name in wired:
                continue          # already done once; respect manual removal
            ok, added, note = do_install()
            if ok:
                wired[name] = {"at": int(time.time()), "events": added or wired.get(name, {}).get("events", [])}
            out.append((name, label, ok, added, note))
        except Exception as e:            # a broken tool config is never fatal
            out.append((name, label, False, [], str(e)))
    save_state(st)
    return out


def install():
    src = os.path.abspath(__file__)

    # migrate an old CoderDong install if present
    if os.path.exists(OLD_PLIST) or os.path.isdir(OLD_DIR):
        print("==> Migrating old CoderDong install")
        launchctl("unload", OLD_PLIST)
        for p in (OLD_PLIST,):
            try: os.unlink(p)
            except OSError: pass
        shutil.rmtree(OLD_DIR, ignore_errors=True)
        unmerge_hooks(CLAUDE_SETTINGS, "coderdong")

    print("==> Installing Glowbug to %s" % APP_DIR)
    os.makedirs(APP_DIR, exist_ok=True)
    dst = os.path.join(APP_DIR, "glowbug.py")
    # re-running install FROM the installed copy is a supported way to
    # refresh hooks/forwarder — skip the self-copy instead of crashing
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy(src, dst)
    fw_src = os.path.join(os.path.dirname(src), "firmware")
    if os.path.exists(os.path.join(fw_src, "glowbug.bin")):
        shutil.copy(os.path.join(fw_src, "glowbug.bin"),
                    os.path.join(APP_DIR, "firmware.bin"))
        for extra in ("VERSION", "SHA256SUMS"):
            if os.path.exists(os.path.join(fw_src, extra)):
                shutil.copy(os.path.join(fw_src, extra),
                            os.path.join(APP_DIR, extra))
        print("    rescue firmware image installed")
    with open(os.path.join(APP_DIR, "glowbug-hook.py"), "w") as f:
        f.write(FORWARDER_SOURCE)
    for name in ("glowbug.py", "glowbug-hook.py"):
        os.chmod(os.path.join(APP_DIR, name), 0o755)

    print("==> Connecting your coding agents")
    results = wire_sources(explicit=True)
    for name, label, ok, added, note in results:
        if ok is None:
            print("    %-14s not installed — skipped (it'll connect itself"
                  " if you install it later)" % label)
        elif ok:
            print("    %-14s %s" % (label, "connected (%d events)" % len(added)
                                    if added else "already connected"))
            if note:
                print("      ! %s" % note)
        else:
            print("    %-14s COULD NOT CONNECT: %s" % (label, note))
    print("    note: hooks apply to NEW sessions only — restart any that are open")

    print("==> Installing LaunchAgent")
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "w") as f:
        f.write(PLIST_TEMPLATE.format(app=APP_DIR, log=LOG_PATH))
    launchctl("unload", PLIST_PATH)
    r = launchctl("load", PLIST_PATH)
    if r.returncode != 0:
        sys.exit("glowbug: launchctl load failed: %s" % r.stderr.strip())

    # self-check
    time.sleep(1.5)
    daemon_ok = launchctl("list", "dev.glowbug.daemon").returncode == 0
    port = find_port()
    print()
    print("  %s daemon %s" % ("✓" if daemon_ok else "✗", "running" if daemon_ok else "NOT RUNNING"))
    print("  %s board %s" % ("✓" if port else "✗", ("connected (%s)" % port) if port else "not found — is it plugged in?"))
    for name, label, ok, added, note in results:
        mark = "✓" if ok else ("—" if ok is None else "✗")
        print("  %s %-14s %s" % (mark, label,
                                 "connected" if ok else
                                 ("not installed" if ok is None else note)))
    print()
    print("Glowbug is %s. New sessions will appear on the device."
          % ("ready" if (daemon_ok and port) else "partially set up"))


def uninstall():
    print("==> Stopping daemon")
    launchctl("unload", PLIST_PATH)
    for p in (PLIST_PATH,):
        try: os.unlink(p)
        except OSError: pass
    print("==> Disconnecting from your coding agents")
    for name, label, _detect, _install, do_uninstall in SOURCES:
        try:
            n = do_uninstall()
            if n:
                print("    %-14s removed %d hook entries (backup kept)" % (label, n))
        except Exception as e:
            print("    %-14s could not clean up: %s" % (label, e))
    print("==> Removing %s" % APP_DIR)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    try:
        os.unlink(SOCK_PATH)
    except OSError:
        pass
    print("Glowbug uninstalled. (Log kept at %s)" % LOG_PATH)


def ask_daemon(timeout=1.0):
    """Ask the running daemon what it sees. None if it isn't listening."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
        s.sendall(json.dumps({"cmd": "report"}).encode())
        s.shutdown(socket.SHUT_WR)          # server reads to EOF
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        return json.loads(data.decode(errors="replace"))
    except (OSError, ValueError):
        return None


def status():
    daemon_ok = launchctl("list", "dev.glowbug.daemon").returncode == 0
    port = find_port()
    board = port or "not found"
    try:                                     # the daemon knows fw + PROTO
        b = _call({"cmd": "info"}, timeout=1.0).get("board") or {}
        if b.get("online"):
            board = "%s (fw %s, PROTO %s)" % (b.get("port") or port or "?",
                                              b.get("fw"), b.get("proto"))
    except GlowbugError:
        pass
    print("glowbug %s · daemon %s · board %s" % (
        VERSION,
        "running" if daemon_ok else "stopped",
        board))


def doctor():
    """Everything you need to answer 'why isn't X showing up?'"""
    daemon_ok = launchctl("list", "dev.glowbug.daemon").returncode == 0
    port = find_port()
    print("glowbug %s" % VERSION)
    print("  daemon    %s" % ("running" if daemon_ok else "STOPPED"))
    print("  board     %s" % (port or "not found — is it plugged in?"))
    print("  app dir   %s" % APP_DIR)
    print("  socket    %s" % SOCK_PATH)
    print("  log       %s" % LOG_PATH)
    print()
    rep = ask_daemon()
    if rep is None:
        print("  The daemon isn't answering — start it with: glowbug install")
        return
    print("  Sessions on the device:")
    if rep.get("sessions"):
        for s in rep["sessions"]:
            print("    %-12s %-21s %s" % (s["source"], s["name"], s["state"]))
    else:
        print("    (none — start a session, the device wakes within a second)")
    print()
    print("  Your coding agents:")
    seen = rep.get("last_event_at") or {}
    wired = (load_state().get("wired") or {})
    for name, label, detect, _i, _u in SOURCES:
        ago = seen.get(name)
        if ago is not None:
            note = "last event %.0fs ago" % ago
        elif name in wired:
            note = "connected — hooks only attach to NEW sessions, start one"
        elif detect():
            note = "installed but not connected yet — run: glowbug install"
        else:
            note = "not installed"
        print("    %-14s %s" % (label, note))



# -------------------------------------------------------------------- rescue
DFU_ID = "0483:df11"          # STM32 ROM bootloader, all families
FW_RAW_URL = "https://raw.githubusercontent.com/pud/glowbug/main/firmware/glowbug.bin"

# Flash map since fw 2.0.0: page 0 (2 KB at 0x08000000) is a resident
# bootloader, the app lives at 0x08000800 and ends where the settings page
# starts. The image the daemon carries and flashes is the PRODUCTION image:
# bootloader (padded to 2 KB) + app, written in one pass at 0x08000000 —
# that is what makes it work on a fresh-from-factory board AND on a 1.4.x
# board that has no bootloader yet, and on a 2.0.0 board it simply rewrites
# the identical frozen bootloader. The image announces itself twice: the
# bootloader id "GLWB" at 0xC0 (after its 48-entry vector table) and the
# app manifest "GLWA" at 0x800 + 0xC0 (after the app's).
FLASH_ADDR = 0x08000000
BOOT_LEN = 0x800
APP_FLASH_ADDR = FLASH_ADDR + BOOT_LEN
APP_FLASH_END = 0x0800F800
BOOT_MAGIC = b"GLWB"
APP_MAGIC = b"GLWA"
MAGIC_OFFSET = 0xC0


def check_rescue_image(path):
    """None if `path` is a Glowbug PRODUCTION image (bootloader + app) safe
    to flash at FLASH_ADDR, else the reason it is not. Refuses the old
    whole-flash 1.4.x image (no GLWB/GLWA marks), an app-only image (it
    would land in page 0), and anything whose app reset vector points
    outside the app region or that runs into the settings page."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return "unreadable (%s)" % e
    if len(data) < BOOT_LEN + MAGIC_OFFSET + len(APP_MAGIC):
        return "too small (%d bytes)" % len(data)
    if data[MAGIC_OFFSET:MAGIC_OFFSET + 4] != BOOT_MAGIC:
        if data[MAGIC_OFFSET:MAGIC_OFFSET + 4] == APP_MAGIC:
            return "app-only image (GLWA at 0xC0) — rescue needs the boot+app production image"
        return 'no "GLWB" bootloader id at offset 0x%X' % MAGIC_OFFSET
    if data[BOOT_LEN + MAGIC_OFFSET:BOOT_LEN + MAGIC_OFFSET + 4] != APP_MAGIC:
        return 'no "GLWA" app manifest at offset 0x%X' % (BOOT_LEN + MAGIC_OFFSET)
    if len(data) > APP_FLASH_END - FLASH_ADDR:
        return "%d bytes runs into the settings page (max %d)" % (
            len(data), APP_FLASH_END - FLASH_ADDR)
    vec = int.from_bytes(data[BOOT_LEN + 4:BOOT_LEN + 8], "little") & ~1
    if not APP_FLASH_ADDR <= vec < APP_FLASH_END:
        return "app reset vector 0x%08X is outside the app region" % vec
    return None


check_app_image = check_rescue_image   # name used by older notes


def _dfu_present():
    try:
        out = subprocess.run(["dfu-util", "-l"], capture_output=True,
                             text=True, timeout=10).stdout
        return DFU_ID in out
    except (OSError, subprocess.SubprocessError):
        return False


def _find_firmware():
    """Locate the bundled known-good image; verify sha256 when a manifest
    sits beside it. Returns (path, version) or (None, None)."""
    import hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (APP_DIR, os.path.join(here, "firmware")):
        bin_path = os.path.join(base, "firmware.bin")
        if not os.path.exists(bin_path):
            bin_path = os.path.join(base, "glowbug.bin")
        if not os.path.exists(bin_path):
            continue
        sums = os.path.join(base, "SHA256SUMS")
        if os.path.exists(sums):
            want = open(sums).read().split()[0]
            got = hashlib.sha256(open(bin_path, "rb").read()).hexdigest()
            if got != want:
                print("!! %s fails its integrity check — ignoring it" % bin_path)
                continue
        ver = "unknown"
        vp = os.path.join(base, "VERSION")
        if os.path.exists(vp):
            ver = open(vp).read().strip()
        return bin_path, ver
    return None, None


def rescue():
    """Reflash the known-good firmware. Handles a running board (sends the
    in-band DFU command) AND a 'bricked' one (user holds the knob at plug-in
    -> ROM bootloader). Never touches the network — if the image is missing,
    prints the fetch command for the USER to run."""
    if shutil.which("dfu-util") is None:
        sys.exit("dfu-util is required for rescue. Install it with:\n\n"
                 "    brew install dfu-util\n\nthen re-run: glowbug rescue")
    fw, fw_ver = _find_firmware()
    if fw is None:
        sys.exit("No firmware image found. Fetch the known-good image with:\n\n"
                 "    mkdir -p %s && curl -fsSL -o %s/firmware.bin \\\n"
                 "        %s\n\nthen re-run: glowbug rescue"
                 % (APP_DIR, APP_DIR, FW_RAW_URL))
    print("==> Firmware image: %s (fw %s)" % (fw, fw_ver))
    why = check_rescue_image(fw)
    if why:
        sys.exit("Refusing to flash %s: %s.\n\n"
                 "rescue writes the boot+app production image at 0x%08X (the\n"
                 "bootloader is frozen, so rewriting it is safe on every board).\n"
                 "Fetch the current image:\n\n"
                 "    curl -fsSL -o %s/firmware.bin %s\n\nthen re-run: glowbug rescue"
                 % (fw, why, FLASH_ADDR, APP_DIR, FW_RAW_URL))

    daemon_was_loaded = os.path.exists(PLIST_PATH)
    if daemon_was_loaded:
        subprocess.run(["launchctl", "unload", PLIST_PATH],
                       capture_output=True)
    try:
        if not _dfu_present():
            port = find_port()
            if port:
                print("==> Glowbug found on %s — asking it to enter update mode"
                      % port)
                try:
                    fd = open_serial(port)
                    os.write(fd, b"DFU\n")
                    os.close(fd)
                except OSError:
                    pass
                deadline = time.time() + 20
            else:
                print("""==> No Glowbug detected. Put it in Rescue Mode:

    1. Unplug the Glowbug.
    2. Press and hold the knob (push straight down) — keep holding.
    3. While holding, plug the USB-C cable back in.
    4. Keep holding two more seconds, then let go.

The middle screen will read RESCUE MODE. Waiting up to 60s...""")
                deadline = time.time() + 60
            while not _dfu_present():
                if time.time() > deadline:
                    sys.exit("Never saw the device in rescue mode. Check the\n"
                             "cable (charge-only cables are common!) and see\n"
                             "TROUBLESHOOTING.md.")
                time.sleep(1.5)
        print("==> Rescue mode detected — writing firmware (~10s)...")
        try:
            r = subprocess.run(
                ["dfu-util", "-a", "0", "-s", "0x%08X:leave" % FLASH_ADDR,
                 "-D", fw],
                capture_output=True, text=True, timeout=90)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            # dfu-util can hang on the final :leave handshake after the
            # device has already reset into the app — judge by the output.
            out = ((e.stdout or b"").decode(errors="replace") +
                   (e.stderr or b"").decode(errors="replace"))
        if "File downloaded successfully" not in out:
            tail = "\n".join(out.strip().splitlines()[-6:])
            sys.exit("Flash FAILED:\n%s" % tail)
        print("==> Firmware written — waiting for the Glowbug to wake up...")
        deadline = time.time() + 20
        while time.time() < deadline:
            if find_port():
                print("\n✓ Glowbug restored (fw %s). Enjoy the welcome show."
                      % fw_ver)
                return
            time.sleep(1.5)
        print("\nFirmware written OK, but the device hasn't re-appeared —\n"
              "unplug it and plug it back in.")
    finally:
        if daemon_was_loaded:
            subprocess.run(["launchctl", "load", PLIST_PATH],
                           capture_output=True)


# --------------------------------------------------------- the Python API
# `import glowbug; glowbug.show(3, color="green", line1="Build OK",
# sound="ding", seconds=5)`. Every helper is one round trip to the daemon
# over the unix socket and returns the reply dict; a refusal raises
# GlowbugError(code, message). Language-neutral equivalent:
#   printf '{"cmd":"show","screen":3,"color":"green","line1":"Build OK",
#           "sound":"ding","for":5}\n' | nc -U "$HOME/Library/Application
#           Support/Glowbug/daemon.sock"

def _connect(timeout):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK_PATH)
    except OSError as e:
        s.close()
        raise GlowbugError("no_daemon", "the Glowbug daemon isn't running (%s) — "
                           "run: glowbug install" % e)
    return s


def _read_line(s, limit=SOCK_REQUEST_MAX):
    """(first line, leftover bytes) from a socket; EOF ends the line."""
    data = b""
    while b"\n" not in data:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise ValueError("reply too long")
    line, _, rest = data.partition(b"\n")
    return line, rest


def _call(msg, timeout=5.0):
    """One request dict -> the daemon's reply dict. Raises GlowbugError
    with the daemon's code on ok:false, or "no_daemon" when nothing
    answers on the socket."""
    s = _connect(timeout)
    try:
        s.sendall((json.dumps(msg) + "\n").encode())
        line, _ = _read_line(s)
        if not line:
            raise GlowbugError("no_daemon", "the daemon closed the connection "
                               "without answering")
        rep = json.loads(line.decode(errors="replace"))
    except (OSError, ValueError) as e:
        raise GlowbugError("no_daemon", "no answer from the daemon (%s)" % e)
    finally:
        s.close()
    if not isinstance(rep, dict):
        raise GlowbugError("no_daemon", "malformed reply from the daemon")
    if "ok" not in rep:                       # a 1.5.0 daemon: report() only
        raise GlowbugError("no_daemon", "the running daemon (%s) predates the API "
                           "— run `glowbug install` to update it"
                           % rep.get("version", "?"))
    if not rep.get("ok"):
        raise GlowbugError(rep.get("error") or "bad_request", rep.get("message") or "")
    return rep


def _req(cmd, **kw):
    """A request dict: None and False arguments are omitted; `seconds`
    becomes the wire's "for"."""
    if "seconds" in kw:
        kw["for"] = kw.pop("seconds")
    msg = {"cmd": cmd}
    for k, v in kw.items():
        if v is not None and v is not False:
            msg[k] = v
    return msg


def show(screen, color=None, mode=None, line1=None, line2=None, big=None,
         sound=None, seconds=None, volume=None, to=None, period=None,
         raw=False, no_led=False):
    """Light screen 1-5 (or "all", or "1,3"): a color (palette name, #RGB,
    RRGGBB) on its status LED, one or two text lines or one big line, a
    sound, held for `seconds` (None = until released)."""
    return _call(_req("show", screen=screen, color=color, mode=mode, line1=line1,
                      line2=line2, big=big, sound=sound, seconds=seconds,
                      volume=volume, to=to, period=period, raw=raw, no_led=no_led))


def led(sel, color, mode=None, seconds=None, to=None, period=None,
        fade_ms=None, raw=False):
    """Color LEDs: 1-5 (status), ug1-ug5 (underglow), all/glass/ug or a
    list; mode solid/fade/pulse/blink/off."""
    return _call(_req("led", sel=sel, color=color, mode=mode, seconds=seconds,
                      to=to, period=period, fade_ms=fade_ms, raw=raw))


def text(screen, line1, line2=None, big=False, seconds=None):
    """Text on screen 1-5 / all: two 21-char lines, or one big line."""
    if big:
        return _call(_req("text", screen=screen, big=line1, seconds=seconds))
    return _call(_req("text", screen=screen, line1=line1, line2=line2,
                      seconds=seconds))


def sound(name_or_notes, volume=None):
    """Play a named sound, "hz:ms,hz:ms,..." (hz 0 = rest), or "off"."""
    return _call(_req("sound", sound=name_or_notes, volume=volume))


def raw(*lines, **kw):
    """Relay wire lines verbatim (PROTOCOL.md). raw("LED 0 SET FF0000",
    confirm=True) waits for the board to parse them and raises board_err
    with its ERR replies."""
    return _call(_req("raw", lines=list(lines), confirm=kw.get("confirm", False)))


def own(*resources, **kw):
    """Claim "led:<sel>", "glass:<sel>", "sound", "enc" or "all", for
    `seconds` (keyword) or until released."""
    return _call(_req("own", resources=list(resources), seconds=kw.get("seconds")))


def release(*resources):
    """Release resources (none = everything this host owns)."""
    return _call(_req("release", resources=list(resources) or None))


def events(kinds=None, timeout=5.0):
    """Generator of event dicts from the board and the daemon: enc, click,
    hold, menu, own, set, err, hello, board, info, redraw, evt. `kinds`
    filters. Runs until the daemon or the caller closes the socket."""
    s = _connect(timeout)
    try:
        s.sendall((json.dumps(_req("events", filter=list(kinds) if kinds else None))
                   + "\n").encode())
        line, buf = _read_line(s)
        rep = json.loads(line.decode(errors="replace")) if line else {}
        if not rep.get("ok"):
            raise GlowbugError(rep.get("error") or "no_daemon",
                               rep.get("message") or "no events stream")
        s.settimeout(None)
        while True:
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield json.loads(line.decode(errors="replace"))
            chunk = s.recv(65536)
            if not chunk:
                return
            buf += chunk
    finally:
        s.close()


def info(fresh=False):
    return _call(_req("info", fresh=fresh))


def palette(reload=False):
    return _call(_req("palette", reload=reload))


def settings_get(key=None):
    rep = _call(_req("settings", op="get", key=key))
    return rep["settings"] if key is None else rep["settings"][key]


def settings_set(key, value):
    return _call({"cmd": "settings", "op": "set", "key": key, "value": value})["settings"][key]


def settings_save():
    _call(_req("settings", op="save"))
    return True


# ---------------------------------------------------------------- the CLI
def build_parser():
    p = argparse.ArgumentParser(
        prog="glowbug",
        description="Glowbug — a machined aluminum bar that shows your coding-agent "
                    "sessions. With no command, runs the daemon.",
        epilog="Colors: a palette name (glowbug palette), #RGB or RRGGBB. Sounds: "
               "a name or hz:ms,hz:ms,... Screens and status LEDs are 1-5 left to "
               "right, underglow ug1-ug5. Exit codes: 0 ok, 1 the daemon refused, "
               "2 usage, 3 no daemon.")
    p.add_argument("--version", action="version", version="glowbug %s" % VERSION)
    sub = p.add_subparsers(dest="cmd", metavar="<command>")
    for name, help_ in (("install", "set everything up (daemon, hooks, autostart)"),
                        ("uninstall", "remove everything cleanly"),
                        ("status", "one-line health check"),
                        ("doctor", "verbose health check (paths, per-tool wiring)"),
                        ("rescue", "reflash firmware (works even on a \"bricked\" board)"),
                        ("version", "print the version")):
        sub.add_parser(name, help=help_)

    def api(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", help="print the daemon's reply as JSON")
        return sp

    def for_arg(sp):
        sp.add_argument("--for", dest="seconds", type=float, metavar="SECONDS",
                        help="release automatically after this long (0.05..86400)")

    def mode_args(sp, with_fade):
        sp.add_argument("--mode", choices=LED_MODES, help="solid (default), fade, pulse, blink, off")
        sp.add_argument("--to", metavar="COLOR", help="pulse/blink: the other color (pulse: 30%% of the color)")
        sp.add_argument("--period", type=int, metavar="MS", help="pulse/blink period (16..65535)")
        if with_fade:
            sp.add_argument("--fade-ms", dest="fade_ms", type=int, metavar="MS", help="fade time (default 700)")
        sp.add_argument("--raw", action="store_true",
                        help="send the color as-is instead of scaled by the board's brightness setting")

    s = api("show", "light a screen: color, text, sound, hold time")
    s.add_argument("screen", help="1-5 (left to right), a list like 1,3, or all")
    s.add_argument("--color", "-c", metavar="COLOR")
    mode_args(s, False)
    s.add_argument("--line1", metavar="TEXT", help="top text line (21 chars)")
    s.add_argument("--line2", metavar="TEXT", help="bottom text line")
    s.add_argument("--big", metavar="TEXT", help="one big centered line instead")
    s.add_argument("--sound", "-s", metavar="SOUND")
    s.add_argument("--volume", type=int, choices=range(5), metavar="0-4")
    for_arg(s)
    s.add_argument("--no-led", dest="no_led", action="store_true", help="text only, leave the LED")

    s = api("led", "color LEDs: 1-5, ug1-ug5, all, glass, ug")
    s.add_argument("sel", help="LED selector, e.g. 3 / ug2 / 1,3,ug5 / all")
    s.add_argument("color", help="palette name, #RGB or RRGGBB")
    mode_args(s, True)
    for_arg(s)

    s = api("text", "text on a screen")
    s.add_argument("screen", help="1-5, a list, or all")
    s.add_argument("line1", help="top line (or the big line with --big)")
    s.add_argument("line2", nargs="?", help="bottom line")
    s.add_argument("--big", action="store_true", help="one big centered line")
    for_arg(s)

    s = api("sound", "play a sound")
    s.add_argument("sound", help="a name, hz:ms,hz:ms,... (hz 0 = rest), or off")
    s.add_argument("--volume", type=int, choices=range(5), metavar="0-4")

    s = api("raw", "relay wire lines verbatim (see PROTOCOL.md)")
    s.add_argument("lines", nargs="+", metavar="LINE", help='"LED 0 SET FF0000" ... or - for stdin')
    s.add_argument("--confirm", action="store_true",
                   help="wait until the board parsed them; fail with its ERR replies")

    s = api("own", "claim resources: led:<sel> glass:<sel> sound enc all")
    s.add_argument("resources", nargs="+", metavar="RES")
    for_arg(s)

    s = api("release", "release resources (none = everything)")
    s.add_argument("resources", nargs="*", metavar="RES")

    s = api("events", "stream board events as JSON lines")
    s.add_argument("--filter", metavar="KINDS",
                   help="comma list: enc,click,hold,menu,own,err,board,hello,set,info,redraw,evt")

    api("info", "daemon + board capabilities, ownership")

    s = api("palette", "list color and sound names")
    s.add_argument("--reload", action="store_true", help="re-read ~/.glowbug/palette.json + sounds.json")

    s = api("settings", "the board's user settings")
    s.add_argument("op", choices=("get", "set", "save"))
    s.add_argument("key", nargs="?", help="brightness ug_brightness ug_mode volume chime flip")
    s.add_argument("value", nargs="?")
    return p


def _cli_request(args):
    """Parsed CLI arguments -> the request dict the daemon gets."""
    c = args.cmd
    if c == "show":
        return _req("show", screen=args.screen, color=args.color, mode=args.mode,
                    to=args.to, period=args.period, line1=args.line1, line2=args.line2,
                    big=args.big, sound=args.sound, volume=args.volume,
                    seconds=args.seconds, no_led=args.no_led, raw=args.raw)
    if c == "led":
        return _req("led", sel=args.sel, color=args.color, mode=args.mode, to=args.to,
                    period=args.period, fade_ms=args.fade_ms, seconds=args.seconds,
                    raw=args.raw)
    if c == "text":
        if args.big:
            return _req("text", screen=args.screen, big=args.line1, seconds=args.seconds)
        return _req("text", screen=args.screen, line1=args.line1, line2=args.line2,
                    seconds=args.seconds)
    if c == "sound":
        return _req("sound", sound=args.sound, volume=args.volume)
    if c == "raw":
        lines = args.lines
        if lines == ["-"]:
            lines = [l.rstrip("\r\n") for l in sys.stdin if l.strip()]
        return _req("raw", lines=lines, confirm=args.confirm)
    if c == "own":
        return _req("own", resources=args.resources, seconds=args.seconds)
    if c == "release":
        return _req("release", resources=args.resources or None)
    if c == "info":
        return _req("info")
    if c == "palette":
        return _req("palette", reload=args.reload)
    if c == "settings":
        if args.op == "set" and args.value is None:
            raise GlowbugError("bad_arg", "settings set needs <key> <value>")
        if args.op == "save" and args.key is not None:
            raise GlowbugError("bad_arg", "settings save takes no arguments")
        msg = _req("settings", op=args.op, key=args.key)
        if args.op == "set":
            msg["value"] = args.value
        return msg
    raise GlowbugError("bad_arg", "unknown command %r" % c)


def _human(cmd, rep):
    """The one-line (or few-line) human form of a reply."""
    if cmd == "info":
        b = rep.get("board") or {}
        o = rep.get("owned") or {}
        out = ["daemon %s · api %s · %s" % (rep.get("daemon"), rep.get("api"),
                                            rep.get("socket"))]
        if b.get("online"):
            out.append("board  fw %s · PROTO %s · %s" % (b.get("fw"), b.get("proto"),
                                                        b.get("port")))
            caps = " ".join("%s %s" % (k, b[k]) for k in
                            ("leds", "glass", "ug", "screens", "w", "h", "pages",
                             "fonts", "notes", "tonemax", "line", "slots", "stack",
                             "txdrop", "up") if k in b)
            if caps:
                out.append("       " + caps)
        else:
            out.append("board  offline")
        out.append("owned  led %s · glass %s · sound %s · enc %s" % (
            ",".join(o.get("led") or []) or "-",
            ",".join(str(g) for g in (o.get("glass") or [])) or "-",
            "yes" if o.get("sound") else "no", "yes" if o.get("enc") else "no"))
        st = rep.get("settings") or {}
        if st:
            out.append("board  " + " ".join("%s %s" % kv for kv in sorted(st.items())))
        return "\n".join(out)
    if cmd == "palette":
        out = ["colors: " + " ".join("%s=%s" % kv for kv in sorted(rep["colors"].items())),
               "sounds: " + " ".join(sorted(rep["sounds"]))]
        out += ["warning: " + w for w in rep.get("warnings") or []]
        return "\n".join(out)
    if cmd == "settings":
        if rep.get("saved"):
            return "saved"
        return "\n".join("%s %s" % kv for kv in sorted(rep["settings"].items()))
    if cmd == "sound":
        return "hushed" if rep.get("duration_ms") == 0 else "ok (%d ms)" % rep["duration_ms"]
    n = len(rep.get("lines") or [])
    s = "ok (%d line%s %s)" % (n, "" if n == 1 else "s",
                               "confirmed" if rep.get("confirmed") else "queued")
    if rep.get("held_until"):
        s += " until %s" % time.strftime("%H:%M:%S", time.localtime(rep["held_until"]))
    if rep.get("truncated"):
        s += " — text clipped to %d chars" % TEXT_MAX
    return s


def _cli_events(args):
    kinds = [k for k in (args.filter or "").split(",") if k] or None
    try:
        for ev in events(kinds):
            sys.stdout.write(json.dumps(ev) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:                  # `glowbug events | head`
        try:                                 # keep the exit-time flush quiet
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except GlowbugError as e:
        sys.stderr.write("glowbug: %s\n" % e.message)
        return 3 if e.code == "no_daemon" else 1
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        run_daemon()                         # the LaunchAgent passes no args
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)           # bad usage: argparse exits 2
    cmd = args.cmd
    if cmd is None:
        parser.print_usage(sys.stderr)
        return 2
    legacy = {"install": install, "uninstall": uninstall, "status": status,
              "doctor": doctor, "rescue": rescue}
    if cmd in legacy:
        legacy[cmd]()
        return 0
    if cmd == "version":
        print("glowbug %s" % VERSION)
        return 0
    if cmd == "events":
        return _cli_events(args)
    try:
        req = _cli_request(args)
    except GlowbugError as e:                # usage, caught before the daemon
        parser.error(e.message)              # exits 2
    try:
        rep = _call(req)
    except GlowbugError as e:
        sys.stderr.write("glowbug: %s\n" % e.message)
        return 3 if e.code == "no_daemon" else 1
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        print(_human(cmd, rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
