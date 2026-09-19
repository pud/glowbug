#!/usr/bin/env python3
"""Glowbug host tests — no hardware, no network, nothing written outside a
temp dir. From the repo root:

    python3 -m unittest discover -s tests -v

Every path constant the daemon touches (~/Library/Application Support/
Glowbug, ~/.glowbug, ~/.claude/sessions, the Cursor DB, the log) is
redirected into a TemporaryDirectory for the duration of each test, and
the module's `time` is swapped for a settable clock — Session stamps
created/born_at/last_seen with time.time() (glowbug.py Session.__init__)
and display_state()/assign_slots() read it back, so nothing here sleeps.
"""

import json
import os
import select
import sys
import tempfile
import threading
import time as _real_time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import glowbug  # noqa: E402

PATH_CONSTS = ("APP_DIR", "SOCK_PATH", "SESS_STATE", "LOG_PATH",
               "SESSIONS_DIR", "CURSOR_STATE_DB")

IDLE_LINE = "SLOT %d STATE idle NAME - DETAIL  SUB 0 SID -"   # push_state, the
                                                             # empty-slot branch


class FakeClock:
    """Stands in for the `time` module inside glowbug. Only the three
    attributes glowbug uses exist: time(), sleep() (advances), strftime()
    (passes through, for log())."""
    def __init__(self, t=1_700_000_000.0):
        self.now = float(t)

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def advance(self, s):
        self.now += s

    def strftime(self, *a, **k):
        return _real_time.strftime(*a, **k)


class GlowbugCase(unittest.TestCase):
    """Sandboxed paths + fake clock + captured log for every test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self._saved = {k: getattr(glowbug, k) for k in PATH_CONSTS}
        glowbug.APP_DIR = os.path.join(root, "app")
        glowbug.SOCK_PATH = os.path.join(root, "daemon.sock")
        glowbug.SESS_STATE = os.path.join(root, "sessions.json")
        glowbug.LOG_PATH = os.path.join(root, "glowbug.log")
        glowbug.SESSIONS_DIR = os.path.join(root, "claude-sessions")
        glowbug.CURSOR_STATE_DB = os.path.join(root, "state.vscdb")
        self.logs = []
        self._log = glowbug.log
        glowbug.log = self.logs.append
        self.clock = FakeClock()
        glowbug.time = self.clock

    def tearDown(self):
        glowbug.time = _real_time
        glowbug.log = self._log
        for k, v in self._saved.items():
            setattr(glowbug, k, v)
        self.tmp.cleanup()

    # -- helpers --
    def add(self, d, sid, source="claude", name=None, **attrs):
        s = glowbug.Session(sid, source)
        s.name = sid[:8] if name is None else name
        for k, v in attrs.items():
            setattr(s, k, v)
        d.sessions[s.key] = s
        return s

    def lines(self, d):
        """assign_slots + push_state, as a list of text lines."""
        d.assign_slots()
        raw = d.push_state()
        self.assertTrue(raw.endswith(b"\n"))
        return raw.decode().split("\n")[:-1]

    def slot_names(self, d):
        d.assign_slots()
        return [d.sessions[k].name if k else None for k in d.slots]


# ------------------------------------------------------ push_state format
class PushStateTests(GlowbugCase):

    def test_01_empty_board_is_exactly_five_idle_lines(self):
        d = glowbug.Daemon()
        want = "".join(IDLE_LINE % i + "\n" for i in range(1, 6)).encode()
        self.assertEqual(d.push_state(), want)
        # and push_state consumed the dirty flag
        self.assertFalse(d.dirty)

    def test_02_one_busy_claude_session_is_thinking_in_slot_1(self):
        d = glowbug.Daemon()
        self.add(d, "abcdef12-3456-7890", name="myproject", busy=True)
        self.clock.advance(2.0)      # past the 1.5 s "arriving" window
        lines = self.lines(d)
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0],
                         "SLOT 1 STATE thinking NAME myproject DETAIL  SUB 0 SID abcdef12")
        self.assertEqual(lines[1:], [IDLE_LINE % i for i in range(2, 6)])

    def test_03_name_truncated_to_21_chars(self):
        d = glowbug.Daemon()
        self.add(d, "sid00001", name="A" * 30)
        self.clock.advance(2.0)
        line = self.lines(d)[0]
        self.assertIn(" NAME " + "A" * 21 + " DETAIL", line)
        self.assertNotIn("A" * 22, line)

    def test_04_detail_only_for_permission_and_error(self):
        d = glowbug.Daemon()
        # thinking with a stale detail -> DETAIL blank (push_state: detail
        # only when st in ("permission", "error"))
        s = self.add(d, "sid00001", name="n", busy=True, detail="Bash")
        self.clock.advance(2.0)
        self.assertIn(" DETAIL  SUB 0 ", self.lines(d)[0])
        # permission (registry waiting + a gated tool name) -> DETAIL Bash
        s.reg_waiting = True
        self.assertEqual(self.lines(d)[0],
                         "SLOT 1 STATE permission NAME n DETAIL Bash SUB 0 SID sid00001")
        # question (AskUserQuestion dialog) -> DETAIL blank
        s.detail = "AskUserQuestion"
        self.assertEqual(self.lines(d)[0],
                         "SLOT 1 STATE question NAME n DETAIL  SUB 0 SID sid00001")
        # error -> DETAIL <error_type>, truncated to 21
        s.reg_waiting = False
        s.hook_state = "error"
        s.detail = "E" * 30
        self.assertEqual(self.lines(d)[0],
                         "SLOT 1 STATE error NAME n DETAIL %s SUB 0 SID sid00001" % ("E" * 21))

    def test_05_subagents_are_never_slotted_but_format_carries_sub_1(self):
        d = glowbug.Daemon()
        self.add(d, "subagent1", name="sub", is_subagent=True)
        self.add(d, "primary01", name="main")
        # assign_slots filters `not s.is_subagent` (user decision 2026-08-16):
        # the subagent never gets a screen, only the primary does
        self.assertEqual(self.slot_names(d), ["main", None, None, None, None])
        # ...so `SUB 1` is unreachable through assign_slots. The format
        # string still emits it if a subagent key is forced into a slot —
        # documented here so a future change to the filter is a conscious one.
        d.slots = [("claude", "subagent1")] + [None] * 4
        self.clock.advance(2.0)
        self.assertEqual(d.push_state().decode().split("\n")[0],
                         "SLOT 1 STATE idle NAME sub DETAIL  SUB 1 SID subagent")

    def test_06_sid_is_first_8_chars_with_spaces_stripped(self):
        d = glowbug.Daemon()
        self.add(d, "ab cd ef gh ij kl", name="spaced")
        self.clock.advance(2.0)
        self.assertTrue(self.lines(d)[0].endswith(" SID abcdefgh"))
        # empty / all-space sid -> "-"   ((s.sid or "-").replace(" ", "")[:8] or "-")
        for sid in ("", "   "):
            d = glowbug.Daemon()
            self.add(d, sid, name="blank")
            self.clock.advance(2.0)
            self.assertTrue(self.lines(d)[0].endswith(" SID -"), sid)

    def test_07_chronological_left_to_right(self):
        d = glowbug.Daemon()
        t = self.clock.now
        # inserted newest-first to prove the dict order is irrelevant
        self.add(d, "c", name="third", created=t + 2)
        self.add(d, "b", name="second", created=t + 1)
        self.add(d, "a", name="first", created=t)
        self.assertEqual(self.slot_names(d),
                         ["first", "second", "third", None, None])
        # tie-break: same `created` -> source, then sid  (sort key
        # (created, source, sid) in assign_slots)
        d = glowbug.Daemon()
        self.add(d, "zz", "cursor", name="cur-zz", created=t)
        self.add(d, "zz", "claude", name="cla-zz", created=t)
        self.add(d, "aa", "claude", name="cla-aa", created=t)
        self.clock.advance(3.0)      # cursor needs HOOK_APPEAR_S to be shown
        self.assertEqual(self.slot_names(d),
                         ["cla-aa", "cla-zz", "cur-zz", None, None])

    def test_08_overflow_drops_the_oldest_off_the_left(self):
        d = glowbug.Daemon()
        t = self.clock.now
        for i in range(7):
            self.add(d, "s%d" % i, name="s%d" % i, created=t + i)
        # live[-board_slots:] keeps the 5 NEWEST, oldest two fall off
        self.assertEqual(self.slot_names(d), ["s2", "s3", "s4", "s5", "s6"])
        # the newest arrival lands on the RIGHT
        self.add(d, "s7", name="s7", created=t + 7)
        self.assertEqual(self.slot_names(d)[-1], "s7")

    def test_09_dead_session_holds_its_slot_under_2_2s_then_compacts(self):
        d = glowbug.Daemon()
        t = self.clock.now
        a = self.add(d, "a", name="a", created=t)
        self.add(d, "b", name="b", created=t + 1)
        self.clock.advance(5.0)
        self.assertEqual(self.slot_names(d), ["a", "b", None, None, None])
        a.alive = False
        a.died_at = self.clock.now
        self.clock.advance(2.1)
        self.assertEqual(self.slot_names(d), ["a", "b", None, None, None],
                         "farewell window: slot held while < 2.2 s dead")
        self.clock.advance(0.2)       # 2.3 s dead
        self.assertEqual(self.slot_names(d), ["b", None, None, None, None],
                         "ticker compacts once the farewell is over")
        # a session that dies before it was ever visible gets no farewell
        # (died_at >= appear_at() guard): cursor, dead at age 1 s
        d = glowbug.Daemon()
        c = self.add(d, "c", "cursor", name="c")
        self.clock.advance(1.0)
        c.alive = False
        c.died_at = self.clock.now
        self.clock.advance(0.5)
        self.assertEqual(self.slot_names(d), [None] * 5)

    def test_10_hook_appear_debounce_only_for_hook_only_sources(self):
        self.assertEqual(glowbug.HOOK_APPEAR_S, 2.0)
        d = glowbug.Daemon()
        self.add(d, "cur", "cursor", name="cursor-chat")
        self.add(d, "cla", "claude", name="claude-chat")
        # claude is instant (appear_at = born_at); cursor waits HOOK_APPEAR_S
        self.assertEqual(self.slot_names(d), ["claude-chat", None, None, None, None])
        self.clock.advance(1.0)
        self.assertEqual(self.slot_names(d), ["claude-chat", None, None, None, None])
        self.clock.advance(1.5)       # age 2.5 s
        self.assertEqual(self.slot_names(d),
                         ["claude-chat", "cursor-chat", None, None, None])

    def test_11_display_state_precedence(self):
        s = glowbug.Session("sid", "claude")
        now = self.clock.now
        # everything at once -> dead wins
        s.alive, s.hook_state, s.reg_waiting, s.busy = False, "error", True, True
        s.detail = "Bash"
        self.assertEqual(s.display_state(), "closing")
        # alive: error beats waiting
        s.alive = True
        self.assertEqual(s.display_state(), "error")
        # waiting (registry): permission iff detail names a gated tool
        s.hook_state = "idle"
        self.assertEqual(s.display_state(), "permission")
        s.detail = "AskUserQuestion"
        self.assertEqual(s.display_state(), "question")
        s.detail = ""
        self.assertEqual(s.display_state(), "question")
        # waiting via the hook alone lasts 3 s, then falls through
        s.reg_waiting = False
        s.hook_state, s.waiting_at, s.detail = "waiting", now, "Edit"
        self.assertEqual(s.display_state(), "permission")
        self.clock.advance(3.0)
        self.assertNotIn(s.display_state(), ("permission", "question"))
        s.hook_state = "idle"
        # arriving: 1.5 s from appear_at(), and it beats thinking
        s.born_at = self.clock.now
        s.busy = True
        self.assertEqual(s.display_state(), "arriving")
        self.clock.advance(1.4)
        self.assertEqual(s.display_state(), "arriving")
        self.clock.advance(0.2)
        self.assertEqual(s.display_state(), "thinking")
        # thinking beats done
        s.done_at = self.clock.now
        self.assertEqual(s.display_state(), "thinking")
        s.busy = False
        self.assertEqual(s.display_state(), "done")
        self.clock.advance(glowbug.DONE_S - 0.1)
        self.assertEqual(s.display_state(), "done")
        self.clock.advance(0.2)
        self.assertEqual(s.display_state(), "idle")
        # hook-only source: "working" = thinking until WORK_STALE_S
        c = glowbug.Session("c", "cursor")
        c.born_at = self.clock.now - 10.0     # well past arriving
        c.hook_state, c.activity_at = "working", self.clock.now
        self.assertEqual(c.display_state(), "thinking")
        self.clock.advance(glowbug.WORK_STALE_S + 1)
        self.assertEqual(c.display_state(), "idle")

    def test_report_keeps_the_1_5_0_keys(self):
        d = glowbug.Daemon()
        self.add(d, "sid00001", name="n", busy=True)
        d.last_event_at["claude"] = self.clock.now - 4.0
        d.assign_slots()
        r = d.report()
        self.assertEqual(set(r), {"version", "sessions", "last_event_at"})
        self.assertEqual(r["version"], glowbug.VERSION)
        self.assertEqual(r["sessions"],
                         [{"source": "claude", "name": "n", "state": "arriving"}])
        self.assertEqual(r["last_event_at"], {"claude": 4.0})


# --------------------------------------------------- HELLO negotiation
class NegotiationTests(GlowbugCase):

    def test_12_proto3_hello_widens_to_32(self):
        d = glowbug.Daemon()
        d.handle_board_line("EVT HELLO 1.4.17 PROTO 3 SLOTS 32")
        self.assertEqual(d.board_slots, 32)
        self.assertEqual(len(d.slots), 32)
        self.assertEqual(len(self.lines(d)), 32)
        self.assertEqual(self.lines(d)[31], IDLE_LINE % 32)

    def test_13_proto2_hello_keeps_5(self):
        d = glowbug.Daemon()
        d.handle_board_line("EVT HELLO 1.0.0 PROTO 2")
        self.assertEqual(d.board_slots, 5)
        # and an old board plugged in after a new one re-narrows the window
        d.handle_board_line("EVT HELLO 1.4.17 PROTO 3 SLOTS 32")
        self.assertEqual(d.board_slots, 32)
        d.handle_board_line("EVT HELLO 1.0.0 PROTO 2")
        self.assertEqual(d.board_slots, 5)
        self.assertEqual(len(self.lines(d)), 5)

    def test_14_slots_clamped_to_board_slots_max(self):
        d = glowbug.Daemon()
        d.handle_board_line("EVT HELLO x PROTO 3 SLOTS 64")
        self.assertEqual(d.board_slots, 32)
        self.assertEqual(glowbug.BOARD_SLOTS_MAX, 32)
        # ...and never below NUM_SLOTS either
        d.handle_board_line("EVT HELLO x PROTO 3 SLOTS 1")
        self.assertEqual(d.board_slots, 5)

    def test_15_garbage_hello_keeps_5_without_raising(self):
        d = glowbug.Daemon()
        for line in ("EVT HELLO PROTO SLOTS", "EVT HELLO", "EVT HELLO 1 PROTO",
                     "EVT HELLO 1 PROTO 3", "EVT HELLO 1 PROTO 3 SLOTS",
                     "EVT HELLO 1 PROTO three SLOTS 32"):
            d.handle_board_line(line)
            self.assertEqual(d.board_slots, 5, line)
        self.assertEqual(len(self.lines(d)), 5)

    def test_17_hello_sets_dirty(self):
        d = glowbug.Daemon()
        d.push_state()                    # clears dirty
        self.assertFalse(d.dirty)
        d.handle_board_line("EVT HELLO 1.4.17 PROTO 3 SLOTS 32")
        self.assertTrue(d.dirty)
        d.push_state()
        # even a HELLO that changes nothing re-pushes (the board may have
        # just re-enumerated and lost its slot state)
        d.handle_board_line("EVT HELLO 1.4.17 PROTO 3 SLOTS 32")
        self.assertTrue(d.dirty)

    def test_18_proto4_hello_also_widens(self):
        d = glowbug.Daemon()
        d.handle_board_line("EVT HELLO 2.0.0 PROTO 4 SLOTS 32")   # check is >= 3
        self.assertEqual(d.board_slots, 32)

    def test_other_board_lines_are_ignored_today(self):
        """1.5.0 parses only EVT HELLO; everything else is dropped without
        touching any state. (Step 3 of the PROTO 4 plan turns this into a
        dispatcher — update this test then.)"""
        d = glowbug.Daemon()
        d.push_state()
        for line in ("EVT ENC 1", "EVT CLICK", "ERR X unknown", "OK PING",
                     "EVT INFO FW 2.0.0 PROTO 4", "", "   ", "\r",
                     "EVT HELLOX 1 PROTO 3 SLOTS 32", "HELLO"):
            d.handle_board_line(line)
            self.assertEqual(d.board_slots, 5, repr(line))
            self.assertFalse(d.dirty, repr(line))
        self.assertEqual(self.logs, [])


# ------------------------------------------ serial_loop reconnect (real pty)
class _StopLoop(BaseException):
    """Raised from sleep() to leave serial_loop's `while True` — it only
    catches OSError, and BaseException is not one."""


class LoopClock:
    """Real time, stoppable sleep — for driving the real serial_loop."""
    def __init__(self):
        self.stop = threading.Event()

    def time(self):
        return _real_time.time()

    def strftime(self, *a, **k):
        return _real_time.strftime(*a, **k)

    def sleep(self, s):
        if self.stop.is_set():
            raise _StopLoop()
        _real_time.sleep(min(s, 0.05))


@unittest.skipUnless(hasattr(os, "openpty"), "needs a pty")
class SerialLoopTests(GlowbugCase):
    """close_fd() is a closure over serial_loop's locals (fd/txbuf), so it
    cannot be called directly — instead the real loop is run on a pty:
    the test plays the board on the master side."""

    def setUp(self):
        super().setUp()
        self._patched = {k: getattr(glowbug, k) for k in
                         ("find_port", "hid_idle_s", "wire_sources",
                          "running_sources")}
        self.port = {"path": None}
        glowbug.find_port = lambda: self.port["path"]
        glowbug.hid_idle_s = lambda: None                 # no ioreg
        glowbug.wire_sources = lambda explicit=False: []  # never touch hooks
        glowbug.running_sources = lambda: set()           # no `ps`
        self.loop_clock = LoopClock()
        glowbug.time = self.loop_clock

    def tearDown(self):
        self.loop_clock.stop.set()
        for k, v in self._patched.items():
            setattr(glowbug, k, v)
        super().tearDown()

    @staticmethod
    def _wait_until(pred, timeout):
        end = _real_time.time() + timeout
        while _real_time.time() < end:
            if pred():
                return True
            _real_time.sleep(0.02)
        return pred()

    @staticmethod
    def _wait_for_bytes(fd, needle, timeout):
        acc, end = b"", _real_time.time() + timeout
        while _real_time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                acc += os.read(fd, 4096)
                if needle in acc:
                    return acc
        return None

    def test_16_window_resets_to_num_slots_on_reconnect(self):
        master, slave = os.openpty()
        self.port["path"] = os.ttyname(slave)
        d = glowbug.Daemon()

        def run():
            try:
                d.serial_loop()
            except _StopLoop:
                pass
        t = threading.Thread(target=run, daemon=True)
        t.start()
        try:
            # the daemon solicits capabilities on open
            self.assertIsNotNone(self._wait_for_bytes(master, b"HELLO\n", 10))
            os.write(master, b"EVT HELLO 1.4.17 PROTO 3 SLOTS 32\n")
            self.assertTrue(self._wait_until(lambda: d.board_slots == 32, 10))
            self.assertEqual(len(d.slots), 32)
            # the port vanishes (unplug / re-enumeration): the recheck
            # (PORT_RECHECK_S = 2 s) sees find_port() disagree -> close_fd()
            self.port["path"] = None
            self.assertTrue(self._wait_until(lambda: d.board_slots == 5, 10))
            self.assertEqual(len(d.slots), 5)
            self.assertTrue(any("window back to 5" in m for m in self.logs),
                            self.logs)
        finally:
            self.loop_clock.stop.set()
            t.join(5)
            os.close(master)
            os.close(slave)


# ------------------------------------------------- tables + resolvers (step 2)
class TablesTests(unittest.TestCase):
    """PALETTE / SOUNDS / parse_color / parse_notes / scale_color /
    load_tables — pure functions, no daemon, no fixtures."""

    def test_color_forms_resolve_identically(self):
        for form in ("green", "GREEN", " Green ", "#0f0", "#0F0",
                     "#00ff00", "00FF00", "00ff00", " #00FF00 "):
            self.assertEqual(glowbug.parse_color(form), "00FF00", form)
        self.assertEqual(glowbug.parse_color("#abc"), "AABBCC")

    def test_color_unknown_or_malformed(self):
        with self.assertRaises(ValueError) as cm:
            glowbug.parse_color("nope")
        self.assertEqual(str(cm.exception),
                         "unknown color 'nope' (glowbug palette lists them)")
        for bad in ("#GGGGGG", "12345", "1234567", "0f0", "", "#", "2-bad",
                    "a" * 25, "#00FF00FF", 5, None, ["green"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                glowbug.parse_color(bad)
        # a merged table is consulted when given
        self.assertEqual(glowbug.parse_color("brand", {"brand": "123456"}),
                         "123456")
        with self.assertRaises(ValueError):
            glowbug.parse_color("green", {"brand": "123456"})

    def test_palette_staples_match_firmware(self):
        P = glowbug.PALETTE
        # fw 1.4.17 v5/firmware/src/main.cpp state_render, mid-point of each
        # eased channel (see the comments beside each entry in glowbug.py)
        self.assertEqual(P["thinking"], "5400FF")
        self.assertEqual(P["question"], "FF3800")
        self.assertEqual(P["permission"], "FF1C50")
        self.assertEqual(P["error"], "C00000")
        self.assertEqual(P["done"], "008800")
        self.assertEqual(P["unread"], "00C000")
        self.assertEqual(P["subagent"], "303030")
        self.assertEqual(P["lamp"], "FF6412")
        self.assertEqual(P["warm"], P["lamp"])
        for n, h in (("off", "000000"), ("white", "FFFFFF"), ("red", "FF0000"),
                     ("green", "00FF00"), ("blue", "0000FF"),
                     ("yellow", "FFFF00"), ("orange", "FF8000"),
                     ("violet", "8000FF"), ("cyan", "00FFFF"),
                     ("magenta", "FF00FF")):
            self.assertEqual(P[n], h, n)
        for n in ("permission pink ember amber gold lime mint teal sky indigo "
                  "purple rose coral warm cool grey gray dim").split():
            self.assertIn(n, P)
        for name, hex6 in P.items():
            self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,23}$")
            self.assertRegex(hex6, r"^[0-9A-F]{6}$")
            self.assertEqual(glowbug.parse_color(name), hex6)

    def test_sound_staples_match_firmware(self):
        S = glowbug.SOUNDS
        # note-for-note from fw 1.4.17 main.cpp:173-191
        self.assertEqual(S["fanfare"], [(1568, 55), (2093, 55), (2637, 55),
                                        (3136, 85), (0, 30), (3136, 55),
                                        (4186, 200)])
        self.assertEqual(S["ding"], [(2637, 70), (0, 20), (3951, 260)])
        self.assertEqual(S["soft"], [(3136, 90), (2637, 90), (2093, 200)])
        self.assertEqual(S["blip"], [(3136, 45)])
        self.assertEqual(S["hello"], [(2637, 25), (3520, 40)])
        self.assertEqual(S["bye"], [(2637, 70), (2093, 70), (1568, 150)])
        self.assertEqual(S["boot"], [(2093, 50), (2637, 50), (3136, 50),
                                     (4186, 150)])
        for n in ("tick flutter snap beep double rise fall warn fail alarm "
                  "coin knock sos").split():
            self.assertIn(n, S)

    def test_every_sound_is_within_wire_limits(self):
        for name, notes in glowbug.SOUNDS.items():
            self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,23}$")
            self.assertIs(glowbug.check_notes(notes), notes, name)
            self.assertLessEqual(len(notes), 32, name)
            self.assertLessEqual(sum(ms for _, ms in notes), 5000, name)
            for hz, ms in notes:
                self.assertTrue(hz == 0 or 50 <= hz <= 20000, (name, hz))
                self.assertTrue(1 <= ms <= 5000, (name, ms))
        sos = glowbug.SOUNDS["sos"]
        self.assertEqual(len(sos), 18)
        self.assertEqual(sum(ms for _, ms in sos), 3000)
        self.assertEqual(glowbug.parse_notes("sos"), sos)

    def test_notes_grammar(self):
        want = [(2637, 70), (0, 20), (3951, 260)]
        self.assertEqual(glowbug.parse_notes("2637:70,0:20,3951:260"), want)
        self.assertEqual(glowbug.parse_notes(" 2637 : 70 , 0:20 ,3951:260 "), want)
        self.assertEqual(glowbug.parse_notes("ding"), want)
        self.assertEqual(glowbug.parse_notes(" DING "), want)
        self.assertEqual(glowbug.parse_notes([[2637, 70], (0, 20), [3951, 260]]),
                         want)
        self.assertEqual(glowbug.parse_notes(((2637, 70),)), [(2637, 70)])
        self.assertIsNot(glowbug.parse_notes("ding"), glowbug.SOUNDS["ding"])
        self.assertEqual(glowbug.parse_notes("riff", {"riff": [(60, 1)]}),
                         [(60, 1)])
        self.assertEqual(glowbug.notes_to_wire(want), "2637:70,0:20,3951:260")
        self.assertEqual(glowbug.notes_to_wire(glowbug.parse_notes("blip")),
                         "3136:45")
        # the limits, inclusive
        self.assertEqual(len(glowbug.parse_notes("2700:156," * 31 + "2700:164")), 32)
        self.assertEqual(glowbug.parse_notes("50:5000"), [(50, 5000)])
        self.assertEqual(glowbug.parse_notes("20000:1"), [(20000, 1)])

    def test_notes_rejections_name_the_note(self):
        cases = [
            (("2700:100," * 33)[:-1],  "33 notes (max 32)"),
            ("2700:5000,0:1",          "total 5001 ms (max 5000)"),
            ("2700:100,30:100",        "note 2 (30:100): hz must be 0 or 50..20000"),
            ("20001:10",               "note 1 (20001:10): hz must be 0 or 50..20000"),
            ("2700:0",                 "note 1 (2700:0): ms must be 1..5000"),
            ("2700:10,2700:5001",      "note 2 (2700:5001): ms must be 1..5000"),
            ("abc:10",                 "note 1 ('abc:10'): want hz:ms"),
            ("2700:10,,2700:10",       "note 2 (''): want hz:ms"),
            ("2700:1.5",               "note 1 ('2700:1.5'): want hz:ms"),
            ("2700",                   "unknown sound '2700' (glowbug sounds lists them)"),
            ("",                       "unknown sound '' (glowbug sounds lists them)"),
            ("no such",                "unknown sound 'no such' (glowbug sounds lists them)"),
            ([],                       "no notes"),
            ([[2700, 10, 5]],          "note 1 ([2700, 10, 5]): want [hz, ms] integers"),
            ([[2700.5, 10]],           "note 1 ([2700.5, 10]): want [hz, ms] integers"),
            ([[True, 10]],             "note 1 ([True, 10]): want [hz, ms] integers"),
            ([(2700, 10), None],       "note 2 (None): want [hz, ms] integers"),
            (5,                        "notes must be a name, 'hz:ms,...' or a list of pairs"),
        ]
        for spec, msg in cases:
            with self.assertRaises(ValueError, msg=repr(spec)) as cm:
                glowbug.parse_notes(spec)
            self.assertEqual(str(cm.exception), msg, repr(spec))

    def test_scale_color_rounds_half_up_like_the_board(self):
        # (c * pct + 50) // 100 — state_render's own fold
        self.assertEqual(glowbug.scale_color("FF8000", 50), "804000")   # 255 -> 128
        self.assertEqual(glowbug.scale_color("orange", 50), "804000")
        self.assertEqual(glowbug.scale_color("#fff", 33), "545454")     # 255*33 -> 84
        self.assertEqual(glowbug.scale_color("FFFFFF", 0), "000000")
        self.assertEqual(glowbug.scale_color("FF6412", 100), "FF6412")
        self.assertEqual(glowbug.scale_color("010101", 1), "000000")    # 0.51 -> 0
        self.assertEqual(glowbug.scale_color("010101", 50), "010101")   # 0.5 -> 1
        self.assertEqual(glowbug.scale_color("000000", 100), "000000")
        for bad in (-1, 101, "x", None, "50.5"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                glowbug.scale_color("FFFFFF", bad)
        with self.assertRaises(ValueError):
            glowbug.scale_color("nope", 50)

    def test_load_tables_defaults_without_files(self):
        with tempfile.TemporaryDirectory() as root:
            pp = os.path.join(root, "palette.json")
            sp = os.path.join(root, "sounds.json")
            colors, sounds, warnings = glowbug.load_tables(pp, sp)
            self.assertEqual(colors, glowbug.PALETTE)
            self.assertIsNot(colors, glowbug.PALETTE)
            self.assertEqual(sounds, glowbug.SOUNDS)
            self.assertIsNot(sounds, glowbug.SOUNDS)
            self.assertEqual(warnings, [])
            self.assertEqual(os.listdir(root), [])       # never created
            # the defaults come from module constants, redirectable
            saved = glowbug.PALETTE_PATH, glowbug.SOUNDS_PATH
            try:
                glowbug.PALETTE_PATH, glowbug.SOUNDS_PATH = pp, sp
                self.assertEqual(glowbug.load_tables()[2], [])
            finally:
                glowbug.PALETTE_PATH, glowbug.SOUNDS_PATH = saved
            self.assertEqual(os.listdir(root), [])
        self.assertEqual(glowbug.PALETTE_PATH,
                         os.path.join(glowbug.APP_DIR, "palette.json"))
        self.assertEqual(glowbug.SOUNDS_PATH,
                         os.path.join(glowbug.APP_DIR, "sounds.json"))

    def test_load_tables_user_wins_and_bad_entries_warn(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            pp = os.path.join(root, "palette.json")
            sp = os.path.join(root, "sounds.json")
            with open(pp, "w") as f:
                json.dump({"green": "#00aa00",        # override, any hex form
                           "Brand": "ff00aa",         # new, case-folded
                           "warm": "amber",           # alias to a built-in
                           "bad name!": "000000",     # bad name
                           "ok": "zz",                # bad value
                           "FACADE": "000000",        # would read as hex
                           "num": 5,                  # not a string
                           "later": "notyet",         # forward reference
                           "notyet": "112233"}, f)
            with open(sp, "w") as f:
                json.dump({"ding": "2700:100",                       # override
                           "Riff": [[2700, 50], [0, 20], [3000, 50]],
                           "alias": "riff",                          # earlier in file
                           "long": [[2700, 5001]],
                           "x": 5,
                           "thirty": "30:10",
                           "_bad": "2700:10"}, f)
            before = dict(glowbug.PALETTE), dict(glowbug.SOUNDS)
            colors, sounds, warnings = glowbug.load_tables(pp, sp)
            self.assertEqual(colors["green"], "00AA00")
            self.assertEqual(colors["brand"], "FF00AA")
            self.assertEqual(colors["warm"], glowbug.PALETTE["amber"])
            self.assertEqual(colors["blue"], "0000FF")
            self.assertEqual(colors["notyet"], "112233")
            for gone in ("bad name!", "ok", "facade", "num", "later"):
                self.assertNotIn(gone, colors)
            self.assertEqual(sounds["ding"], [(2700, 100)])
            self.assertEqual(sounds["riff"], [(2700, 50), (0, 20), (3000, 50)])
            self.assertEqual(sounds["alias"], sounds["riff"])
            self.assertEqual(sounds["blip"], [(3136, 45)])
            for gone in ("long", "x", "thirty", "_bad"):
                self.assertNotIn(gone, sounds)
            self.assertEqual(len(warnings), 9, warnings)
            for w in warnings:
                self.assertRegex(w, r"^(palette|sounds)\.json: '.*' skipped — ")
            self.assertTrue(any("'FACADE'" in w and "hex" in w for w in warnings))
            self.assertTrue(any("'long'" in w and "5001" in w for w in warnings))
            self.assertTrue(any("'thirty'" in w and "note 1 (30:10)" in w
                                for w in warnings))
            self.assertTrue(any("'later'" in w and "unknown color 'notyet'" in w
                                for w in warnings))
            # the merged tables drive the resolvers
            self.assertEqual(glowbug.parse_color("BRAND", colors), "FF00AA")
            self.assertEqual(glowbug.parse_notes("Alias", sounds), sounds["riff"])
            # the built-ins were not touched
            self.assertEqual((glowbug.PALETTE, glowbug.SOUNDS), before)

    def test_load_tables_malformed_files_warn(self):
        with tempfile.TemporaryDirectory() as root:
            pp = os.path.join(root, "palette.json")
            sp = os.path.join(root, "sounds.json")
            with open(pp, "w") as f:
                f.write("{")
            with open(sp, "w") as f:
                f.write("[1, 2]")
            colors, sounds, warnings = glowbug.load_tables(pp, sp)
            self.assertEqual(colors, glowbug.PALETTE)
            self.assertEqual(sounds, glowbug.SOUNDS)
            self.assertEqual(len(warnings), 2, warnings)
            self.assertTrue(warnings[0].startswith("palette.json: unreadable ("))
            self.assertEqual(warnings[1],
                             "sounds.json: expected a JSON object {name: value} — ignored")

    def test_api_constants(self):
        self.assertEqual(glowbug.SOCKET_API, 2)
        self.assertEqual(glowbug.PROTOCOL_MIN, 4)
        self.assertEqual((glowbug.NOTES_MAX, glowbug.TONE_HZ_MIN,
                          glowbug.TONE_HZ_MAX, glowbug.TONE_MS_MAX),
                         (32, 50, 20000, 5000))


# ------------------------------------ the Claude DESKTOP app (no "status")
class StatuslessRegistryTests(GlowbugCase):
    """Claude Code's CLI stamps "status": busy|waiting|idle into its registry
    entry; the Claude desktop app (entrypoint "claude-desktop") writes an
    entry with no status field at all (bench 2026-09-19 — the session showed
    on a screen but its LED never lit). A status-less registrar must fall
    back to the hooks, exactly like Cursor/Codex do."""

    def write_entry(self, sid, **extra):
        os.makedirs(glowbug.SESSIONS_DIR, exist_ok=True)
        d = {"pid": os.getpid(), "sessionId": sid, "cwd": "/tmp/proj",
             "startedAt": 1_700_000_000_000, "kind": "interactive",
             "name": "myproject"}
        d.update(extra)
        with open(os.path.join(glowbug.SESSIONS_DIR, sid[:8] + ".json"),
                  "w") as f:
            json.dump(d, f)

    def test_read_registry_reports_whether_status_exists(self):
        self.write_entry("cli00000-0000", entrypoint="cli", status="busy")
        self.write_entry("desk0000-0000", entrypoint="claude-desktop")
        reg = glowbug.read_registry()
        self.assertTrue(reg["cli00000-0000"]["has_status"])
        self.assertTrue(reg["cli00000-0000"]["busy"])
        self.assertFalse(reg["desk0000-0000"]["has_status"])
        self.assertFalse(reg["desk0000-0000"]["busy"])

    def test_desktop_session_is_thinking_from_its_hooks(self):
        d = glowbug.Daemon()
        self.write_entry("desk0000-0000", entrypoint="claude-desktop")
        d.poll_claude_registry()
        s = d.sessions[("claude", "desk0000-0000")]
        self.assertFalse(s.reg_status)
        self.clock.advance(2.0)                   # past "arriving"
        self.assertEqual(s.display_state(), "idle")
        d.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "desk0000-0000"})
        self.assertEqual(s.display_state(), "thinking")
        # a tool event alone is enough too (the daemon can restart mid-turn
        # and never see that turn's UserPromptSubmit)
        s.hook_state = "idle"
        d.handle_hook({"hook_event_name": "PostToolUse", "session_id": "desk0000-0000",
                       "tool_name": "Bash"})
        self.assertEqual(s.display_state(), "thinking")
        d.handle_hook({"hook_event_name": "PostToolUse", "session_id": "desk0000-0000",
                       "tool_name": "Bash"})
        self.assertEqual(s.display_state(), "thinking")
        # a registry poll must not undo it (the entry says nothing about busy)
        d.poll_claude_registry()
        self.assertEqual(s.display_state(), "thinking")
        # Stop ends the turn AND starts the green celebration, which normally
        # rides the registry's busy -> idle edge
        d.handle_hook({"hook_event_name": "Stop", "session_id": "desk0000-0000"})
        self.assertEqual(s.display_state(), "done")
        self.clock.advance(glowbug.DONE_S + 0.1)
        self.assertEqual(s.display_state(), "idle")

    def test_desktop_permission_prompt_outlives_the_3s_registry_handoff(self):
        d = glowbug.Daemon()
        self.write_entry("desk0000-0000", entrypoint="claude-desktop")
        d.poll_claude_registry()
        s = d.sessions[("claude", "desk0000-0000")]
        self.clock.advance(2.0)
        d.handle_hook({"hook_event_name": "PermissionRequest", "session_id": "desk0000-0000",
                       "tool_name": "Bash"})
        self.assertEqual(s.display_state(), "permission")
        self.clock.advance(30.0)                  # no registry "waiting" ever
        d.poll_claude_registry()
        self.assertEqual(s.display_state(), "permission")
        d.handle_hook({"hook_event_name": "PostToolUse", "session_id": "desk0000-0000",
                       "tool_name": "Bash"})
        self.assertEqual(s.display_state(), "thinking")

    def test_cli_session_still_follows_the_registry(self):
        d = glowbug.Daemon()
        self.write_entry("cli00000-0000", entrypoint="cli", status="busy")
        d.poll_claude_registry()
        s = d.sessions[("claude", "cli00000-0000")]
        self.assertTrue(s.reg_status)
        self.clock.advance(2.0)
        self.assertEqual(s.display_state(), "thinking")
        # hook says "working", registry says idle -> registry wins (unchanged)
        self.write_entry("cli00000-0000", entrypoint="cli", status="idle")
        d.poll_claude_registry()
        s.hook_state, s.activity_at = "working", self.clock.now
        self.assertEqual(s.display_state(), "done")   # busy -> idle edge
        self.clock.advance(glowbug.DONE_S + 0.1)
        self.assertEqual(s.display_state(), "idle")


if __name__ == "__main__":
    unittest.main()
