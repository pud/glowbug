#!/usr/bin/env python3
"""Glowbug hardware conformance, fuzz and soak tool — PROTO 4 (frozen fw 2.0.0).

Drives a real board through every verb, selector form, bound and error path
in PROTOCOL.md, measures the timing the contract promises (FOR expiry, the
15 s silence release, the SAVE rate limit), records throughput (BLIT frames
per second) and stack headroom, fuzzes the line parser, and — with the
daemon running — soaks the whole stack through the daemon's socket.

    python3 tools/conformance.py                      # full run (~6 min + fuzz)
    python3 tools/conformance.py --quick              # ~30 s production spot check
    python3 tools/conformance.py --only tone,blit     # a subset
    python3 tools/conformance.py --no-touch           # skip the operator checks
    python3 tools/conformance.py --fuzz 50000         # more fuzz lines
    python3 tools/conformance.py --soak 24            # 24 h soak via the daemon
    python3 tools/conformance.py --json > result.json

The conformance run needs the port to itself: stop the daemon first
(`launchctl unload ~/Library/LaunchAgents/dev.glowbug.daemon.plist`; the
tool refuses to start while `launchctl list dev.glowbug.daemon` succeeds).
The soak is the opposite: it talks to the running daemon's unix socket and
never opens the serial port.

Output: one line per check — `PASS name (details)`, `FAIL name: reason`,
`SKIP name (why)` — then a summary with the measured numbers. Exit code 0
only when nothing failed. `--json` prints the same as one JSON object on
stdout (the per-check lines then go to stderr).

Not exercised here: the `DFU` verb and the page-0 bootloader paths
(bad-CRC app, 3 faults, the button). Those are covered by `make usbflash`
and the bench procedure, never by a tool that fuzzes the same port.

Stdlib only, Python 3.9+. The serial port is opened exactly the way the
daemon opens it (glowbug.open_serial / glowbug.find_port).
"""

import argparse
import base64
import json
import os
import queue
import random
import re
import select
import socket
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import glowbug  # noqa: E402  (find_port, open_serial, PALETTE, SOUNDS, SOCK_PATH)

DAEMON_LABEL = "dev.glowbug.daemon"

# ------------------------------------------------------------ the contract
# Numbers PROTOCOL.md promises in the INFO line (the freeze checklist says
# "PROTOCOL.md numbers match INFO output" — this is that check).
INFO_EXPECT = {
    "FW": "2.0.0", "PROTO": 4, "HW": 5, "LEDS": 10, "GLASS": 5, "UG": 5,
    "SCREENS": 5, "W": 128, "H": 32, "PAGES": 4, "FONTS": "7,16,24",
    "NOTES": 32, "TONEMAX": 5000, "LINE": 1024, "SLOTS": 32,
}
INFO_DYNAMIC = ("STACK", "HEAP", "TXDROP", "UP")      # ints, values vary
COLOR_NAMES = ("off white red green blue yellow orange violet cyan magenta "
               "thinking question permission error done unread subagent lamp").split()
SOUND_NAMES = "blip ding soft fanfare hello bye boot".split()
SETTING_MAX = {"brightness": 100, "ug_brightness": 100, "ug_mode": 2,
               "volume": 4, "chime": 2, "flip": 1}
STACK_MIN = 2048            # bytes of never-touched stack the fuzz must leave
ECHO_LATENCY_MAX = 0.5      # s, after every 1000 fuzz lines
BLIT_FPS_MIN = 15.0
OWN_MASK_LINE = "EVT OWN LED %03X GLASS %02X SOUND %d ENC %d"
OWN_RE = re.compile(r"^EVT OWN LED ([0-9A-F]{3}) GLASS ([0-9A-F]{2}) SOUND ([01]) ENC ([01])$")
HELLO_RE = re.compile(r"^EVT HELLO (\S+) PROTO 4 SLOTS 32$")
NOISE_RE = re.compile(r"^EVT (ENC [+-]?\d+|CLICK|HOLD|MENU [01])$")   # knob input
B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


# ------------------------------------------------------------ pure helpers
# (unit-tested offline in tests/test_conformance_offline.py)

def page_bytes(frame, page, glass=0):
    """One SSD1306 page (128 columns, one byte each, LSB = top) of a pattern
    that visibly moves with `frame` and differs per page and glass: a
    rolling single-pixel diagonal plus a solid 8-column block that sweeps."""
    out = bytearray(128)
    for i in range(128):
        v = 1 << ((i + frame + page * 2 + glass) % 8)
        if (i + frame * 4 + glass * 25 + page * 32) % 128 < 8:
            v = 0xFF
        out[i] = v
    return bytes(out)


def page_b64(frame, page, glass=0):
    """172 chars: one page, RFC 4648 with '=' padding (128 B -> 43 groups)."""
    return base64.b64encode(page_bytes(frame, page, glass)).decode("ascii")


def frame_b64(frame, glass=0):
    """684 chars: all four pages of one glass (512 B)."""
    raw = b"".join(page_bytes(frame, p, glass) for p in range(4))
    return base64.b64encode(raw).decode("ascii")


def b64_junk(n, rng=None):
    """n characters from the base64 alphabet (no padding) — right alphabet,
    arbitrary length, for the BLIT length checks."""
    rng = rng or random.Random(n)
    return "".join(rng.choice(B64_ALPHABET) for _ in range(n))


def parse_info(line):
    """'EVT INFO K V K V ...' -> {K: int|str}. None if it is not an INFO line
    or the pairs do not line up."""
    toks = line.split()
    if toks[:2] != ["EVT", "INFO"] or len(toks) % 2:
        return None
    d = {}
    for k, v in zip(toks[2::2], toks[3::2]):
        d[k] = int(v) if re.fullmatch(r"-?\d+", v) else v
    return d


def parse_own(line):
    """'EVT OWN LED 3FF GLASS 1F SOUND 1 ENC 0' -> dict of ints, else None."""
    m = OWN_RE.match(line)
    if not m:
        return None
    return {"led": int(m.group(1), 16), "glass": int(m.group(2), 16),
            "sound": int(m.group(3)), "enc": int(m.group(4))}


def own_line(led=0, glass=0, sound=0, enc=0):
    """The exact EVT OWN line the board prints for these masks."""
    return OWN_MASK_LINE % (led, glass, sound, enc)


def parse_launchctl_pid(text):
    """PID from `launchctl list <label>` output ('"PID" = 1234;'), or None
    when the job is loaded but not running."""
    m = re.search(r'"PID"\s*=\s*(\d+)', text or "")
    return int(m.group(1)) if m else None


# --- fuzz generators -------------------------------------------------------
# Three verbs must never reach the board from a fuzzer: DFU (exact line,
# reboots into the bootloader), RESET (reboots — trailing junk is ignored by
# its parser) and SAVE (a flash write; the page is rated 10k). Everything
# else is fair game: the contract says no line can have a partial effect.

def is_dangerous_segment(seg):
    """True for a line the fuzzer must not send (bytes, no terminator).
    The firmware sees a C string, so everything from the first NUL on is
    invisible to it: b'RESET\\x00junk' IS a RESET."""
    seg = seg.split(b"\x00", 1)[0]
    if seg == b"DFU":
        return True
    first = seg.lstrip(b" ").split(b" ", 1)[0]
    return first in (b"RESET", b"SAVE")


def _first_byte_neutral(seg):
    """Defuse a dangerous segment without changing its length."""
    i = len(seg) - len(seg.lstrip(b" "))
    return seg[:i] + b"X" + seg[i + 1:]


def scrub_raw(chunk):
    """Split a byte blob on CR/LF and neutralise every dangerous segment;
    the length and every terminator are preserved."""
    out = bytearray()
    seg = bytearray()
    for b in chunk:
        if b in (0x0A, 0x0D):
            s = bytes(seg)
            out += _first_byte_neutral(s) if is_dangerous_segment(s) else s
            out.append(b)
            seg = bytearray()
        else:
            seg.append(b)
    s = bytes(seg)
    out += _first_byte_neutral(s) if is_dangerous_segment(s) else s
    return bytes(out)


_FUZZ_VERBS = ("HELLO INFO ECHO REPLY OWN RELEASE LED SOUND TONE HUSH TEXT BIG CLEAR "
               "BLIT CONTRAST INVERT SCREEN GET SET PING WAKE BRIGHT UG SLOT FROB "
               "hello Led own DFUX RESETX SAVEX").split()
_FUZZ_ARGS = ("0 1 2 3 4 5 9 10 99 0,4,7 1,3 ALL GLASS UG ENC SOUND LED red white off "
              "GGGGGG 00FF00 ff00ff SET FADE PULSE BLINK OFF FOR 1000 65535 65536 16 15 "
              "86400000 86400001 2700:100 0:5000,2700:1 50:1,20000:1 ding fanfare nope "
              "VOL 4 5 brightness ug_brightness ug_mode volume chime flip ON OFF | "
              "STATE thinking question NAME t DETAIL d SUB SID abcdefgh MODE echo lamp "
              "COLOR - + 0x10 -1 2147483648 4294967296 99999999999999999999").split()


def _fuzz_len(rng):
    r = rng.random()
    if r < 0.3:
        return rng.randint(0, 20)
    if r < 0.7:
        return rng.randint(20, 300)
    return rng.randint(300, 2000)


def fuzz_line(rng):
    """One terminated fuzz line (bytes). Kinds: random printable ASCII,
    random binary incl. NUL and embedded CR/LF, and mutants built from real
    verbs with shuffled arguments. Never a dangerous segment."""
    kind = rng.random()
    if kind < 0.35:
        n = _fuzz_len(rng)
        body = bytes(rng.choice(b" !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                b"[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~") for _ in range(n))
    elif kind < 0.6:
        n = _fuzz_len(rng)
        body = rng.randbytes(n)
    else:
        toks = [rng.choice(_FUZZ_VERBS)]
        for _ in range(rng.randint(0, 8)):
            r = rng.random()
            if r < 0.7:
                toks.append(rng.choice(_FUZZ_ARGS))
            elif r < 0.85:
                toks.append(str(rng.randint(-5, 70000)))
            elif r < 0.95:
                toks.append(b64_junk(rng.choice((171, 172, 173, 683, 684, 685, 10)), rng))
            else:
                toks.append("x" * rng.randint(1, 1100))
        sep = " " if rng.random() < 0.9 else "  "
        body = sep.join(toks).encode("ascii")
        if rng.random() < 0.1:
            body = b" " + body
    term = rng.choice((b"\n", b"\r", b"\r\n", b"\n"))
    return scrub_raw(body) + term


def fuzz_lines(rng, n):
    for _ in range(n):
        yield fuzz_line(rng)


def fmt_secs(s):
    s = int(s)
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


# ------------------------------------------------------------ the port
class Fail(Exception):
    pass


class Skip(Exception):
    pass


class PortGone(Exception):
    pass


class Board:
    """The serial port + a reader thread that splits lines into a queue.

    send()    -> monotonic time the write completed
    expect()  -> (match, t_received, other_lines) or Fail on timeout
    barrier() -> the lines the board printed before it echoed our token
    ask()     -> send + barrier (the whole reply to one line, in order)
    Knob events (EVT ENC/CLICK/HOLD/MENU) are filtered out of ask()/barrier()
    results and counted as noise; expect() sees everything."""

    def __init__(self, port, verbose=False):
        self.port = port
        self.verbose = verbose
        self.fd = None
        self.q = queue.Queue()
        self.gone = None                 # (monotonic time, reason) once the port died
        self._stop = threading.Event()
        self._thread = None
        self._seq = 0
        self.noise = 0
        self.noise_lines = []
        self.tx_lines = 0
        self.tx_bytes = 0
        self.last_tx = time.monotonic()

    # ---- lifecycle ----
    def open(self, drain=0.3):
        self.fd = glowbug.open_serial(self.port)
        self.gone = None
        self._stop.clear()
        self.q = queue.Queue()
        self._thread = threading.Thread(target=self._reader, name="reader", daemon=True)
        self._thread.start()
        if drain:
            self.drain(drain)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def is_open(self):
        return self.fd is not None and self.gone is None

    def _mark_gone(self, why):
        if self.gone is None:
            self.gone = (time.monotonic(), str(why))
            if self.verbose:
                sys.stderr.write("   [port gone: %s]\n" % why)

    def _reader(self):
        buf = b""
        fd = self.fd
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.05)
            except (OSError, ValueError) as e:
                self._mark_gone(e)
                return
            if not r:
                continue
            try:
                data = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError as e:
                self._mark_gone(e)
                return
            if not data:                      # EOF: the device went away
                self._mark_gone("EOF")
                return
            now = time.monotonic()
            buf += data
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                seg, buf = buf[:m.start()], buf[m.end():]
                if seg:
                    line = seg.decode("ascii", "replace")
                    if self.verbose:
                        sys.stderr.write("   <- %s\n" % line[:110])
                    self.q.put((now, line))

    # ---- writing ----
    def write(self, data, timeout=5.0):
        """Write all of `data`, waiting out EAGAIN (the board NAKs while it
        is busy). Returns the monotonic time the last byte was accepted."""
        if self.gone is not None or self.fd is None:
            raise PortGone("port is gone (%s)" % (self.gone[1] if self.gone else "closed"))
        view = memoryview(data)
        deadline = time.monotonic() + timeout
        while len(view):
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Fail("write stalled for %.1f s (%d B left) — board wedged?"
                               % (timeout, len(view)))
                try:
                    select.select([], [self.fd], [], min(left, 0.5))
                except (OSError, ValueError) as e:
                    self._mark_gone(e)
                    raise PortGone(str(e))
            except OSError as e:
                self._mark_gone(e)
                raise PortGone(str(e))
        self.tx_bytes += len(data)
        self.last_tx = time.monotonic()
        return self.last_tx

    def send(self, line):
        """One line (str or bytes), terminated for you."""
        if isinstance(line, str):
            line = line.encode("latin-1")
        if not line.endswith((b"\n", b"\r")):
            line += b"\n"
        if self.verbose:
            sys.stderr.write("   -> %s\n" % line[:110].decode("latin-1").rstrip("\r\n"))
        self.tx_lines += 1
        return self.write(line)

    # ---- reading ----
    def _get(self, timeout):
        try:
            return self.q.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def expect(self, pattern, timeout=2.0, keepalive=None):
        """Wait for a line matching `pattern` (str regex or compiled).
        Returns (match, t_received, other_lines_before_it). `keepalive`
        = seconds of host silence after which a PING is sent while waiting
        (operator steps: the board releases everything after 15 s without
        a line); None = stay silent."""
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        others = []
        deadline = time.monotonic() + timeout
        while True:
            slice_ = deadline - time.monotonic()
            if keepalive is not None:
                if time.monotonic() - self.last_tx >= keepalive:
                    self.send("PING")
                slice_ = min(slice_, keepalive / 4.0)
            item = self._get(slice_)
            if item is None:
                if self.gone is not None:
                    raise PortGone("port died while waiting for %r (%s)" % (rx.pattern, self.gone[1]))
                if time.monotonic() >= deadline:
                    raise Fail("no %r within %.1f s (got %s)" % (rx.pattern, timeout, others))
                continue
            t, line = item
            m = rx.match(line)
            if m:
                return m, t, others
            others.append(line)

    def drain(self, seconds):
        """Everything the board says in the next `seconds` (no filtering)."""
        out = []
        deadline = time.monotonic() + seconds
        while True:
            item = self._get(deadline - time.monotonic())
            if item is None:
                if time.monotonic() >= deadline:
                    return out
                continue
            out.append(item[1])

    def barrier(self, timeout=2.0):
        """ECHO a fresh token; return the (non-noise) lines printed before
        the echo came back — the complete reply to everything sent before."""
        self._seq += 1
        token = "b%d" % self._seq
        self.send("ECHO " + token)
        _, _, others = self.expect(re.compile(r"^EVT ECHO %s$" % re.escape(token)), timeout)
        keep = []
        for line in others:
            if NOISE_RE.match(line):
                self.noise += 1
                self.noise_lines.append(line)
            else:
                keep.append(line)
        return keep

    def ask(self, line, timeout=2.0):
        self.send(line)
        return self.barrier(timeout)


# ------------------------------------------------------------ check plumbing
CHECKS = []                       # ordered [(name, fn, meta)]
BOOTSTRAP = ("hello", "reply", "info")          # always run first, in order
FINAL = ("settings_restore", "reset")           # always last, in order
QUICK = ("hello", "reply", "info", "own_release", "led_ops", "sound", "text",
         "blit", "settings", "settings_restore")


def check(name, doc, quick=False, interactive=False, seconds=None):
    def deco(fn):
        CHECKS.append((name, fn, {"doc": doc, "quick": quick, "interactive": interactive,
                                  "seconds": seconds}))
        return fn
    return deco


def short(cmd, n=70):
    s = cmd if isinstance(cmd, str) else cmd.decode("latin-1", "replace")
    s = s.rstrip("\r\n")
    return s if len(s) <= n else s[:n - 3] + "..."


class Ctx:
    def __init__(self, board, args):
        self.board = board
        self.args = args
        self.quick = args.quick
        self.fw = None
        self.info0 = None
        self.settings0 = {}
        self.min_stack = None
        self.measure = {}
        self.results = []
        self.rng = random.Random(args.seed)

    # ---- assertions on whole replies ----
    def X(self, cmd, *expected, **kw):
        """Send `cmd`; the board's complete reply must equal `expected`
        (strings compared exactly, compiled regexes matched). Returns the
        reply lines."""
        timeout = kw.get("timeout", 2.0)
        got = self.board.ask(cmd, timeout)
        if not self.same(got, expected):
            want = [e.pattern if hasattr(e, "pattern") else e for e in expected]
            raise Fail("%r -> %s (want %s)" % (short(cmd), got, want))
        return got

    @staticmethod
    def same(got, expected):
        if len(got) != len(expected):
            return False
        for g, e in zip(got, expected):
            if hasattr(e, "match"):
                if not e.match(g):
                    return False
            elif g != e:
                return False
        return True

    def info(self):
        """INFO -> dict (records the stack low-water mark)."""
        lines = self.board.ask("INFO")
        infos = [parse_info(x) for x in lines if x.startswith("EVT INFO ")]
        if not infos or infos[0] is None:
            raise Fail("INFO -> %s" % lines)
        d = infos[0]
        self.record_info(d)
        return d

    def record_info(self, d):
        st = d.get("STACK")
        if isinstance(st, int):
            self.min_stack = st if self.min_stack is None else min(self.min_stack, st)

    def get(self, key):
        lines = self.board.ask("GET " + key)
        for x in lines:
            m = re.match(r"^EVT SET %s (\d+)$" % re.escape(key), x)
            if m:
                return int(m.group(1))
        raise Fail("GET %s -> %s" % (key, lines))

    def unmute(self):
        """Make sure SOUND/TONE are not refused as muted; returns a restore fn."""
        v = self.get("volume")
        if v != 0:
            return lambda: None
        self.X("SET volume 2", "OK SET")
        return lambda: self.X("SET volume 0", "OK SET")

    def prompt(self, msg):
        if self.args.no_touch:
            raise Skip("operator step (--no-touch)")
        sys.stderr.write("\n   >>> %s\n" % msg)
        sys.stderr.flush()

    def reopen(self, wait=5.0, settle=0.5):
        """Wait for the port to come back (same path, or the Glowbug wherever
        ioreg finds it), open it, drain. Returns seconds waited."""
        b = self.board
        b.close()
        t0 = time.monotonic()
        while time.monotonic() - t0 < wait:
            port = b.port if os.path.exists(b.port) else glowbug.find_port()
            if port:
                time.sleep(settle)
                try:
                    b.port = port
                    b.open()
                    return time.monotonic() - t0
                except OSError:
                    pass
            time.sleep(0.1)
        raise Fail("port did not come back within %.1f s" % wait)

    def wait_gone(self, timeout, keepalive=None):
        """Block until the reader sees the port die; returns the time it did.
        With `keepalive`, PINGs meanwhile so ownership survives the wait."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.board.gone is not None:
                return self.board.gone[0]
            if not os.path.exists(self.board.port):
                self.board._mark_gone("path vanished")
                return self.board.gone[0]
            if keepalive is not None and time.monotonic() - self.board.last_tx >= keepalive:
                try:
                    self.board.send("PING")
                except (PortGone, Fail):
                    return self.board.gone[0] if self.board.gone else time.monotonic()
            time.sleep(0.05)
        raise Fail("port still there after %.1f s" % timeout)


# ------------------------------------------------------------ checks
# Bootstrap: the session the contract describes — HELLO (session reset),
# REPLY ON, INFO.

@check("hello", "HELLO answers EVT HELLO <fw> PROTO 4 SLOTS 32 and is a session reset (REPLY OFF)",
       quick=True)
def chk_hello(ctx):
    b = ctx.board
    lines = b.ask("HELLO")
    hello = [x for x in lines if HELLO_RE.match(x)]
    if len(hello) != 1:
        raise Fail("HELLO -> %s" % lines)
    ctx.fw = HELLO_RE.match(hello[0]).group(1)
    extra = [x for x in lines if x != hello[0]]
    # a session reset releases everything: at most one all-zero EVT OWN
    if extra and extra != [own_line()]:
        raise Fail("HELLO printed more than the hello: %s" % lines)
    # REPLY is OFF now: INFO answers without OK
    lines = b.ask("INFO")
    if any(x.startswith("OK ") for x in lines) or not any(x.startswith("EVT INFO ") for x in lines):
        raise Fail("after HELLO, INFO -> %s (REPLY should be OFF)" % lines)
    return "fw %s, session reset, REPLY OFF" % ctx.fw


@check("reply", "REPLY ON -> OK REPLY; OFF is silent; bad arg -> ERR REPLY arg", quick=True)
def chk_reply(ctx):
    ctx.X("REPLY ON", "OK REPLY")
    ctx.X("REPLY OFF")
    ctx.X("INFO", re.compile(r"^EVT INFO "))          # no OK while OFF
    ctx.X("REPLY", "ERR REPLY arg")
    ctx.X("REPLY MAYBE", "ERR REPLY arg")
    ctx.X("REPLY ON", "OK REPLY")
    ctx.X("REPLY ON", "OK REPLY")                     # idempotent
    return "ON/OFF/arg"


@check("info", "INFO: every documented key present with the documented value; STACK/HEAP/TXDROP/UP ints",
       quick=True)
def chk_info(ctx):
    lines = ctx.X("INFO", re.compile(r"^EVT INFO "), "OK INFO")
    d = parse_info(lines[0])
    if d is None:
        raise Fail("INFO line does not parse as key/value pairs: %r" % lines[0])
    bad = []
    for k, v in INFO_EXPECT.items():
        if d.get(k) != v:
            bad.append("%s=%r (want %r)" % (k, d.get(k), v))
    for k in INFO_DYNAMIC:
        if not isinstance(d.get(k), int):
            bad.append("%s=%r (want int)" % (k, d.get(k)))
    if bad:
        raise Fail("; ".join(bad))
    if d["STACK"] < STACK_MIN:
        raise Fail("STACK %d < %d" % (d["STACK"], STACK_MIN))
    ctx.record_info(d)
    ctx.info0 = d
    return "STACK %d HEAP %d TXDROP %d UP %d" % (d["STACK"], d["HEAP"], d["TXDROP"], d["UP"])


@check("tokenizer", "exact-match verbs, legacy prefix quirks (PINGX, WAKEx), ERR <verb> unknown "
                    "with the verb capped at 16 printable chars, empty lines ignored, ERR before ECHO")
def chk_tokenizer(ctx):
    b = ctx.board
    ctx.X("FROB", "ERR FROB unknown")
    ctx.X("PINGX")                          # a PING (frozen prefix quirk): silent, ECHO still answers
    ctx.X("PING")
    ctx.X("WAKEUP")                         # WAKE by prefix
    ctx.X("HELLOX", "ERR HELLOX unknown")   # HELLO is exact
    ctx.X("hello", "ERR hello unknown")     # verbs are upper case
    ctx.X("led 0 SET red", "ERR led unknown")
    ctx.X("ABCDEFGHIJKLMNOPQRST", "ERR ABCDEFGHIJKLMNOP unknown")   # echo capped at 16
    ctx.X(b"AB\x01C\n", "ERR AB?C unknown")                          # non-printable -> ?
    ctx.X(b"\t", "ERR ? unknown")
    b.send(b"\n\n\r\n")                     # empty lines are ignored
    got = b.barrier()
    if got:
        raise Fail("empty lines answered: %s" % got)
    # ordering: the ERR of a line precedes the echo of the line after it
    b.send(b"FROB\nECHO ord1\n")
    _, _, before = b.expect(r"^EVT ECHO ord1$")
    if before != ["ERR FROB unknown"]:
        raise Fail("ERR did not precede the echo: %s" % before)
    return "FROB/PINGX/HELLOX/case/16-char cap/ordering"


@check("echo", "ECHO <token>: <=16 printable chars echoed; longer, missing or non-printable -> ERR ECHO token")
def chk_echo(ctx):
    ctx.X("ECHO abcdefghijklmnop", "EVT ECHO abcdefghijklmnop")
    ctx.X("ECHO abcdefghijklmnopq", "ERR ECHO token")
    ctx.X("ECHO", "ERR ECHO token")
    ctx.X(b"ECHO a\x01b\n", "ERR ECHO token")
    ctx.X("ECHO ~", "EVT ECHO ~")
    return "16 ok / 17 token / missing / non-printable"


@check("own_release", "every OWN/RELEASE form and the EVT OWN masks; bare OWN query; sel/arg/for errors; "
                      "untimed re-claim makes a timed claim indefinite", quick=True)
def chk_own_release(ctx):
    X = ctx.X
    X("RELEASE ALL", "OK RELEASE")           # may or may not report a change; normalise
    ctx.board.barrier()
    X("OWN", own_line())                     # bare query: EVT OWN, no OK
    X("OWN LED 0", "OK OWN", own_line(0x001))
    X("OWN LED 0,4,7", "OK OWN", own_line(0x091))
    X("OWN LED GLASS", "OK OWN", own_line(0x09F))
    X("OWN LED UG", "OK OWN", own_line(0x3FF))
    X("OWN GLASS 1,3", "OK OWN", own_line(0x3FF, 0x0A))
    X("OWN GLASS ALL", "OK OWN", own_line(0x3FF, 0x1F))
    X("OWN SOUND", "OK OWN", own_line(0x3FF, 0x1F, 1, 0))
    X("OWN ENC", "OK OWN", own_line(0x3FF, 0x1F, 1, 1))
    X("OWN", own_line(0x3FF, 0x1F, 1, 1))
    X("RELEASE LED 0", "OK RELEASE", own_line(0x3FE, 0x1F, 1, 1))
    X("RELEASE LED UG", "OK RELEASE", own_line(0x01E, 0x1F, 1, 1))
    X("RELEASE GLASS 1", "OK RELEASE", own_line(0x01E, 0x1D, 1, 1))
    X("RELEASE SOUND", "OK RELEASE", own_line(0x01E, 0x1D, 0, 1))
    X("RELEASE ENC", "OK RELEASE", own_line(0x01E, 0x1D, 0, 0))
    X("RELEASE ALL", "OK RELEASE", own_line())
    X("RELEASE ALL", "OK RELEASE")           # nothing changed: no EVT OWN
    X("OWN ALL", "OK OWN", own_line(0x3FF, 0x1F, 1, 1))
    X("RELEASE GLASS ALL", "OK RELEASE", own_line(0x3FF, 0, 1, 1))
    X("RELEASE LED ALL", "OK RELEASE", own_line(0, 0, 1, 1))
    X("RELEASE ALL", "OK RELEASE", own_line())
    # errors (no effect: the query afterwards is still all zero)
    X("OWN LED 10", "ERR OWN sel")
    X("OWN LED 0,10", "ERR OWN sel")
    X("OWN LED", "ERR OWN sel")
    X("OWN GLASS 5", "ERR OWN sel")
    X("OWN GLASS x", "ERR OWN sel")
    X("OWN FOO", "ERR OWN arg")
    X("OWN LED 0 XYZ", "ERR OWN arg")
    X("OWN LED 0 FOR 0", "ERR OWN for")
    X("OWN LED 0 FOR 86400001", "ERR OWN for")
    X("OWN LED 0 FOR", "ERR OWN for")
    X("OWN LED 0 FOR -5", "ERR OWN for")
    X("RELEASE LED 10", "ERR RELEASE sel")
    X("RELEASE FOO", "ERR RELEASE arg")
    X("OWN", own_line())
    if ctx.quick:
        return "all forms, masks, errors"
    X("OWN LED 0 FOR 86400000", "OK OWN", own_line(0x001))     # max FOR accepted
    b = ctx.board
    b.send("OWN GLASS 2 FOR 1")                                 # min FOR accepted ...
    b.expect(re.compile("^" + re.escape(own_line(0x001, 0x04)) + "$"), 1.0)
    b.expect(re.compile("^" + re.escape(own_line(0x001, 0x00)) + "$"), 1.0)   # ... and expires
    b.barrier()
    # an untimed re-claim of a timed LED makes it indefinite again
    X("OWN LED 1 FOR 800", "OK OWN", own_line(0x003))
    X("OWN LED 1", "OK OWN", own_line(0x003))
    time.sleep(1.3)
    X("OWN", own_line(0x003))
    X("RELEASE ALL", "OK RELEASE", own_line())
    return "all forms, masks, errors, FOR bounds, re-claim"


@check("led_ops", "every LED selector form x every op; colors (18 names, hex either case); bounds "
                  "(index 10, GGGGGG, period 15/65536); NOTOWNED for explicit lists, group = owned subset",
       quick=True)
def chk_led_ops(ctx):
    X = ctx.X
    X("OWN LED ALL", "OK OWN", own_line(0x3FF))
    sels = ("3", "0,4,7", "ALL", "GLASS", "UG")
    ops = ("SET red", "SET 00ff00", "SET 00FF00", "OFF", "FADE blue 500",
           "PULSE red blue 1000", "BLINK white off 200")
    n = 0
    for s in sels:
        for op in ops:
            X("LED %s %s" % (s, op), "OK LED")
            n += 1
    for c in COLOR_NAMES:
        X("LED ALL SET %s" % c, "OK LED")
    # bounds
    X("LED 10 SET red", "ERR LED sel")
    X("LED 0,10 SET red", "ERR LED sel")
    X("LED x SET red", "ERR LED sel")
    X("LED", "ERR LED sel")
    X("LED 0 SET GGGGGG", "ERR LED color")
    X("LED 0 SET 12345", "ERR LED color")
    X("LED 0 SET", "ERR LED color")
    X("LED 0 SET Red", "ERR LED color")           # names are lower case
    X("LED 0 FADE red 15", "ERR LED range")
    X("LED 0 FADE red 65536", "ERR LED range")
    X("LED 0 PULSE red blue 15", "ERR LED range")
    X("LED 0 BLINK red blue 65536", "ERR LED range")
    X("LED 0 FADE red", "ERR LED range")
    X("LED 0 PULSE red", "ERR LED color")
    X("LED 0 FADE red 16", "OK LED")
    X("LED 0 PULSE red blue 65535", "OK LED")
    X("LED 0 BLINK red blue 16", "OK LED")
    X("LED 0 GLOW red", "ERR LED op")
    X("LED 0", "ERR LED op")
    X("LED 0 SET red extra", "ERR LED arg")
    X("LED 0 OFF now", "ERR LED arg")
    X("LED ALL OFF", "OK LED")
    # ownership
    X("RELEASE LED ALL", "OK RELEASE", own_line())
    X("LED 0 SET red", "ERR LED NOTOWNED")
    X("LED ALL SET red", "ERR LED NOTOWNED")      # group with nothing owned
    X("OWN LED 0", "OK OWN", own_line(0x001))
    X("LED 0,1 SET red", "ERR LED NOTOWNED")      # explicit list must be fully owned
    X("LED GLASS SET red", "OK LED")              # group = the owned subset
    X("LED UG SET red", "ERR LED NOTOWNED")       # group with none owned in it
    X("LED 0 SET red", "OK LED")
    X("RELEASE ALL", "OK RELEASE", own_line())
    return "%d selector x op combos, %d colors, bounds, NOTOWNED" % (n, len(COLOR_NAMES))


@check("sound", "SOUND: all 7 names, VOL 0-4, VOL 5 -> arg, unknown name -> ERR SOUND name, HUSH",
       quick=True)
def chk_sound(ctx):
    X = ctx.X
    restore = ctx.unmute()
    try:
        for name in SOUND_NAMES:
            X("SOUND %s" % name, "OK SOUND")
            time.sleep(0.25 if ctx.quick else 0.6)
        X("SOUND ding VOL 0", "OK SOUND")
        X("SOUND ding VOL 4", "OK SOUND")
        X("SOUND ding VOL 5", "ERR SOUND arg")
        X("SOUND ding LOUD", "ERR SOUND arg")
        X("SOUND ding VOL", "ERR SOUND arg")
        X("SOUND nope", "ERR SOUND name")
        X("SOUND Ding", "ERR SOUND name")
        X("SOUND", "ERR SOUND name")
        X("HUSH", "OK HUSH")
    finally:
        restore()
    return "7 names, VOL, name/arg errors, HUSH"


@check("tone", "TONE bounds: 32 notes ok / 33 -> count; hz 30 or 20001 -> range; ms 0 -> range; "
               "total 5001 -> total; rests; malformed -> notes; VOL")
def chk_tone(ctx):
    X = ctx.X
    restore = ctx.unmute()
    try:
        X("TONE 2700:100", "OK TONE")
        X("TONE 0:100", "OK TONE")                        # a rest
        X("TONE 50:10,20000:10", "OK TONE")               # hz bounds
        X("TONE " + ",".join(["2700:10"] * 32), "OK TONE")
        X("TONE " + ",".join(["2700:10"] * 33), "ERR TONE count")
        X("TONE 30:100", "ERR TONE range")
        X("TONE 49:100", "ERR TONE range")
        X("TONE 20001:100", "ERR TONE range")
        X("TONE 2700:0", "ERR TONE range")
        X("TONE 2700:5001", "ERR TONE range")
        X("TONE 2700:5000,2700:1", "ERR TONE total")
        X("TONE " + ",".join(["2700:200"] * 25) + ",0:1", "ERR TONE total")   # 5001 over 26 notes
        X("TONE 2700:2500,0:2500", "OK TONE")             # total exactly 5000
        X("HUSH", "OK HUSH")
        X("TONE abc", "ERR TONE notes")
        X("TONE 2700", "ERR TONE notes")
        X("TONE 2700:", "ERR TONE notes")
        X("TONE 2700:100,", "ERR TONE notes")
        X("TONE :100", "ERR TONE notes")
        X("TONE 2700:100;", "ERR TONE notes")
        X("TONE -1:100", "ERR TONE notes")
        X("TONE", "ERR TONE notes")
        X("TONE 2700:100 VOL 9", "ERR TONE arg")
        X("TONE 2700:100 LOUD", "ERR TONE arg")
        X("TONE 2700:100 VOL 0", "OK TONE")
        X("TONE 2700:100 VOL 4", "OK TONE")
        time.sleep(0.2)
        X("HUSH", "OK HUSH")
    finally:
        restore()
    return "count/range/total/notes/arg bounds"


@check("muted", "volume 0: SOUND -> ERR SOUND muted, TONE -> ERR TONE muted, even with VOL; restored after")
def chk_muted(ctx):
    X = ctx.X
    v0 = ctx.get("volume")
    X("SET volume 0", "OK SET")
    try:
        X("SOUND ding", "ERR SOUND muted")
        X("SOUND ding VOL 3", "ERR SOUND muted")
        X("TONE 2700:100", "ERR TONE muted")
        X("TONE 2700:100 VOL 3", "ERR TONE muted")
    finally:
        X("SET volume %d" % v0, "OK SET")
    if ctx.get("volume") != v0:
        raise Fail("volume not restored")
    return "SOUND/TONE refused while volume 0; VOL does not override; restored to %d" % v0


@check("text", "TEXT/BIG/CLEAR/CONTRAST/INVERT/SCREEN on an owned glass; long text clipped not rejected; "
               "NOTOWNED for unowned glass and lists; sel/range/arg bounds", quick=True)
def chk_text(ctx):
    X = ctx.X
    X("OWN GLASS 0", "OK OWN", own_line(0, 0x01))
    X("TEXT 0 Hello|World", "OK TEXT")
    X("TEXT 0 " + "x" * 40 + "|" + "y" * 40, "OK TEXT")          # clipped to 21, never refused
    X(b"TEXT 0 caf\xc3\xa9 \x01|\x7f\n", "OK TEXT")             # non-ASCII renders '?'
    X("TEXT 0 one", "OK TEXT")                                   # single line
    X("TEXT ALL every owned", "OK TEXT")
    X("BIG 0 Hi", "OK BIG")
    X("BIG ALL " + "z" * 30, "OK BIG")
    X("CLEAR 0", "OK CLEAR")
    X("CLEAR ALL", "OK CLEAR")
    X("CONTRAST 0 128", "OK CONTRAST")
    X("CONTRAST ALL 0", "OK CONTRAST")
    X("CONTRAST 0 255", "OK CONTRAST")
    X("INVERT 0 1", "OK INVERT")
    X("INVERT ALL 0", "OK INVERT")
    X("SCREEN 0 OFF", "OK SCREEN")
    X("SCREEN ALL ON", "OK SCREEN")
    if not ctx.quick:
        X("TEXT 1 x", "ERR TEXT NOTOWNED")
        X("TEXT 0,1 x", "ERR TEXT NOTOWNED")
        X("TEXT 5 x", "ERR TEXT sel")
        X("TEXT x x", "ERR TEXT sel")
        X("TEXT", "ERR TEXT sel")
        X("BIG 1 Hi", "ERR BIG NOTOWNED")
        X("BIG 5 Hi", "ERR BIG sel")
        X("CLEAR 1", "ERR CLEAR NOTOWNED")
        X("CLEAR 9", "ERR CLEAR sel")
        X("CONTRAST 0 256", "ERR CONTRAST range")
        X("CONTRAST 0 -1", "ERR CONTRAST range")
        X("CONTRAST 0", "ERR CONTRAST range")
        X("CONTRAST 0 1 x", "ERR CONTRAST arg")
        X("CONTRAST 1 1", "ERR CONTRAST NOTOWNED")
        X("INVERT 0 2", "ERR INVERT range")
        X("INVERT 0", "ERR INVERT range")
        X("INVERT 1 1", "ERR INVERT NOTOWNED")
        X("SCREEN 0 MAYBE", "ERR SCREEN arg")
        X("SCREEN 0", "ERR SCREEN arg")
        X("SCREEN 0 ON x", "ERR SCREEN arg")
        X("SCREEN 1 ON", "ERR SCREEN NOTOWNED")
        X("SCREEN 0 on", "ERR SCREEN arg")
    X("TEXT 0 bye", "OK TEXT")
    X("RELEASE GLASS ALL", "OK RELEASE", own_line())
    X("TEXT ALL x", "ERR TEXT NOTOWNED")                          # group with nothing owned
    X("CONTRAST 0 0", "ERR CONTRAST NOTOWNED")
    return "paint verbs on glass 0, clipping, NOTOWNED, bounds"


@check("blit", "BLIT one page (0-3) and ALL on an owned glass; 171/173/683/685 chars -> len; "
               "bad char or misplaced '=' -> b64; page 4 -> page; ALL/5 -> sel; unowned -> NOTOWNED",
       quick=True)
def chk_blit(ctx):
    X = ctx.X
    X("OWN GLASS 2", "OK OWN", own_line(0, 0x04))
    if ctx.quick:
        X("BLIT 2 0 " + page_b64(0, 0, 2), "OK BLIT")
        X("RELEASE ALL", "OK RELEASE", own_line())
        return "one page"
    for p in range(4):
        X("BLIT 2 %d %s" % (p, page_b64(1, p, 2)), "OK BLIT")
    X("BLIT 2 ALL " + frame_b64(2, 2), "OK BLIT")
    for n in (171, 173):
        X("BLIT 2 0 " + b64_junk(n), "ERR BLIT len")
    for n in (683, 685):
        X("BLIT 2 ALL " + b64_junk(n), "ERR BLIT len")
    X("BLIT 2 0 " + b64_junk(683), "ERR BLIT len")       # right length for the other form
    X("BLIT 2 ALL " + b64_junk(172), "ERR BLIT len")
    good = page_b64(0, 0, 2)
    X("BLIT 2 0 " + good[:5] + "*" + good[6:], "ERR BLIT b64")
    X("BLIT 2 0 " + good[:5] + "=" + good[6:], "ERR BLIT b64")   # '=' only as the last char
    X("BLIT 2 0 " + good[:5] + " " + good[6:], "ERR BLIT arg")   # a space = trailing junk
    X("BLIT 2 4 " + good, "ERR BLIT page")
    X("BLIT 2 x " + good, "ERR BLIT page")
    X("BLIT 2", "ERR BLIT page")
    X("BLIT 2 0", "ERR BLIT arg")
    X("BLIT 2 0 " + good + " junk", "ERR BLIT arg")
    X("BLIT ALL 0 " + good, "ERR BLIT sel")
    X("BLIT 5 0 " + good, "ERR BLIT sel")
    X("BLIT", "ERR BLIT sel")
    X("BLIT 1 0 " + good, "ERR BLIT NOTOWNED")
    X("BLIT 2 3 " + page_b64(3, 3, 2), "OK BLIT")          # still fine after the errors
    X("RELEASE ALL", "OK RELEASE", own_line())
    X("BLIT 2 0 " + good, "ERR BLIT NOTOWNED")
    return "pages 0-3 + ALL, len/b64/page/arg/sel/NOTOWNED"


@check("settings", "GET every key (EVT SET k v + OK GET); SET bounds per key -> range; unknown key -> key; "
                   "flip takes effect and is put back", quick=True)
def chk_settings(ctx):
    X = ctx.X
    vals = {}
    for k in SETTING_MAX:
        lines = X("GET " + k, re.compile(r"^EVT SET %s \d+$" % k), "OK GET")
        vals[k] = int(lines[0].split()[-1])
        if vals[k] > SETTING_MAX[k]:
            raise Fail("%s=%d above its documented max %d" % (k, vals[k], SETTING_MAX[k]))
    if not ctx.settings0:
        ctx.settings0 = dict(vals)
    if ctx.quick:
        return " ".join("%s=%d" % kv for kv in sorted(vals.items()))
    X("GET nokey", "ERR GET key")
    X("GET", "ERR GET key")
    X("GET Brightness", "ERR GET key")
    X("SET nokey 1", "ERR SET key")
    X("SET", "ERR SET key")
    for k, mx in SETTING_MAX.items():
        X("SET %s %d" % (k, mx + 1), "ERR SET range")
        X("SET %s -1" % k, "ERR SET range")
        X("SET %s" % k, "ERR SET range")
        X("SET %s 1x" % k, "ERR SET range")
        X("SET %s %d" % (k, mx), "OK SET")           # max accepted (RAM only)
        if ctx.get(k) != mx:
            raise Fail("SET %s %d did not stick" % (k, mx))
        X("SET %s 0" % k, "OK SET")
        X("SET %s %d" % (k, vals[k]), "OK SET")
        if ctx.get(k) != vals[k]:
            raise Fail("%s not restored" % k)
    return "6 keys: " + " ".join("%s=%d" % kv for kv in sorted(vals.items()))


@check("save", "SAVE writes; a second SAVE within 10 s -> ERR SAVE ratelimit <s>; after the wait it "
               "writes; an unchanged SAVE is OK SAVE (no-op). Flash ends as it started (2 writes)",
       seconds=12)
def chk_save(ctx):
    X = ctx.X
    b0 = ctx.get("brightness")
    bx = b0 - 1 if b0 > 0 else b0 + 1
    rl = re.compile(r"^ERR SAVE ratelimit (\d+)$")
    X("SET brightness %d" % bx, "OK SET")
    got = ctx.board.ask("SAVE")
    if got != ["OK SAVE"]:
        m = rl.match(got[0]) if got else None
        if not m:
            raise Fail("first SAVE -> %s" % got)
        time.sleep(int(m.group(1)) + 0.3)            # someone saved recently; wait it out
        X("SAVE", "OK SAVE")
    X("SET brightness %d" % b0, "OK SET")
    got = ctx.board.ask("SAVE")
    m = rl.match(got[0]) if len(got) == 1 else None
    if not m:
        raise Fail("second SAVE within 10 s -> %s (want ERR SAVE ratelimit <s>)" % got)
    wait = int(m.group(1))
    if not 1 <= wait <= 10:
        raise Fail("ratelimit says %d s (want 1..10)" % wait)
    time.sleep(wait + 0.3)
    X("SAVE", "OK SAVE")                             # the write that puts flash back
    X("SAVE", "OK SAVE")                             # nothing changed: successful no-op, no ratelimit
    X("SAVE", "OK SAVE")
    if ctx.get("brightness") != b0:
        raise Fail("brightness not restored")
    return "ratelimit %d s, unchanged SAVE is a no-op, flash back to brightness=%d" % (wait, b0)


@check("own_timing_for", "OWN LED 0 FOR 2000 releases on the board at 2.0 s +-0.15", seconds=3)
def chk_own_timing_for(ctx):
    b = ctx.board
    b.send("OWN LED 0 FOR 2000")
    _, t0, _ = b.expect(re.compile("^" + re.escape(own_line(0x001)) + "$"), 1.0)
    _, t1, others = b.expect(re.compile("^" + re.escape(own_line()) + "$"), 3.5)
    dt = t1 - t0
    ctx.measure["for_2000_s"] = round(dt, 3)
    if abs(dt - 2.0) > 0.15:
        raise Fail("released after %.3f s (want 2.0 +-0.15)" % dt)
    return "released after %.3f s" % dt


@check("own_silence", "OWN GLASS 1 then total silence: all-zero EVT OWN at 15 s +-0.5", seconds=16)
def chk_own_silence(ctx):
    b = ctx.board
    b.send("OWN GLASS 1")
    _, t0, _ = b.expect(re.compile("^" + re.escape(own_line(0, 0x02)) + "$"), 1.0)
    _, t1, others = b.expect(re.compile("^" + re.escape(own_line()) + "$"), 17.0)
    dt = t1 - t0
    ctx.measure["silence_release_s"] = round(dt, 3)
    if abs(dt - 15.0) > 0.5:
        raise Fail("released after %.2f s (want 15.0 +-0.5)" % dt)
    return "released after %.2f s" % dt


@check("own_keepalive", "PING every second keeps OWN LED 0 alive for 60 s (no EVT OWN meanwhile)",
       seconds=61)
def chk_own_keepalive(ctx):
    b = ctx.board
    ctx.X("OWN LED 0", "OK OWN", own_line(0x001))
    ctx.X("LED 0 SET green", "OK LED")
    t0 = time.monotonic()
    seen = []
    while time.monotonic() - t0 < 60.0:
        b.send("PING")
        for line in b.drain(1.0):
            if parse_own(line) is not None:
                seen.append(line)
    if seen:
        raise Fail("ownership changed during the PING minute: %s" % seen)
    ctx.X("OWN", own_line(0x001))
    ctx.X("RELEASE ALL", "OK RELEASE", own_line())
    return "still owned after 60 s of PINGs"


@check("tone_long", "TONE 2700:5000 -> OK; ECHO answered while it plays; HUSH stops it "
                    "(the 5 s cap itself is audible, not on the wire)", seconds=6)
def chk_tone_long(ctx):
    b = ctx.board
    restore = ctx.unmute()
    try:
        ctx.X("TONE 2700:5000", "OK TONE")
        lat = []
        for wait in (0.8, 1.5, 2.0):
            time.sleep(wait)
            t = b.send("ECHO tl")
            _, t1, _ = b.expect(r"^EVT ECHO tl$", 1.0)
            lat.append(t1 - t)
        time.sleep(1.2)                               # ~5.5 s in: the cap has hit
        ctx.X("HUSH", "OK HUSH")
    finally:
        restore()
    ctx.measure["echo_during_tone_ms"] = round(max(lat) * 1000, 1)
    return "ECHO round trips during the tone: %s ms" % ", ".join("%.1f" % (x * 1000) for x in lat)


@check("sound_priority", "a firmware question chime (SLOT ... STATE question) is outranked by host SOUND fanfare",
       seconds=3)
def chk_sound_priority(ctx):
    b = ctx.board
    restore = ctx.unmute()
    try:
        b.send("SLOT 1 STATE question NAME t DETAIL d SUB 0 SID abcdefgh")
        got = b.ask("SOUND fanfare")
        if got != ["OK SOUND"]:
            raise Fail("SOUND fanfare right after the chime -> %s" % got)
        time.sleep(1.2)
        ctx.X("SLOT 1 STATE closing NAME t DETAIL d SUB 0 SID abcdefgh")   # legacy: silent
        time.sleep(1.0)
    finally:
        restore()
    return "SOUND accepted over the chime (hear the fanfare, not the chime)"


@check("line_length", "1023-char line parsed (ERR ECHO token), 1024 and 1030 -> ERR LINE toolong, "
                      "2000 chars of junk -> toolong, and the next line parses")
def chk_line_length(ctx):
    X = ctx.X
    X("ECHO " + "x" * 1018, "ERR ECHO token")            # 1023 chars: parsed, token too long
    X("ECHO " + "x" * 1019, "ERR LINE toolong")          # 1024: one char over
    X("ECHO " + "x" * 1025, "ERR LINE toolong")          # 1030
    X("Z" * 2000, "ERR LINE toolong")
    X(b"Z" * 3000 + b"\r" + b"FROB\n", "ERR LINE toolong", "ERR FROB unknown")
    X("INFO", re.compile(r"^EVT INFO "), "OK INFO")
    return "1023 ok / 1024 / 1030 / 2000 toolong, parser recovers"


@check("led_white", "OWN LED ALL + LED ALL SET white for 1 s (bench: VBUS must stay <= 500 mA; "
                    "the board's limiter caps a frame at 220 mA)", seconds=2)
def chk_led_white(ctx):
    ctx.X("OWN LED ALL", "OK OWN", own_line(0x3FF))
    ctx.X("LED ALL SET white", "OK LED")
    time.sleep(1.0)
    d = ctx.info()
    ctx.X("RELEASE ALL", "OK RELEASE", own_line())
    return "all 10 white, board alive (STACK %d) — meter VBUS on the bench" % d["STACK"]


@check("throughput", "300 frames x 5 glasses BLIT <g> ALL under REPLY ON: count OK BLIT, report fps, "
                     "pass >= 15 fps, TXDROP unchanged", seconds=20)
def chk_throughput(ctx):
    b = ctx.board
    ctx.X("OWN GLASS ALL", "OK OWN", own_line(0, 0x1F))
    tx0 = ctx.info()["TXDROP"]
    frames, per = 300, 5
    ok = err = 0
    bad = []
    t_last = None
    t_start = time.monotonic()

    def pump(timeout):
        nonlocal ok, err, t_last
        item = b._get(timeout)
        if item is None:
            if b.gone is not None:
                raise PortGone(b.gone[1])
            return False
        t, line = item
        if line == "OK BLIT":
            ok += 1
            t_last = t
        elif line.startswith("ERR BLIT"):
            err += 1
            bad.append(line)
        elif NOISE_RE.match(line):
            b.noise += 1
        else:
            bad.append(line)
        return True

    for f in range(frames):
        data = b"".join(("BLIT %d ALL %s\n" % (g, frame_b64(f, g))).encode("ascii")
                        for g in range(per))
        b.write(data, timeout=5.0)
        # at most one frame in flight beyond this one
        while (f + 1) * per - ok - err > per:
            if not pump(5.0):
                raise Fail("board stalled after %d OK BLIT (frame %d)" % (ok, f))
    deadline = time.monotonic() + 5.0
    while ok + err < frames * per and time.monotonic() < deadline:
        pump(deadline - time.monotonic())
    if ok + err < frames * per:
        raise Fail("only %d/%d replies" % (ok + err, frames * per))
    elapsed = (t_last or time.monotonic()) - t_start
    fps = frames / elapsed
    ctx.measure["blit_fps"] = round(fps, 1)
    tx1 = ctx.info()["TXDROP"]
    ctx.X("RELEASE ALL", "OK RELEASE", own_line())
    if err or bad:
        raise Fail("%d errors during the stream: %s" % (err, bad[:5]))
    if tx1 != tx0:
        raise Fail("TXDROP rose %d -> %d during the stream" % (tx0, tx1))
    if fps < BLIT_FPS_MIN:
        raise Fail("%.1f fps < %.0f" % (fps, BLIT_FPS_MIN))
    return "%.1f fps (%d frames in %.2f s), TXDROP unchanged" % (fps, frames, elapsed)


@check("fuzz", "N random lines (printable, binary incl. NUL, mutants; 0-2000 chars; CR/LF mixed) + 10 s "
               "of raw random bytes; after every 1000 lines ECHO and INFO answer within 500 ms, "
               "STACK >= 2048, UP never decreases, the port never disappears", seconds=None)
def chk_fuzz(ctx):
    b = ctx.board
    n = ctx.args.fuzz
    if n <= 0:
        raise Skip("--fuzz 0")
    rng = random.Random(ctx.args.seed)
    d0 = ctx.info()
    last_up = d0["UP"]
    min_stack = d0["STACK"]
    max_lat = 0.0
    checkpoints = 0
    sent_bytes = 0
    t_start = time.monotonic()

    def checkpoint(tag):
        nonlocal last_up, min_stack, max_lat, checkpoints
        if b.gone is not None or not os.path.exists(b.port):
            raise Fail("port disappeared at %s (%s)" % (tag, b.gone[1] if b.gone else "path gone"))
        t = b.send("ECHO %s" % tag)
        _, t1, _ = b.expect(re.compile(r"^EVT ECHO %s$" % re.escape(tag)), 5.0)
        lat = t1 - t
        t = b.send("INFO")
        m, t2, _ = b.expect(r"^EVT INFO ", 5.0)
        lat = max(lat, t2 - t)
        max_lat = max(max_lat, lat)
        d = parse_info(m.string)
        if d is None:
            raise Fail("INFO unparsable at %s: %r" % (tag, m.string))
        ctx.record_info(d)
        min_stack = min(min_stack, d["STACK"])
        if lat > ECHO_LATENCY_MAX:
            raise Fail("ECHO/INFO took %.0f ms at %s (max %.0f)" % (lat * 1000, tag, ECHO_LATENCY_MAX * 1000))
        if d["STACK"] < STACK_MIN:
            raise Fail("STACK %d < %d at %s" % (d["STACK"], STACK_MIN, tag))
        if d["UP"] < last_up:
            raise Fail("UP went backwards %d -> %d at %s (reboot?)" % (last_up, d["UP"], tag))
        last_up = d["UP"]
        checkpoints += 1
        b.drain(0.05)
        return d

    # phase 1: lines
    block = bytearray()
    for i, line in enumerate(fuzz_lines(rng, n), 1):
        block += line
        if len(block) >= 4096:
            sent_bytes += len(block)
            b.write(bytes(block), timeout=10.0)
            block = bytearray()
        if i % 1000 == 0 or i == n:
            if block:
                sent_bytes += len(block)
                b.write(bytes(block), timeout=10.0)
                block = bytearray()
            checkpoint("fz%d" % i)
            if ctx.args.verbose or (i % 5000 == 0):
                sys.stderr.write("   fuzz %d/%d lines, %d B, min STACK %d, max latency %.0f ms\n"
                                 % (i, n, sent_bytes, min_stack, max_lat * 1000))
    # phase 2: 10 s of raw random bytes (every 512 B chunk ends in a terminator
    # so a dangerous segment can never straddle chunks)
    t_raw = time.monotonic()
    raw_bytes = 0
    while time.monotonic() - t_raw < 10.0:
        chunk = scrub_raw(rng.randbytes(511)) + b"\n"
        b.write(chunk, timeout=10.0)
        raw_bytes += len(chunk)
    checkpoint("fzraw")
    # the session is unknowable now (fuzz may have HELLO'd, OWN'd, SET ...): reset it
    lines = b.ask("HELLO")
    if not any(HELLO_RE.match(x) for x in lines):
        raise Fail("HELLO after the fuzz -> %s" % lines)
    ctx.X("REPLY ON", "OK REPLY")
    ctx.X("HUSH", "OK HUSH")
    d1 = ctx.info()
    elapsed = time.monotonic() - t_start
    ctx.measure["fuzz_lines"] = n
    ctx.measure["fuzz_bytes"] = sent_bytes + raw_bytes
    ctx.measure["fuzz_min_stack"] = min_stack
    ctx.measure["fuzz_max_latency_ms"] = round(max_lat * 1000, 1)
    ctx.measure["fuzz_txdrop_delta"] = d1["TXDROP"] - d0["TXDROP"]
    return ("%d lines (%.1f MB) + %.1f MB raw in %.0f s (%.0f KB/s), %d checkpoints, min STACK %d, "
            "max ECHO/INFO latency %.0f ms, TXDROP +%d (drops are allowed under a flood)"
            % (n, sent_bytes / 1e6, raw_bytes / 1e6, elapsed, (sent_bytes + raw_bytes) / 1024 / elapsed,
               checkpoints, min_stack, max_lat * 1000, d1["TXDROP"] - d0["TXDROP"]))


@check("dtr_drop", "closing the port (DTR drop) releases everything: reopen, bare OWN shows all zero",
       seconds=2)
def chk_dtr_drop(ctx):
    b = ctx.board
    ctx.X("OWN LED 0", "OK OWN", own_line(0x001))
    ctx.X("OWN GLASS 0", "OK OWN", own_line(0x001, 0x01))
    ctx.X("LED 0 SET red", "OK LED")
    b.close()
    time.sleep(0.5)
    b.open(drain=0.3)
    lines = b.ask("OWN")
    if lines != [own_line()]:
        raise Fail("after close/reopen OWN -> %s (want all zero)" % lines)
    lines = b.ask("INFO")
    reply_reset = not any(x == "OK INFO" for x in lines)
    ctx.X("REPLY ON", "OK REPLY")
    return "released on DTR drop; REPLY %s by the drop" % ("also reset" if reply_reset else "kept")


@check("encoder", "OWN ENC: turn -> EVT ENC <+-n>, click -> EVT CLICK (no menu), 1 s hold -> EVT HOLD, "
                  ">=3 s hold -> EVT HOLD then EVT MENU 1, then EVT MENU 0 when the menu closes",
       interactive=True)
def chk_encoder(ctx):
    b = ctx.board
    if ctx.args.no_touch:
        raise Skip("operator step (--no-touch)")
    ctx.X("OWN ENC", "OK OWN", own_line(0, 0, 0, 1))
    ka = 1.0                                   # PING while the operator takes their time
    try:
        ctx.prompt("Turn the knob a few clicks (either way).")
        m, _, others = b.expect(r"^EVT ENC ([+-]\d+)$", 30.0, keepalive=ka)
        steps = m.group(1)
        ctx.prompt("Click the knob once (a short press).")
        b.expect(r"^EVT CLICK$", 30.0, keepalive=ka)
        if any(x.startswith("EVT MENU") for x in b.drain(1.5)):
            raise Fail("a click opened the menu while ENC is owned")
        ctx.prompt("Press and hold the knob for about 1 s, then release.")
        b.expect(r"^EVT HOLD$", 30.0, keepalive=ka)
        if any(x.startswith("EVT MENU") for x in b.drain(1.5)):
            raise Fail("a 1 s hold opened the menu while ENC is owned")
        ctx.prompt("Press and hold the knob for 3+ s, release, then do not touch anything.")
        b.expect(r"^EVT HOLD$", 30.0, keepalive=ka)
        _, t_open, _ = b.expect(r"^EVT MENU 1$", 10.0, keepalive=ka)
        _, t_close, _ = b.expect(r"^EVT MENU 0$", 20.0, keepalive=ka)
        if any(parse_own(x) is not None for x in others):
            raise Fail("ownership changed while waiting for the operator: %s" % others)
    finally:
        try:
            b.ask("RELEASE ENC", timeout=3.0)
        except (Fail, PortGone):
            pass
    ctx.X("OWN", own_line())
    return "ENC %s, CLICK, HOLD, HOLD+MENU 1, MENU 0 after %.1f s" % (steps, t_close - t_open)


@check("menu_busy", "device menu (click with ENC unowned): glass paints -> ERR <verb> BUSY, OWN GLASS -> BUSY, "
                    "LED commands accepted; after EVT MENU 0 the host repaints its glass",
       interactive=True)
def chk_menu_busy(ctx):
    b = ctx.board
    if ctx.args.no_touch:
        raise Skip("operator step (--no-touch)")
    ctx.X("OWN GLASS 0", "OK OWN", own_line(0, 0x01))
    ctx.X("OWN LED 0", "OK OWN", own_line(0x001, 0x01))
    ctx.X("TEXT 0 before menu", "OK TEXT")
    ctx.prompt("Click the knob once to open the device menu, then do not touch anything.")
    _, _, others = b.expect(r"^EVT MENU 1$", 30.0, keepalive=1.0)
    if any(parse_own(x) is not None for x in others):
        raise Fail("ownership changed while waiting for the click: %s" % others)
    ctx.X("TEXT 0 during", "ERR TEXT BUSY", timeout=3.0)
    ctx.X("BLIT 0 0 " + page_b64(0, 0, 0), "ERR BLIT BUSY", timeout=3.0)
    ctx.X("CLEAR 0", "ERR CLEAR BUSY", timeout=3.0)
    ctx.X("OWN GLASS 1", "ERR OWN BUSY", timeout=3.0)
    ctx.X("LED 0 SET red", "OK LED", timeout=3.0)
    ctx.X("OWN LED 1", "OK OWN", own_line(0x003, 0x01), timeout=3.0)
    b.expect(r"^EVT MENU 0$", 20.0, keepalive=1.0)
    ctx.X("TEXT 0 after menu", "OK TEXT", timeout=3.0)     # the host redraws its glass
    ctx.X("RELEASE ALL", "OK RELEASE", own_line())
    return "BUSY during the menu, LED accepted, repaint after MENU 0"


@check("replug", "unplug/replug releases ownership: after re-enumeration a bare OWN shows all zero",
       interactive=True)
def chk_replug(ctx):
    b = ctx.board
    if ctx.args.no_touch:
        raise Skip("operator step (--no-touch)")
    ctx.X("OWN LED 0", "OK OWN", own_line(0x001))
    ctx.X("OWN GLASS 0", "OK OWN", own_line(0x001, 0x01))
    ctx.X("LED 0 SET red", "OK LED")
    ctx.prompt("Unplug the Glowbug's USB cable, wait ~2 s, plug it back in (no key press needed).")
    ctx.wait_gone(90.0, keepalive=1.0)             # still owned at the moment of the unplug
    waited = ctx.reopen(wait=60.0, settle=1.0)
    lines = [x for x in b.ask("OWN", timeout=5.0) if not HELLO_RE.match(x)]
    if lines != [own_line()]:
        raise Fail("after replug OWN -> %s" % lines)
    ctx.X("REPLY ON", "OK REPLY")
    return "port back %.1f s after the drop, ownership cleared" % waited


@check("settings_restore", "every setting back to the value read at start (RAM), then SAVE "
                           "(a no-op on the board when nothing differs from flash)", quick=True)
def chk_settings_restore(ctx):
    if not ctx.settings0:
        raise Skip("start values unknown (settings check did not run)")
    changed = []
    for k, v0 in ctx.settings0.items():
        if ctx.get(k) != v0:
            ctx.X("SET %s %d" % (k, v0), "OK SET")
            changed.append(k)
    got = ctx.board.ask("SAVE")
    m = re.match(r"^ERR SAVE ratelimit (\d+)$", got[0]) if got else None
    if m:
        time.sleep(int(m.group(1)) + 0.3)
        got = ctx.board.ask("SAVE")
    if got != ["OK SAVE"]:
        raise Fail("SAVE -> %s" % got)
    d = ctx.info()                                    # the run's TXDROP delta (before any reset)
    if ctx.info0 is not None:
        ctx.measure["txdrop_delta"] = d["TXDROP"] - ctx.info0["TXDROP"]
    return "restored %s; SAVE ok" % (", ".join(changed) if changed else "nothing (all unchanged)")


@check("reset", "RESET -> OK RESET, the port drops and is back within 5 s, HELLO answers", seconds=6)
def chk_reset(ctx):
    b = ctx.board
    t_send = b.send("RESET")                                  # (never barrier a reboot)
    m, _, others = b.expect(r"^(OK RESET|ERR RESET early)$", 2.0)
    if m.group(1) == "ERR RESET early":
        time.sleep(5.0)
        t_send = b.send("RESET")
        b.expect(r"^OK RESET$", 2.0)
    ctx.wait_gone(5.0)
    ctx.reopen(wait=max(0.5, 5.0 - (time.monotonic() - t_send)), settle=0.5)
    total = time.monotonic() - t_send
    lines = b.ask("HELLO", timeout=5.0)
    if not any(HELLO_RE.match(x) for x in lines):
        raise Fail("HELLO after RESET -> %s" % lines)
    ctx.X("REPLY ON", "OK REPLY")
    d = ctx.info()
    ctx.measure["reset_s"] = round(total, 2)
    if total > 5.0:
        raise Fail("port back after %.1f s (want <= 5)" % total)
    return "port back %.1f s after RESET, UP %d s" % (total, d["UP"])


# ------------------------------------------------------------ the runner
def daemon_status():
    """(loaded, pid) from launchctl."""
    try:
        r = subprocess.run(["launchctl", "list", DAEMON_LABEL], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False, None
    return r.returncode == 0, parse_launchctl_pid(r.stdout)


def select_checks(args):
    names = [n for n, _, _ in CHECKS]
    only = set(x.strip() for x in args.only.split(",") if x.strip()) if args.only else None
    skip = set(x.strip() for x in args.skip.split(",") if x.strip()) if args.skip else set()
    for s in (only or set()) | skip:
        if s not in names:
            sys.exit("unknown check %r — known: %s" % (s, ", ".join(names)))
    chosen = []
    for name, fn, meta in CHECKS:
        if name in skip:
            continue
        if name in BOOTSTRAP:
            chosen.append((name, fn, meta))
        elif args.quick:
            if name in QUICK:
                chosen.append((name, fn, meta))
        elif only is not None:
            if name in only or name == "settings_restore":
                chosen.append((name, fn, meta))
        else:
            chosen.append((name, fn, meta))
    return chosen


def normalise(ctx):
    """Between checks: nothing owned, nothing playing, REPLY ON."""
    b = ctx.board
    if not b.is_open():
        return
    try:
        b.send("RELEASE ALL")
        b.send("HUSH")
        b.send("REPLY ON")
        b.barrier(3.0)
    except (Fail, PortGone):
        pass


def run_conformance(args):
    out = sys.stderr if args.json else sys.stdout

    def say(s):
        out.write(s + "\n")
        out.flush()

    loaded, pid = daemon_status()
    if loaded:
        sys.exit("refusing to start: `launchctl list %s` succeeds (pid %s). The daemon owns "
                 "the port — stop it first:\n  launchctl unload ~/Library/LaunchAgents/%s.plist\n"
                 "(and load it again afterwards)" % (DAEMON_LABEL, pid or "-", DAEMON_LABEL))
    port = args.port or glowbug.find_port()
    if not port:
        sys.exit("no Glowbug found (ioreg shows no 'Glowbug' USB product) — pass --port")
    say("glowbug conformance — port %s, seed %d, fuzz %d%s%s" % (
        port, args.seed, args.fuzz, ", quick" if args.quick else "",
        ", no-touch" if args.no_touch else ""))
    say("(DFU and the bootloader paths are not exercised here: make usbflash / the bench do that)")
    board = Board(port, verbose=args.verbose)
    try:
        board.open()
    except OSError as e:
        sys.exit("cannot open %s: %s" % (port, e))
    ctx = Ctx(board, args)
    checks = select_checks(args)
    t_run = time.monotonic()
    aborted = False
    try:
        for name, fn, meta in checks:
            t0 = time.monotonic()
            status, text = "PASS", ""
            try:
                if aborted:
                    raise Skip("port lost earlier")
                text = fn(ctx) or ""
            except Skip as e:
                status, text = "SKIP", str(e)
            except Fail as e:
                status, text = "FAIL", str(e)
            except PortGone as e:
                status, text = "FAIL", "port gone: %s" % e
                try:
                    ctx.reopen(wait=10.0)
                except Fail:
                    aborted = True
            except KeyboardInterrupt:
                raise
            except Exception as e:                     # a bug in a check is a FAIL, not a crash
                status, text = "FAIL", "%s: %s" % (type(e).__name__, e)
            dt = time.monotonic() - t0
            ctx.results.append({"name": name, "status": status, "details": text,
                                "seconds": round(dt, 2), "interactive": meta["interactive"]})
            if status == "PASS":
                say("PASS %s (%s)" % (name, text) if text else "PASS %s" % name)
            elif status == "FAIL":
                say("FAIL %s: %s" % (name, text))
            else:
                say("SKIP %s (%s)" % (name, text))
            if name not in FINAL:
                normalise(ctx)
            if name == BOOTSTRAP[-1] and not aborted:
                # the values every SET must be put back to at the end
                try:
                    ctx.settings0 = dict((k, ctx.get(k)) for k in SETTING_MAX)
                except (Fail, PortGone) as e:
                    say("warning: could not snapshot the settings (%s) — no restore at the end" % e)
    except KeyboardInterrupt:
        say("interrupted — restoring settings")
        try:
            chk_settings_restore(ctx)
            normalise(ctx)
        except Exception:
            pass
        ctx.results.append({"name": "interrupted", "status": "FAIL", "details": "Ctrl-C",
                            "seconds": 0, "interactive": False})
    finally:
        board.close()

    passed = sum(1 for r in ctx.results if r["status"] == "PASS")
    failed = sum(1 for r in ctx.results if r["status"] == "FAIL")
    skipped = sum(1 for r in ctx.results if r["status"] == "SKIP")
    m = ctx.measure
    if ctx.info0 is not None:
        m.setdefault("txdrop_start", ctx.info0["TXDROP"])
    if ctx.min_stack is not None:
        m["min_stack"] = ctx.min_stack
    m["noise_events"] = board.noise
    m["tx_lines"] = board.tx_lines
    m["tx_bytes"] = board.tx_bytes
    m["elapsed_s"] = round(time.monotonic() - t_run, 1)
    summary = {
        "ok": failed == 0, "port": port, "fw": ctx.fw, "seed": args.seed,
        "passed": passed, "failed": failed, "skipped": skipped,
        "checks": ctx.results, "measurements": m, "info": ctx.info0,
        "not_exercised": ["DFU", "bootloader: bad-CRC app / 3 faults / button (make usbflash + bench)"],
    }
    say("")
    say("%d passed, %d failed, %d skipped in %s — fw %s" % (
        passed, failed, skipped, fmt_secs(m["elapsed_s"]), ctx.fw or "?"))
    bits = []
    if "blit_fps" in m:
        bits.append("blit %.1f fps" % m["blit_fps"])
    if "min_stack" in m:
        bits.append("min STACK %d" % m["min_stack"])
    if "txdrop_delta" in m:
        bits.append("TXDROP delta %+d" % m["txdrop_delta"])
    if "fuzz_txdrop_delta" in m:
        bits.append("fuzz TXDROP +%d" % m["fuzz_txdrop_delta"])
    if "for_2000_s" in m:
        bits.append("FOR 2000 -> %.3f s" % m["for_2000_s"])
    if "silence_release_s" in m:
        bits.append("silence -> %.2f s" % m["silence_release_s"])
    if "reset_s" in m:
        bits.append("reset %.1f s" % m["reset_s"])
    bits.append("knob noise %d" % board.noise)
    say("measured: " + ", ".join(bits))
    if failed:
        say("FAILED: " + ", ".join(r["name"] for r in ctx.results if r["status"] == "FAIL"))
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0 if failed == 0 else 1


# ------------------------------------------------------------ soak (daemon running)
def sock_call(msg, timeout=10.0):
    """One JSON request to the daemon's unix socket -> the reply dict.
    Never raises on ok:false; raises OSError/ValueError when the daemon
    does not answer."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(glowbug.SOCK_PATH)
        s.sendall((json.dumps(msg) + "\n").encode())
        try:
            s.shutdown(socket.SHUT_WR)      # "one object, then close your write side":
        except OSError:                     # a pre-API (1.5.0) daemon only answers on EOF
            pass
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > (1 << 20):
                raise ValueError("reply too long")
    finally:
        s.close()
    line = data.partition(b"\n")[0]
    if not line:
        raise ValueError("empty reply")
    rep = json.loads(line.decode(errors="replace"))
    if not isinstance(rep, dict):
        raise ValueError("malformed reply")
    return rep


def run_soak(args):
    hours = args.soak
    log = sys.stderr

    def say(s):
        log.write(time.strftime("%H:%M:%S ") + s + "\n")
        log.flush()

    loaded, pid0 = daemon_status()
    if not loaded or not pid0:
        sys.exit("soak needs the daemon running (`launchctl list %s` shows no PID) — glowbug install"
                 % DAEMON_LABEL)
    try:
        pre = sock_call({"cmd": "info"})
    except (OSError, ValueError) as e:
        sys.exit("soak: the daemon (pid %d) does not answer on %s: %s" % (pid0, glowbug.SOCK_PATH, e))
    if "ok" not in pre:
        sys.exit("soak: the running daemon (%s, pid %d) predates the socket API — this tool needs "
                 "daemon 2.0.0 (api 2): run `glowbug install`" % (pre.get("version", "?"), pid0))
    if not pre.get("ok"):
        sys.exit("soak: info -> %s %s" % (pre.get("error"), pre.get("message")))
    rng = random.Random(args.seed)
    colors = sorted(glowbug.PALETTE)
    sounds = sorted(glowbug.SOUNDS)
    failures = []
    shows = 0
    infos = 0
    min_stack = None
    last_up = None
    last_stack = None

    def fail(what):
        failures.append({"t": time.time(), "what": what})
        say("FAIL " + what)

    def info_tick():
        nonlocal min_stack, last_up, last_stack, infos
        try:
            rep = sock_call({"cmd": "info", "fresh": True})
        except (OSError, ValueError) as e:
            fail("info: no answer from the daemon (%s)" % e)
            return
        infos += 1
        if not rep.get("ok"):
            fail("info -> ok:false %s %s" % (rep.get("error"), rep.get("message")))
            return
        b = rep.get("board") or {}
        if not b.get("online"):
            fail("board offline: %s" % b)
            return
        st, up = b.get("stack"), b.get("up")
        if not isinstance(st, int) or not isinstance(up, int):
            fail("info has no stack/up: %s" % b)
            return
        min_stack = st if min_stack is None else min(min_stack, st)
        last_stack = st
        if st < 512:
            fail("stack %d < 512" % st)
        if last_up is not None and up < last_up:
            fail("up went backwards %d -> %d (reboot)" % (last_up, up))
        last_up = up
        loaded, pid = daemon_status()
        if pid != pid0:
            fail("daemon pid changed %s -> %s" % (pid0, pid))

    def show_tick():
        nonlocal shows
        req = {"cmd": "show", "screen": rng.randint(1, 5), "color": rng.choice(colors),
               "sound": rng.choice(sounds), "for": round(rng.uniform(3, 10), 1),
               "line1": "soak %d" % (shows + 1), "line2": time.strftime("%H:%M:%S")}
        try:
            rep = sock_call(req)
        except (OSError, ValueError) as e:
            fail("show: no answer from the daemon (%s)" % e)
            return
        shows += 1
        if not rep.get("ok"):
            fail("show %s -> ok:false %s %s" % (req, rep.get("error"), rep.get("message")))

    t0 = time.time()
    deadline = t0 + hours * 3600
    say("soak %.2f h via %s (daemon pid %d), seed %d" % (hours, glowbug.SOCK_PATH, pid0, args.seed))
    info_tick()
    next_show = time.time() + rng.uniform(5, 30)
    next_info = time.time() + 60
    next_status = time.time() + 60
    try:
        while time.time() < deadline:
            now = time.time()
            if now >= next_show:
                show_tick()
                next_show = now + rng.uniform(5, 30)
            if now >= next_info:
                info_tick()
                next_info = now + 60
            if now >= next_status:
                say("soak %s/%s | shows %d | infos %d | fails %d | up %s | stack %s (min %s) | pid %s"
                    % (fmt_secs(now - t0), fmt_secs(hours * 3600), shows, infos, len(failures),
                       last_up, last_stack, min_stack, pid0))
                next_status = now + 60
            time.sleep(min(0.5, max(0.0, min(next_show, next_info, next_status) - time.time())))
    except KeyboardInterrupt:
        say("interrupted")
        failures.append({"t": time.time(), "what": "interrupted (Ctrl-C)"})
    elapsed = time.time() - t0
    summary = {"ok": not failures, "hours": round(elapsed / 3600, 3), "shows": shows, "infos": infos,
               "failures": failures, "min_stack": min_stack, "last_up": last_up, "daemon_pid": pid0,
               "seed": args.seed}
    say("soak done: %s, %d shows, %d infos, %d failures, min stack %s, up %s"
        % (fmt_secs(elapsed), shows, infos, len(failures), min_stack, last_up))
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0 if not failures else 1


# ------------------------------------------------------------ CLI
def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="checks, in order:\n" + "\n".join(
                                    "  %-17s %s%s" % (n, m["doc"], " [operator]" if m["interactive"] else "")
                                    for n, _, m in CHECKS))
    p.add_argument("--port", help="serial port (default: the Glowbug ioreg finds)")
    p.add_argument("--only", metavar="NAME[,NAME]", help="run only these checks (+ the session bootstrap)")
    p.add_argument("--skip", metavar="NAME[,NAME]", help="skip these checks")
    p.add_argument("--no-touch", action="store_true", help="skip the operator (knob/replug) checks")
    p.add_argument("--fuzz", type=int, default=10000, metavar="N", help="fuzz lines (default 10000; 0 skips)")
    p.add_argument("--soak", type=float, metavar="HOURS", help="soak through the running daemon instead")
    p.add_argument("--quick", action="store_true", help="~30 s production spot check")
    p.add_argument("--json", action="store_true", help="machine-readable summary on stdout")
    p.add_argument("--seed", type=int, default=None, help="RNG seed (fuzz + soak); default: time")
    p.add_argument("-v", "--verbose", action="store_true", help="echo every line on the wire to stderr")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.seed is None:
        args.seed = int(time.time()) % 1000000
    if args.soak is not None:
        return run_soak(args)
    return run_conformance(args)


if __name__ == "__main__":
    sys.exit(main())
