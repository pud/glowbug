#!/usr/bin/env python3
"""Glowbug host API tests (daemon 2.0 / socket api 2 / board PROTO 4) —
no hardware, no network. Compose functions are checked byte-for-byte
against the wire contract in PROTOCOL.md; the daemon's dispatcher, event
plumbing, ECHO barrier and replay cache are driven through
handle_request() / handle_board_line() with no sockets; one end-to-end
test runs the real socket_loop on a temp socket; the CLI parser and the
rescue image gate are checked in-process.

    python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time as _real_time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glowbug  # noqa: E402
from test_glowbug import GlowbugCase, LoopClock, _StopLoop  # noqa: E402

HELLO4 = "EVT HELLO 2.0.0 PROTO 4 SLOTS 32"
HELLO3 = "EVT HELLO 1.4.17 PROTO 3 SLOTS 32"
CANON_REQ = {"cmd": "show", "screen": 3, "color": "green", "line1": "Build OK",
             "sound": "ding", "for": 5}
CANON_LINES = ["OWN GLASS 2 FOR 5000",
               "OWN LED 2 FOR 5000",
               "LED 2 SET 00FF00",
               "TEXT 2 Build OK",
               "TONE 2637:70,0:20,3951:260"]


class ApiCase(GlowbugCase):
    """GlowbugCase (sandboxed paths, fake clock, captured log) plus the
    user palette files redirected so a real ~/.glowbug never leaks in."""

    def setUp(self):
        super().setUp()
        self._tables = glowbug.PALETTE_PATH, glowbug.SOUNDS_PATH
        glowbug.PALETTE_PATH = os.path.join(self.tmp.name, "palette.json")
        glowbug.SOUNDS_PATH = os.path.join(self.tmp.name, "sounds.json")

    def tearDown(self):
        glowbug.PALETTE_PATH, glowbug.SOUNDS_PATH = self._tables
        super().tearDown()

    def daemon(self, hello=HELLO4):
        d = glowbug.Daemon()
        if hello:
            d.handle_board_line(hello)
            d.tx_queue.clear()
            d.tx_bytes = 0
        return d

    @staticmethod
    def queued(d):
        """Everything in tx_queue as text lines (whole blobs, in order)."""
        return b"".join(d.tx_queue).decode().split("\n")[:-1]

    @staticmethod
    def drain(sub):
        out = []
        while not sub.q.empty():
            out.append(sub.q.get_nowait())
        return out


# ------------------------------------------------------------ compose
class ComposeTests(unittest.TestCase):

    def test_canonical_show_is_byte_exact(self):
        lines, meta = glowbug.compose_show(CANON_REQ)
        self.assertEqual(lines, CANON_LINES)
        self.assertEqual(meta, {"for_ms": 5000, "truncated": False})
        # string screen and float seconds give the same wire
        req = dict(CANON_REQ, screen="3", **{"for": 5.0})
        self.assertEqual(glowbug.compose_show(req)[0], CANON_LINES)

    def test_led_modes(self):
        base = {"screen": 1, "color": "red"}
        want = {
            None:    "LED 0 SET FF0000",
            "solid": "LED 0 SET FF0000",
            "fade":  "LED 0 FADE FF0000 700",
            "pulse": "LED 0 PULSE FF0000 4D0000 2000",     # to = 30 % of FF
            "blink": "LED 0 BLINK FF0000 000000 1000",
            "off":   "LED 0 OFF",
        }
        for mode, line in want.items():
            req = dict(base, mode=mode)
            lines, _ = glowbug.compose_show(req)
            self.assertEqual(lines, ["OWN LED 0", line], mode)
        self.assertEqual(glowbug.scale_color("FF0000", 30), "4D0000")
        # explicit to / period / fade_ms
        lines, _ = glowbug.compose_show(dict(base, mode="pulse", to="blue", period=500))
        self.assertEqual(lines[1], "LED 0 PULSE FF0000 0000FF 500")
        lines, _ = glowbug.compose_show(dict(base, mode="blink", to="#0f0", period=16))
        self.assertEqual(lines[1], "LED 0 BLINK FF0000 00FF00 16")
        lines, _ = glowbug.compose_show(dict(base, mode="fade", period=65535))
        self.assertEqual(lines[1], "LED 0 FADE FF0000 65535")
        lines, _ = glowbug.compose_led({"sel": 1, "color": "red", "mode": "fade",
                                        "fade_ms": 250, "period": 999})
        self.assertEqual(lines[1], "LED 0 FADE FF0000 250")         # fade_ms wins
        for bad in ({"mode": "wobble"}, {"mode": "pulse", "period": 15},
                    {"mode": "blink", "period": 65536}, {"mode": "fade", "period": "x"},
                    {"mode": "pulse", "to": "nope"}, {"mode": True}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                glowbug.compose_show(dict(base, **bad))

    def test_big_conflicts_with_lines(self):
        with self.assertRaises(ValueError) as cm:
            glowbug.compose_show({"screen": 1, "big": "HI", "line1": "x"})
        self.assertIn("big", str(cm.exception))
        lines, _ = glowbug.compose_show({"screen": 2, "big": "  Hi  there "})
        self.assertEqual(lines, ["OWN GLASS 1", "BIG 1 Hi there"])

    def test_for_rounding_and_bounds(self):
        f = glowbug.parse_for
        self.assertEqual(f(5), 5000)
        self.assertEqual(f("0.5"), 500)
        self.assertEqual(f(1.2346), 1235)
        self.assertEqual(f(0.05), 50)
        self.assertEqual(f(86400), 86400000)
        self.assertIsNone(f(None))
        self.assertIsNone(f(0))
        for bad in (0.01, 86401, -1, "soon", True, [5]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                f(bad)
        # no `for` -> bare OWN lines
        lines, meta = glowbug.compose_show({"screen": 3, "color": "green",
                                            "line1": "x"})
        self.assertEqual(lines[:2], ["OWN GLASS 2", "OWN LED 2"])
        self.assertIsNone(meta["for_ms"])

    def test_resources_follow_what_is_given(self):
        # text only: no LED, no OWN LED
        lines, _ = glowbug.compose_show({"screen": 3, "line1": "x", "for": 1})
        self.assertEqual(lines, ["OWN GLASS 2 FOR 1000", "TEXT 2 x"])
        # color only: no glass
        lines, _ = glowbug.compose_show({"screen": 3, "color": "red"})
        self.assertEqual(lines, ["OWN LED 2", "LED 2 SET FF0000"])
        # no_led drops the LED even with a color
        lines, _ = glowbug.compose_show({"screen": 3, "color": "red", "line1": "x",
                                         "no_led": True})
        self.assertEqual(lines, ["OWN GLASS 2", "TEXT 2 x"])
        # sound only: no OWN at all (one-shots need no ownership)
        lines, _ = glowbug.compose_show({"screen": 1, "sound": "blip", "volume": 2})
        self.assertEqual(lines, ["TONE 3136:45 VOL 2"])
        with self.assertRaises(ValueError):
            glowbug.compose_show({"screen": 1})
        # two lines, and only line2
        lines, _ = glowbug.compose_show({"screen": 1, "line1": "a", "line2": "b"})
        self.assertEqual(lines[-1], "TEXT 0 a|b")
        lines, _ = glowbug.compose_show({"screen": 1, "line2": "b"})
        self.assertEqual(lines[-1], "TEXT 0 |b")
        # empty text clears
        lines, _ = glowbug.compose_show({"screen": 1, "line1": "  "})
        self.assertEqual(lines, ["OWN GLASS 0", "CLEAR 0"])

    def test_all_and_lists(self):
        lines, _ = glowbug.compose_show({"screen": "all", "color": "blue",
                                         "line1": "hi", "for": 2})
        self.assertEqual(lines, ["OWN GLASS ALL FOR 2000", "OWN LED GLASS FOR 2000",
                                 "LED GLASS SET 0000FF", "TEXT ALL hi"])
        lines, _ = glowbug.compose_show({"screen": "1,3", "color": "blue", "line1": "hi"})
        self.assertEqual(lines, ["OWN GLASS 0", "OWN GLASS 2", "OWN LED 0", "OWN LED 2",
                                 "LED 0 SET 0000FF", "LED 2 SET 0000FF",
                                 "TEXT 0 hi", "TEXT 2 hi"])
        self.assertEqual(glowbug.compose_show({"screen": [3, "1", 3], "line1": "x"})[0],
                         ["OWN GLASS 2", "OWN GLASS 0", "TEXT 2 x", "TEXT 0 x"])

    def test_selectors(self):
        ps, pl = glowbug.parse_screen_sel, glowbug.parse_led_sel
        self.assertEqual(ps(3), ["2"])
        self.assertEqual(ps(" ALL "), ["ALL"])
        self.assertEqual(ps("2,all"), ["ALL"])
        self.assertEqual(pl("ug2"), ["6"])
        self.assertEqual(pl("UG5"), ["9"])
        self.assertEqual(pl("1,3,ug2"), ["0", "2", "6"])
        self.assertEqual(pl(["glass", "ug"]), ["GLASS", "UG"])
        self.assertEqual(pl("all"), ["ALL"])
        for bad in (0, 6, "ug0", "ug6", "", ",", "1,,2", None, True, 2.5, "x", [None]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pl(bad)
        for bad in (0, 6, "ug1", "glass", "", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ps(bad)
        # led: ug2 -> LED 6, with its own OWN
        lines, _ = glowbug.compose_led({"sel": "ug2", "color": "amber", "for": 0.25})
        self.assertEqual(lines, ["OWN LED 6 FOR 250", "LED 6 SET FFA000"])
        self.assertEqual([glowbug.led_name(i) for i in range(10)],
                         ["1", "2", "3", "4", "5", "ug1", "ug2", "ug3", "ug4", "ug5"])
        si = glowbug.sel_indices
        self.assertEqual(si("led", "ALL"), set(range(10)))
        self.assertEqual(si("led", "GLASS"), set(range(5)))
        self.assertEqual(si("led", "UG"), set(range(5, 10)))
        self.assertEqual(si("glass", "ALL"), set(range(5)))
        self.assertEqual(si("glass", "2"), {2})
        self.assertEqual(si("led", "1,9"), {1, 9})
        self.assertEqual(si("glass", "GLASS"), set())
        self.assertEqual(si("glass", "7"), set())

    def test_text_sanitised(self):
        s = glowbug.sanitize_text
        self.assertEqual(s("a|b"), ("a/b", False))
        self.assertEqual(s("café → ok"), ("caf? ? ok", False))
        self.assertEqual(s("  tabs\tand\n newlines "), ("tabs and newlines", False))
        self.assertEqual(s("x" * 22), ("x" * 21, True))
        self.assertEqual(s("x" * 21), ("x" * 21, False))
        self.assertEqual(s(None), ("", False))
        self.assertEqual(s(42), ("42", False))
        lines, meta = glowbug.compose_show(
            {"screen": 5, "line1": "Build|OK — really long line here", "line2": "ü"})
        self.assertEqual(lines, ["OWN GLASS 4", "TEXT 4 Build/OK ? really lon|?"])
        self.assertTrue(meta["truncated"])
        lines, meta = glowbug.compose_text({"screen": 1, "big": "é" * 30, "for": 1})
        self.assertEqual(lines, ["OWN GLASS 0 FOR 1000", "BIG 0 " + "?" * 21])
        self.assertTrue(meta["truncated"])

    def test_brightness_scaling_and_raw(self):
        req = {"screen": 3, "color": "green"}
        self.assertEqual(glowbug.compose_show(req, brightness=50)[0][1], "LED 2 SET 008000")
        self.assertEqual(glowbug.compose_show(req, brightness=0)[0][1], "LED 2 SET 000000")
        self.assertEqual(glowbug.compose_show(dict(req, raw=True), brightness=50)[0][1],
                         "LED 2 SET 00FF00")
        # pulse: `to` derives from the SCALED color; explicit `to` scales too
        lines, _ = glowbug.compose_show(dict(req, mode="pulse"), brightness=50)
        self.assertEqual(lines[1], "LED 2 PULSE 008000 002600 2000")
        lines, _ = glowbug.compose_show(dict(req, mode="pulse", to="white"), brightness=50)
        self.assertEqual(lines[1], "LED 2 PULSE 008000 808080 2000")
        lines, _ = glowbug.compose_led({"sel": 1, "color": "white", "raw": True},
                                       brightness=1)
        self.assertEqual(lines[1], "LED 0 SET FFFFFF")
        # a user-merged table is consulted
        lines, _ = glowbug.compose_show({"screen": 1, "color": "brand"},
                                        colors={"brand": "123456"})
        self.assertEqual(lines[1], "LED 0 SET 123456")

    def test_sound(self):
        self.assertEqual(glowbug.compose_sound({"sound": "ding"}),
                         (["TONE 2637:70,0:20,3951:260"], {"duration_ms": 350}))
        self.assertEqual(glowbug.compose_sound({"sound": "2700:100,0:50", "volume": 4})[0],
                         ["TONE 2700:100,0:50 VOL 4"])
        self.assertEqual(glowbug.compose_sound({"sound": [[60, 1]]})[0], ["TONE 60:1"])
        for off in ("off", "HUSH", " stop "):
            self.assertEqual(glowbug.compose_sound({"sound": off}),
                             (["HUSH"], {"duration_ms": 0}))
        self.assertEqual(glowbug.compose_sound({"sound": "riff"}, {"riff": [(50, 5)]})[0],
                         ["TONE 50:5"])
        for bad in ({}, {"sound": "nope"}, {"sound": "ding", "volume": 5},
                    {"sound": "ding", "volume": -1}, {"sound": "30:10"}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                glowbug.compose_sound(bad)

    def test_own_release(self):
        lines, meta = glowbug.compose_own({"resources": ["led:1,ug2", "glass:all",
                                                         "sound", "enc"], "for": 3})
        self.assertEqual(lines, ["OWN LED 0 FOR 3000", "OWN LED 6 FOR 3000",
                                 "OWN GLASS ALL FOR 3000", "OWN SOUND FOR 3000",
                                 "OWN ENC FOR 3000"])
        self.assertEqual(meta, {"for_ms": 3000})
        self.assertEqual(glowbug.compose_own({"resources": "all"})[0], ["OWN ALL"])
        self.assertEqual(glowbug.compose_own({"resources": ["led", "glass:2"]})[0],
                         ["OWN LED ALL", "OWN GLASS 1"])
        self.assertEqual(glowbug.compose_release({})[0], ["RELEASE ALL"])
        self.assertEqual(glowbug.compose_release({"resources": []})[0], ["RELEASE ALL"])
        self.assertEqual(glowbug.compose_release({"resources": ["glass:3", "enc"]})[0],
                         ["RELEASE GLASS 2", "RELEASE ENC"])
        for bad in ({}, {"resources": ["screen:1"]}, {"resources": ["sound:1"]},
                    {"resources": [1]}, {"resources": ["led:7"]}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                glowbug.compose_own(bad)

    def test_settings(self):
        cs = glowbug.compose_settings
        self.assertEqual(cs({"op": "get"}),
                         (["GET brightness", "GET ug_brightness", "GET ug_mode",
                           "GET volume", "GET chime", "GET flip"],
                          {"op": "get", "keys": ["brightness", "ug_brightness", "ug_mode",
                                                 "volume", "chime", "flip"]}))
        self.assertEqual(cs({"op": "get", "key": " Volume "})[0], ["GET volume"])
        self.assertEqual(cs({"op": "set", "key": "brightness", "value": "60"})[0],
                         ["SET brightness 60", "GET brightness"])
        self.assertEqual(cs({"op": "set", "key": "flip", "value": "on"})[0][0], "SET flip 1")
        self.assertEqual(cs({"op": "set", "key": "flip", "value": False})[0][0], "SET flip 0")
        self.assertEqual(cs({"op": "set", "key": "volume", "value": 0})[0][0], "SET volume 0")
        self.assertEqual(cs({"op": "save"}), (["SAVE"], {"op": "save", "keys": []}))
        for bad in ({"op": "reset"}, {"op": "get", "key": "screen_mode"},
                    {"op": "set", "key": "brightness"}, {"op": "set", "value": 1},
                    {"op": "set", "key": "brightness", "value": 101},
                    {"op": "set", "key": "volume", "value": "loud"},
                    {"op": "set", "key": "volume", "value": True}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                cs(bad)

    def test_raw_line_filter(self):
        ok = glowbug.check_raw_lines
        self.assertEqual(ok("  LED 0 SET FF0000 \r\n"), ["LED 0 SET FF0000"])
        self.assertEqual(ok(["PING", "INFO"]), ["PING", "INFO"])
        self.assertEqual(ok(["x" * 1023]), ["x" * 1023])
        cases = [(["DFU"], "DFU"), (["dfu now"], "dfu"), (["REPLY ON"], "REPLY"),
                 (["reply off"], "reply"), (["x" * 1024], "1024 chars"),
                 (["café"], "printable ASCII"), (["a\x00b"], "printable ASCII"),
                 ([""], "empty"), (["   "], "empty"), ([], "non-empty"),
                 ([5], "not a string"), (["PING"] * 257, "257"),
                 (None, "non-empty")]
        for lines, needle in cases:
            with self.assertRaises(ValueError, msg=repr(lines)[:40]) as cm:
                ok(lines)
            self.assertIn(needle, str(cm.exception), repr(lines)[:40])
        with self.assertRaises(ValueError) as cm:
            ok(["x" * 1000] * 40)
        self.assertIn("too large", str(cm.exception))


# ---------------------------------------------------- dispatch (no sockets)
class DispatchTests(ApiCase):

    def test_gating(self):
        d = self.daemon(hello=None)
        for cmd in ("show", "led", "text", "sound", "raw", "own", "release", "settings"):
            r = d.handle_request({"cmd": cmd})
            self.assertEqual((r["ok"], r["api"], r["error"]), (False, 2, "no_board"), cmd)
        d.handle_board_line(HELLO3)
        r = d.handle_request(CANON_REQ)
        self.assertEqual(r["error"], "proto_too_old")
        self.assertEqual(r["message"], "board firmware 1.4.17 speaks PROTO 3; the API "
                         "needs PROTO 4 — run `glowbug rescue` to flash the bundled image")
        self.assertEqual(len(d.tx_queue), 0)          # nothing queued, no INFO either
        # report/info/palette always answer
        for cmd in ("report", "info", "palette"):
            self.assertTrue(d.handle_request({"cmd": cmd})["ok"], cmd)
        d.handle_board_line("EVT HELLO 1.0.0 PROTO 2")
        self.assertEqual(d.handle_request(CANON_REQ)["error"], "proto_too_old")
        d.handle_board_line("EVT HELLO 3.1.0 PROTO 5 SLOTS 32")   # a newer board is fine
        self.assertTrue(d.handle_request(CANON_REQ)["ok"])

    def test_show_queues_the_canonical_blob(self):
        d = self.daemon()
        r = d.handle_request(CANON_REQ)
        self.assertEqual(r, {"ok": True, "api": 2, "lines": CANON_LINES,
                             "held_until": self.clock.now + 5.0, "truncated": False})
        self.assertEqual(list(d.tx_queue), [("\n".join(CANON_LINES) + "\n").encode()])
        self.assertEqual(d.tx_bytes, len(d.tx_queue[0]))
        # the serial thread pulls whole blobs while under the low-water mark
        d.handle_request({"cmd": "sound", "sound": "blip"})
        self.assertEqual(len(d.tx_queue), 2)
        self.assertEqual(d._tx_take(glowbug.TX_API_LOWWATER), b"")
        got = d._tx_take(glowbug.TX_API_LOWWATER - 1)      # one blob crosses the mark
        self.assertEqual(got, ("\n".join(CANON_LINES) + "\n").encode())
        self.assertEqual(len(d.tx_queue), 1)
        self.assertEqual(d._tx_take(0), b"TONE 3136:45\n")
        self.assertEqual((len(d.tx_queue), d.tx_bytes), (0, 0))
        self.assertEqual(d._tx_take(0), b"")

    def test_brightness_from_the_board_scales_show_not_raw(self):
        d = self.daemon()
        d.handle_board_line("EVT SET brightness 50")
        self.assertEqual(d.settings, {"brightness": 50})
        self.assertEqual(d.handle_request(CANON_REQ)["lines"][2], "LED 2 SET 008000")
        self.assertEqual(d.handle_request(dict(CANON_REQ, raw=True))["lines"][2],
                         "LED 2 SET 00FF00")
        self.assertEqual(d.handle_request({"cmd": "led", "sel": "ug1", "color": "white"})
                         ["lines"], ["OWN LED 5", "LED 5 SET 808080"])
        d.handle_board_line("EVT SET brightness 250")        # nonsense -> 100
        self.assertEqual(d.handle_request(CANON_REQ)["lines"][2], "LED 2 SET 00FF00")

    def test_raw(self):
        d = self.daemon()
        r = d.handle_request({"cmd": "raw", "lines": ["LED 0 SET FF0000", "PING"]})
        self.assertEqual(r, {"ok": True, "api": 2, "lines": ["LED 0 SET FF0000", "PING"],
                             "queued": 2})
        self.assertEqual(self.queued(d), ["LED 0 SET FF0000", "PING"])
        self.assertEqual(d.handle_request({"cmd": "raw", "line": " INFO "})["lines"], ["INFO"])
        for bad in ({"lines": ["DFU"]}, {"lines": ["reply on"]}, {"line": "x" * 1024},
                    {"lines": ["café"]}, {"lines": []}, {}):
            r = d.handle_request(dict(bad, cmd="raw"))
            self.assertEqual(r["error"], "bad_arg", repr(bad))
        self.assertEqual(len(d.tx_queue), 2)                 # refusals queue nothing

    def test_busy_at_the_queue_cap(self):
        d = self.daemon()
        d.tx_bytes = glowbug.TX_API_MAX_BYTES - 10
        r = d.handle_request(CANON_REQ)
        self.assertEqual(r["error"], "busy")
        self.assertIn("not keeping up", r["message"])
        self.assertEqual(len(d.tx_queue), 0)
        d.tx_bytes = 0
        d.tx_queue.extend([b"x\n"] * glowbug.TX_API_MAX_ENTRIES)
        self.assertEqual(d.handle_request(CANON_REQ)["error"], "busy")
        self.assertEqual(d.cache, {})                        # nothing cached either

    def test_request_shape(self):
        d = self.daemon()
        self.assertEqual(d.handle_request("hi")["error"], "bad_request")
        self.assertEqual(d.handle_request({"cmd": "dance"})["error"], "unknown_cmd")
        self.assertEqual(d.handle_request({})["error"], "unknown_cmd")
        self.assertEqual(d.handle_request({"cmd": "info", "api": 3})["error"], "bad_request")
        self.assertEqual(d.handle_request({"cmd": "info", "api": "2"})["error"], "bad_request")
        self.assertTrue(d.handle_request({"cmd": "info", "api": 2})["ok"])
        self.assertTrue(d.handle_request({"cmd": "info", "api": 1})["ok"])
        self.assertEqual(d.handle_request({"cmd": "events"})["error"], "bad_request")
        for r in (d.handle_request({"cmd": "show", "screen": 9}),
                  d.handle_request({"cmd": "show", "screen": 1, "color": "nope"})):
            self.assertEqual(r["error"], "bad_arg")
            self.assertTrue(r["message"])

    def test_report_and_info(self):
        d = self.daemon(hello=None)
        d.last_event_at["claude"] = self.clock.now - 4.0
        r = d.handle_request({"cmd": "report"})
        self.assertEqual(set(r), {"version", "sessions", "last_event_at", "ok", "api", "board"})
        self.assertEqual((r["version"], r["sessions"], r["last_event_at"], r["board"]),
                         (glowbug.VERSION, [], {"claude": 4.0}, {"online": False}))
        self.assertEqual(set(d.report()), {"version", "sessions", "last_event_at"})
        r = d.handle_request({"cmd": "info"})
        self.assertEqual(r["board"], {"online": False})
        self.assertEqual(r["owned"], {"led": [], "glass": [], "sound": False, "enc": False})
        self.assertEqual((r["daemon"], r["api"], r["socket"]),
                         (glowbug.VERSION, 2, glowbug.SOCK_PATH))
        self.assertEqual(r["palette"], {"colors": len(glowbug.PALETTE),
                                        "sounds": len(glowbug.SOUNDS)})
        d.port = "/dev/cu.usbmodem1"
        d.handle_board_line(HELLO4)
        d.handle_board_line("EVT INFO FW 2.0.0 PROTO 4 HW 5 LEDS 10 GLASS 5 UG 5 SCREENS 5 "
                            "W 128 H 32 PAGES 4 FONTS 7,16,24 NOTES 32 TONEMAX 5000 "
                            "LINE 1024 SLOTS 32 STACK 3100 TXDROP 0 UP 42 NEWKEY x")
        d.handle_board_line("EVT OWN LED 041 GLASS 04 SOUND 0 ENC 1")
        r = d.handle_request({"cmd": "info"})
        b = r["board"]
        self.assertEqual((b["online"], b["port"], b["fw"], b["proto"], b["slots"]),
                         (True, "/dev/cu.usbmodem1", "2.0.0", 4, 32))
        self.assertEqual((b["leds"], b["glass"], b["fonts"], b["stack"], b["newkey"]),
                         (10, 5, "7,16,24", 3100, "x"))
        self.assertEqual(r["owned"], {"led": ["1", "ug2"], "glass": [3], "sound": False,
                                      "enc": True})
        self.assertTrue(any("EVT INFO" in m for m in self.logs))

    def test_palette_command(self):
        d = self.daemon(hello=None)
        r = d.handle_request({"cmd": "palette"})
        self.assertEqual(r["colors"]["green"], "00FF00")
        self.assertEqual(r["sounds"]["blip"], [[3136, 45]])
        self.assertEqual(r["files"], {"palette": glowbug.PALETTE_PATH,
                                      "sounds": glowbug.SOUNDS_PATH})
        self.assertEqual(r["warnings"], [])
        with open(glowbug.PALETTE_PATH, "w") as f:
            json.dump({"brand": "#123456", "bad": "zz"}, f)
        self.assertNotIn("brand", d.handle_request({"cmd": "palette"})["colors"])
        r = d.handle_request({"cmd": "palette", "reload": True})
        self.assertEqual(r["colors"]["brand"], "123456")
        self.assertEqual(len(r["warnings"]), 1)
        d.handle_board_line(HELLO4)
        self.assertEqual(d.handle_request({"cmd": "show", "screen": 1, "color": "brand"})
                         ["lines"][1], "LED 0 SET 123456")


# ------------------------------------------------------------- events
class EventTests(ApiCase):

    def test_every_evt_line_becomes_an_event(self):
        d = self.daemon(hello=None)
        sub = d.subscribe()
        d.handle_board_line("EVT ENC -3")
        d.handle_board_line("EVT CLICK")
        d.handle_board_line("EVT HOLD")
        d.handle_board_line("EVT MENU 1")
        d.handle_board_line("EVT SET volume 2")
        d.handle_board_line("EVT OWN LED 3ff GLASS 1f SOUND 1 ENC 0")
        d.handle_board_line("EVT WEATHER sunny")
        d.handle_board_line("EVT ENC lots")
        d.handle_board_line("ERR TEXT NOTOWNED")
        d.handle_board_line("OK PING")
        d.handle_board_line("garbage")
        self.assertEqual(self.drain(sub), [
            {"event": "enc", "delta": -3},
            {"event": "click"},
            {"event": "hold"},
            {"event": "menu", "open": True},
            {"event": "set", "key": "volume", "value": 2},
            {"event": "own", "led": [str(i) for i in range(1, 6)] +
             ["ug%d" % i for i in range(1, 6)], "glass": [1, 2, 3, 4, 5],
             "sound": True, "enc": False},
            {"event": "evt", "raw": "EVT WEATHER sunny"},
            {"event": "evt", "raw": "EVT ENC lots"},
            {"event": "err", "verb": "TEXT", "text": "ERR TEXT NOTOWNED"},
        ])
        self.assertEqual(d.settings, {"volume": 2})
        self.assertEqual(d.owned, {"led": 0x3ff, "glass": 0x1f, "sound": 1, "enc": 0})
        # an ERR from an unidentified board is not logged (1.5.0 behaviour pinned
        # by test_other_board_lines_are_ignored_today); a known board's is
        self.assertEqual(self.logs, [])
        d.handle_board_line(HELLO4)
        d.handle_board_line("ERR SAVE ratelimit 7")
        self.assertIn("board: ERR SAVE ratelimit 7", self.logs)

    def test_hello_events_info_and_filters(self):
        d = self.daemon(hello=None)
        everything = d.subscribe()
        only_enc = d.subscribe({"enc"})
        d.handle_board_line(HELLO4)
        self.assertEqual(self.drain(everything), [
            {"event": "hello", "fw": "2.0.0", "proto": 4, "slots": 32},
            {"event": "board", "online": True, "fw": "2.0.0", "proto": 4},
            {"event": "redraw", "reason": "hello", "lines": 0},
        ])
        self.assertEqual(d.board, {"online": True, "port": None, "fw": "2.0.0",
                                   "proto": 4, "slots": 32})
        self.assertEqual(self.queued(d), ["INFO", "GET brightness"])   # proto >= 4 only
        d.handle_board_line("EVT ENC 1")
        self.assertEqual(self.drain(only_enc), [{"event": "enc", "delta": 1}])
        self.assertEqual(self.drain(everything), [{"event": "enc", "delta": 1}])
        d.unsubscribe(only_enc)
        d.handle_board_line("EVT ENC 2")
        self.assertEqual(self.drain(only_enc), [])
        self.assertEqual(self.drain(everything), [{"event": "enc", "delta": 2}])
        # a PROTO 3 HELLO sets the board but solicits nothing
        d.tx_queue.clear()
        d.tx_bytes = 0
        d.handle_board_line(HELLO3)
        self.assertEqual(d.board["proto"], 3)
        self.assertEqual(len(d.tx_queue), 0)
        self.assertEqual([e["event"] for e in self.drain(everything)], ["hello", "board"])
        d.handle_board_line("EVT HELLO 1 PROTO 3 SLOTS")             # garbage: fw "1"
        self.assertEqual(d.board["fw"], "1")
        d.handle_board_line("EVT HELLO PROTO SLOTS")
        self.assertEqual((d.board["fw"], d.board["proto"]), ("?", 0))

    def test_slow_subscriber_is_dropped(self):
        d = self.daemon(hello=None)
        sub = d.subscribe()
        for i in range(glowbug.SUB_QUEUE):
            d.handle_board_line("EVT ENC 1")
        self.assertFalse(sub.dropped)
        self.assertIn(sub, d.subscribers)
        d.handle_board_line("EVT ENC 1")
        self.assertTrue(sub.dropped)
        self.assertNotIn(sub, d.subscribers)
        self.assertEqual(sub.q.qsize(), glowbug.SUB_QUEUE)
        # and the cap on streams
        subs = [d.subscribe() for _ in range(glowbug.MAX_SUBSCRIBERS)]
        self.assertTrue(all(subs))
        self.assertIsNone(d.subscribe())

    def test_echo_barrier_attributes_errs(self):
        d = self.daemon()
        result = {}

        def run():
            try:
                result["errs"] = d._barrier(["TEXT 2 x"], timeout=5.0)
            except glowbug.GlowbugError as e:
                result["err"] = e
        t = threading.Thread(target=run)
        t.start()
        end = _real_time.time() + 5
        while not d.waiters and _real_time.time() < end:
            _real_time.sleep(0.01)
        self.assertEqual(list(d.waiters), ["h1"])
        self.assertEqual(self.queued(d), ["TEXT 2 x", "ECHO h1"])
        d.handle_board_line("ERR TEXT NOTOWNED")
        d.handle_board_line("EVT ECHO h1")
        t.join(5)
        self.assertEqual(result, {"errs": ["ERR TEXT NOTOWNED"]})
        self.assertEqual(d.waiters, {})
        # a stray ECHO is just an event; a timeout is a clean error
        sub = d.subscribe()
        d.handle_board_line("EVT ECHO nobody")
        self.assertEqual(self.drain(sub), [{"event": "evt", "raw": "EVT ECHO nobody"}])
        with self.assertRaises(glowbug.GlowbugError) as cm:
            d._barrier(["PING"], timeout=0.05)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertEqual(d.waiters, {})
        # the commands that ride on it
        r = d.handle_request({"cmd": "raw", "lines": ["PING"], "confirm": True})
        self.assertEqual(r["error"], "timeout")

    def test_settings_roundtrip_over_the_barrier(self):
        d = self.daemon()
        result = {}

        def run():
            result["get"] = d.handle_request({"cmd": "settings", "op": "get",
                                              "key": "brightness"})
            result["set"] = d.handle_request({"cmd": "settings", "op": "set",
                                              "key": "flip", "value": "on"})
            result["save"] = d.handle_request({"cmd": "settings", "op": "save"})
            result["save2"] = d.handle_request({"cmd": "settings", "op": "save"})
        t = threading.Thread(target=run)
        t.start()

        def answer(token, *lines):
            end = _real_time.time() + 5
            while token not in d.waiters and _real_time.time() < end:
                _real_time.sleep(0.01)
            self.assertIn(token, d.waiters)
            for line in lines:
                d.handle_board_line(line)
            d.handle_board_line("EVT ECHO " + token)
        answer("h1", "EVT SET brightness 70")
        answer("h2", "EVT SET flip 1")
        answer("h3")
        answer("h4", "ERR SAVE ratelimit 9")
        t.join(5)
        self.assertEqual(result["get"], {"ok": True, "api": 2, "settings": {"brightness": 70}})
        self.assertEqual(result["set"]["settings"], {"flip": 1})
        self.assertEqual(result["save"], {"ok": True, "api": 2, "saved": True})
        self.assertEqual((result["save2"]["error"], result["save2"]["errors"]),
                         ("board_err", ["ERR SAVE ratelimit 9"]))
        self.assertEqual(self.queued(d), ["GET brightness", "ECHO h1", "SET flip 1",
                                          "GET flip", "ECHO h2", "SAVE", "ECHO h3",
                                          "SAVE", "ECHO h4"])

    def test_board_gone(self):
        d = self.daemon()
        sub = d.subscribe()
        d.handle_request(CANON_REQ)
        result = {}

        def run():
            try:
                d._barrier(["PING"], timeout=5.0)
            except glowbug.GlowbugError as e:
                result["code"] = e.code
        t = threading.Thread(target=run)
        t.start()
        end = _real_time.time() + 5
        while not d.waiters and _real_time.time() < end:
            _real_time.sleep(0.01)
        d._board_gone()
        t.join(5)
        self.assertEqual(result, {"code": "no_board"})
        self.assertIsNone(d.board)
        self.assertEqual((len(d.tx_queue), d.tx_bytes, d.board_info), (0, 0, {}))
        self.assertEqual(self.drain(sub), [{"event": "board", "online": False}])
        self.assertEqual(d.handle_request(CANON_REQ)["error"], "no_board")
        d._board_gone()                                      # idempotent, no event
        self.assertEqual(self.drain(sub), [])
        self.assertNotEqual(d.cache, {})                     # replayed on the next HELLO


# ------------------------------------------------------------- replay
class ReplayTests(ApiCase):

    def test_menu_replays_glass_content_only(self):
        d = self.daemon()
        sub = d.subscribe({"redraw", "menu"})
        d.handle_request(CANON_REQ)
        d.handle_request({"cmd": "led", "sel": "ug1", "color": "red"})
        d.tx_queue.clear()
        d.tx_bytes = 0
        d.handle_board_line("EVT MENU 1")
        self.assertEqual(self.queued(d), [])
        d.handle_board_line("EVT MENU 0")
        self.assertEqual(self.queued(d), ["GET brightness", "TEXT 2 Build OK"])
        self.assertEqual(self.drain(sub), [{"event": "menu", "open": True},
                                           {"event": "menu", "open": False},
                                           {"event": "redraw", "reason": "menu", "lines": 1}])

    def test_hello_replays_own_with_remaining_time_and_skips_expired(self):
        d = self.daemon()
        d.handle_request(CANON_REQ)                            # FOR 5000
        d.handle_request({"cmd": "led", "sel": "ug1", "color": "red"})   # indefinite
        d.handle_request({"cmd": "own", "resources": ["glass:5"], "for": 1})
        d.handle_request({"cmd": "raw", "lines": ["OWN GLASS 3",
                                                  "BLIT 3 0 " + "A" * 172,
                                                  "BLIT 3 1 " + "B" * 172,
                                                  "BLIT 3 0 " + "C" * 172]})
        d.handle_request({"cmd": "text", "screen": 5, "line1": "gone soon", "for": 1})
        self.assertEqual(d.cache[("glass", "4")]["content"], ["TEXT 4 gone soon"])
        self.clock.advance(2.0)
        d.tx_queue.clear()
        d.tx_bytes = 0
        sub = d.subscribe({"redraw"})
        d.handle_board_line(HELLO4)
        # OWN lines first (remaining time recomputed), then content, in the
        # order the resources were first claimed; a page BLIT replaces its
        # earlier page; glass 5 (wire 4) died with its 1 s claim
        self.assertEqual(self.queued(d), [
            "INFO", "GET brightness",
            "OWN GLASS 2 FOR 3000", "OWN LED 2 FOR 3000", "OWN LED 5", "OWN GLASS 3",
            "TEXT 2 Build OK", "LED 2 SET 00FF00", "LED 5 SET FF0000",
            "BLIT 3 1 " + "B" * 172, "BLIT 3 0 " + "C" * 172])
        self.assertEqual(self.drain(sub), [{"event": "redraw", "reason": "hello", "lines": 9}])
        self.assertNotIn(("glass", "4"), d.cache)
        # the 5 s claims expire too
        self.clock.advance(3.5)
        d.tx_queue.clear()
        d.tx_bytes = 0
        d.handle_board_line(HELLO4)
        self.assertEqual(self.queued(d), ["INFO", "GET brightness", "OWN LED 5", "OWN GLASS 3",
                                          "LED 5 SET FF0000",
                                          "BLIT 3 1 " + "B" * 172, "BLIT 3 0 " + "C" * 172])
        self.assertEqual(set(d.cache), {("led", "5"), ("glass", "3")})

    def test_evt_own_prunes_after_the_grace_period(self):
        d = self.daemon()
        d.handle_request(CANON_REQ)
        self.assertEqual(set(d.cache), {("glass", "2"), ("led", "2")})
        d.handle_board_line("EVT OWN LED 000 GLASS 00 SOUND 0 ENC 0")   # stale, in flight
        self.assertEqual(len(d.cache), 2)
        d.handle_board_line("EVT OWN LED 004 GLASS 04 SOUND 0 ENC 0")
        self.clock.advance(1.0)
        d.handle_board_line("EVT OWN LED 004 GLASS 04 SOUND 0 ENC 0")   # still owned
        self.assertEqual(len(d.cache), 2)
        d.handle_board_line("EVT OWN LED 004 GLASS 00 SOUND 0 ENC 0")   # glass released
        self.assertEqual(set(d.cache), {("led", "2")})
        d.handle_board_line("EVT OWN LED 000 GLASS 00 SOUND 0 ENC 0")
        self.assertEqual(d.cache, {})
        # group entries need every member owned
        d.handle_request({"cmd": "led", "sel": "glass", "color": "red"})
        self.clock.advance(1.0)
        d.handle_board_line("EVT OWN LED 01f GLASS 00 SOUND 0 ENC 0")
        self.assertEqual(set(d.cache), {("led", "GLASS")})
        d.handle_board_line("EVT OWN LED 01e GLASS 00 SOUND 0 ENC 0")
        self.assertEqual(d.cache, {})

    def test_release_drops_cache(self):
        d = self.daemon()
        d.handle_request(CANON_REQ)
        d.handle_request({"cmd": "led", "sel": "all", "color": "red"})
        self.assertEqual(set(d.cache), {("glass", "2"), ("led", "2"), ("led", "ALL")})
        d.handle_request({"cmd": "release", "resources": ["led:3"]})
        self.assertEqual(set(d.cache), {("glass", "2")})      # ALL shares index 2
        d.handle_request({"cmd": "raw", "lines": ["OWN GLASS 1", "TEXT 1 hi", "CLEAR 2"]})
        self.assertEqual(d.cache[("glass", "1")]["content"], ["TEXT 1 hi"])
        self.assertEqual(d.cache[("glass", "2")]["content"], ["CLEAR 2"])
        d.handle_request({"cmd": "release"})
        self.assertEqual(d.cache, {})
        self.assertEqual(self.queued(d)[-1], "RELEASE ALL")


# ---------------------------------------------------- socket end-to-end
class SocketTests(ApiCase):

    def test_socket_loop_end_to_end(self):
        d = self.daemon()
        hooks = []
        d.handle_hook = hooks.append
        t = threading.Thread(target=d.socket_loop, daemon=True)
        t.start()
        end = _real_time.time() + 5
        while not os.path.exists(glowbug.SOCK_PATH) and _real_time.time() < end:
            _real_time.sleep(0.02)
        try:
            self.assertEqual(oct(os.stat(glowbug.SOCK_PATH).st_mode & 0o777), "0o600")
            r = glowbug._call({"cmd": "info"})
            self.assertEqual((r["ok"], r["api"], r["board"]["proto"]), (True, 2, 4))
            # a client that hangs up before reading must not kill the loop
            for _ in range(3):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(glowbug.SOCK_PATH)
                s.sendall(b'{"cmd":"info"}\n')
                s.close()
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)   # and one silent
            s.connect(glowbug.SOCK_PATH)
            s.close()
            self.assertEqual(glowbug.show(3, color="green", line1="Build OK",
                                          sound="ding", seconds=5)["lines"], CANON_LINES)
            self.assertEqual(self.queued(d)[-5:], CANON_LINES)
            # a hook payload: JSON without a newline, then close (the forwarder)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(glowbug.SOCK_PATH)
            s.sendall(b'{"source": "codex", "hook_event_name": "Stop", "session_id": "abc"}')
            s.close()
            end = _real_time.time() + 5
            while not hooks and _real_time.time() < end:
                _real_time.sleep(0.02)
            self.assertEqual(hooks, [{"source": "codex", "hook_event_name": "Stop",
                                      "session_id": "abc"}])
            # the 1.5.0 client (no newline, shutdown, read to EOF) still works
            rep = glowbug.ask_daemon()
            self.assertEqual(rep["version"], glowbug.VERSION)
            self.assertIn("sessions", rep)
            # errors come back as replies, not dropped connections
            with self.assertRaises(glowbug.GlowbugError) as cm:
                glowbug.raw("DFU")
            self.assertEqual(cm.exception.code, "bad_arg")
            # events: one header line, then a stream
            got = []

            def reader():
                for ev in glowbug.events(["enc", "click"]):
                    got.append(ev)
                    if len(got) == 2:
                        return
            rt = threading.Thread(target=reader, daemon=True)
            rt.start()
            end = _real_time.time() + 5
            while not d.subscribers and _real_time.time() < end:
                _real_time.sleep(0.02)
            self.assertEqual(len(d.subscribers), 1)
            d.handle_board_line("EVT HOLD")                  # filtered out
            d.handle_board_line("EVT ENC 1")
            d.handle_board_line("EVT CLICK")
            rt.join(5)
            self.assertEqual(got, [{"event": "enc", "delta": 1}, {"event": "click"}])
            end = _real_time.time() + 5                      # the reader's close is noticed
            while d.subscribers and _real_time.time() < end:
                _real_time.sleep(0.02)
            self.assertEqual(d.subscribers, [])
        finally:
            d.stop_event.set()
            t.join(5)
        self.assertFalse(t.is_alive())
        with self.assertRaises(glowbug.GlowbugError) as cm:
            glowbug.info()
        self.assertEqual(cm.exception.code, "no_daemon")


# ------------------------------------------ serial thread hand-off (real pty)
@unittest.skipUnless(hasattr(os, "openpty"), "needs a pty")
class SerialApiTests(ApiCase):
    """The design's one hard threading rule: only the serial thread ever
    writes to the port. handle_request() is called here from the test
    thread; the bytes must still come out of the pty, behind HELLO / INFO,
    pulled by serial_loop's _tx_take. The test plays the board on the
    master side, like SerialLoopTests."""

    def setUp(self):
        super().setUp()
        self._patched = {k: getattr(glowbug, k) for k in
                         ("find_port", "hid_idle_s", "wire_sources", "running_sources")}
        self.port = {"path": None}
        glowbug.find_port = lambda: self.port["path"]
        glowbug.hid_idle_s = lambda: None
        glowbug.wire_sources = lambda explicit=False: []
        glowbug.running_sources = lambda: set()
        self.loop_clock = LoopClock()
        glowbug.time = self.loop_clock
        self.acc = b""

    def tearDown(self):
        self.loop_clock.stop.set()
        for k, v in self._patched.items():
            setattr(glowbug, k, v)
        super().tearDown()

    def expect(self, fd, needle, timeout=10):
        """Read the pty until `needle` shows up in everything read so far."""
        import select as _select
        end = _real_time.time() + timeout
        while needle not in self.acc and _real_time.time() < end:
            r, _, _ = _select.select([fd], [], [], 0.1)
            if r:
                self.acc += os.read(fd, 4096)
        self.assertIn(needle, self.acc)

    def test_api_lines_reach_the_port_only_via_the_serial_thread(self):
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
            self.expect(master, b"HELLO\n")
            os.write(master, b"EVT HELLO 2.0.0 PROTO 4 SLOTS 32\n")
            self.expect(master, b"INFO\nGET brightness\n")   # solicited on PROTO 4
            self.assertEqual(d.board["port"], self.port["path"])
            os.write(master, b"EVT SET brightness 50\n")
            end = _real_time.time() + 10
            while d.settings.get("brightness") != 50 and _real_time.time() < end:
                _real_time.sleep(0.02)
            self.assertEqual(d.settings.get("brightness"), 50)
            self.acc = b""
            r = d.handle_request(CANON_REQ)                  # this thread only queues
            self.assertTrue(r["ok"])
            self.expect(master, b"TONE 2637:70,0:20,3951:260\n")
            burst = ("OWN GLASS 2 FOR 5000\nOWN LED 2 FOR 5000\nLED 2 SET 008000\n"
                     "TEXT 2 Build OK\nTONE 2637:70,0:20,3951:260\n").encode()
            self.assertIn(burst, self.acc)                   # contiguous, brightness-scaled
            self.assertEqual((len(d.tx_queue), d.tx_bytes), (0, 0))
            # the device menu closed: the daemon repaints the owned glass
            self.acc = b""
            os.write(master, b"EVT MENU 0\n")
            self.expect(master, b"TEXT 2 Build OK\n")
            self.assertNotIn(b"LED 2 SET", self.acc)          # LEDs come back by themselves
            # an ECHO barrier round trip through the real port
            result = {}

            def confirm():
                result["rep"] = d.handle_request({"cmd": "raw", "lines": ["INFO"],
                                                  "confirm": True})
            ct = threading.Thread(target=confirm, daemon=True)
            ct.start()
            self.acc = b""
            self.expect(master, b"INFO\nECHO h1\n")
            os.write(master, b"EVT ECHO h1\n")
            ct.join(5)
            self.assertEqual(result["rep"], {"ok": True, "api": 2, "lines": ["INFO"],
                                             "queued": 1, "confirmed": True})
        finally:
            self.loop_clock.stop.set()
            t.join(5)
            os.close(master)
            os.close(slave)


# ------------------------------------------------------------------ CLI
class CliTests(ApiCase):

    def test_parser_covers_every_command(self):
        p = glowbug.build_parser()
        for argv in (["install"], ["uninstall"], ["status"], ["doctor"], ["rescue"],
                     ["version"], ["show", "3"], ["led", "ug2", "red", "--mode", "pulse"],
                     ["text", "all", "hi", "there", "--big"], ["sound", "ding"],
                     ["raw", "PING", "INFO", "--confirm"], ["own", "led:1", "--for", "2"],
                     ["release"], ["events", "--filter", "enc,click"], ["info", "--json"],
                     ["palette", "--reload"], ["settings", "get"],
                     ["settings", "set", "flip", "1"], ["settings", "save"]):
            self.assertEqual(p.parse_args(argv).cmd, argv[0], argv)
        for argv in (["bogus"], ["show"], ["led", "1"], ["sound"], ["raw"],
                     ["settings", "reset"], ["show", "3", "--mode", "wobble"],
                     ["show", "3", "--volume", "5"]):
            with self.assertRaises(SystemExit, msg=argv) as cm:
                with self.redirect_stderr():
                    p.parse_args(argv)
            self.assertEqual(cm.exception.code, 2, argv)

    def redirect_stderr(self):
        import contextlib
        return contextlib.redirect_stderr(io.StringIO())

    def test_requests_from_args(self):
        p = glowbug.build_parser()
        req = glowbug._cli_request(p.parse_args(["show", "3", "--for", "5"]))
        self.assertEqual(req, {"cmd": "show", "screen": "3", "for": 5.0})
        req = glowbug._cli_request(p.parse_args(
            ["show", "all", "-c", "green", "--line1", "Build OK", "-s", "ding",
             "--for", "5", "--raw", "--no-led", "--mode", "pulse", "--to", "#000",
             "--period", "900", "--volume", "0"]))
        self.assertEqual(req, {"cmd": "show", "screen": "all", "color": "green",
                               "line1": "Build OK", "sound": "ding", "for": 5.0,
                               "raw": True, "no_led": True, "mode": "pulse",
                               "to": "#000", "period": 900, "volume": 0})
        self.assertEqual(glowbug._cli_request(p.parse_args(["text", "2", "BIG", "--big"])),
                         {"cmd": "text", "screen": "2", "big": "BIG"})
        self.assertEqual(glowbug._cli_request(p.parse_args(["text", "2", "a", "b"])),
                         {"cmd": "text", "screen": "2", "line1": "a", "line2": "b"})
        self.assertEqual(glowbug._cli_request(p.parse_args(["led", "1,ug1", "red",
                                                            "--fade-ms", "300"])),
                         {"cmd": "led", "sel": "1,ug1", "color": "red", "fade_ms": 300})
        self.assertEqual(glowbug._cli_request(p.parse_args(["raw", "PING", "--confirm"])),
                         {"cmd": "raw", "lines": ["PING"], "confirm": True})
        self.assertEqual(glowbug._cli_request(p.parse_args(["release"])),
                         {"cmd": "release"})
        self.assertEqual(glowbug._cli_request(p.parse_args(["own", "all", "--for", "1"])),
                         {"cmd": "own", "resources": ["all"], "for": 1.0})
        self.assertEqual(glowbug._cli_request(p.parse_args(["settings", "set", "flip", "on"])),
                         {"cmd": "settings", "op": "set", "key": "flip", "value": "on"})
        self.assertEqual(glowbug._cli_request(p.parse_args(["settings", "get"])),
                         {"cmd": "settings", "op": "get"})
        with self.assertRaises(glowbug.GlowbugError):
            glowbug._cli_request(p.parse_args(["settings", "set", "flip"]))
        # raw - reads stdin
        saved = sys.stdin
        try:
            sys.stdin = io.StringIO("PING\n\n  INFO  \n")
            self.assertEqual(glowbug._cli_request(p.parse_args(["raw", "-"]))["lines"],
                             ["PING", "  INFO  "])
        finally:
            sys.stdin = saved

    def test_main_exit_codes(self):
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            self.assertEqual(glowbug.main(["version"]), 0)
        self.assertEqual(out.getvalue(), "glowbug %s\n" % glowbug.VERSION)
        with self.assertRaises(SystemExit) as cm:
            with self.redirect_stderr():
                glowbug.main(["bogus"])
        self.assertEqual(cm.exception.code, 2)
        with self.assertRaises(SystemExit) as cm:
            with self.redirect_stderr():
                glowbug.main(["settings", "set", "flip"])
        self.assertEqual(cm.exception.code, 2)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(glowbug.main(["info"]), 3)      # no daemon on the temp sock
        self.assertTrue(err.getvalue().startswith("glowbug: the Glowbug daemon isn't running"))
        self.assertEqual(glowbug._human("show", {"lines": CANON_LINES, "held_until": None,
                                                 "truncated": True}),
                         "ok (5 lines queued) — text clipped to 21 chars")
        self.assertEqual(glowbug._human("sound", {"duration_ms": 0}), "hushed")
        # the Python helpers build the documented requests
        self.assertEqual(glowbug._req("show", screen=3, color=None, raw=False, seconds=5,
                                      volume=0),
                         {"cmd": "show", "screen": 3, "for": 5, "volume": 0})


# --------------------------------------------------------------- rescue
class RescueTests(ApiCase):

    @staticmethod
    def app_image(size=4096, reset=0x08000911, magic=b"GLWA"):
        img = bytearray(size)
        img[0:4] = (0x20004000).to_bytes(4, "little")
        img[4:8] = reset.to_bytes(4, "little")
        img[0xC0:0xC4] = magic
        return bytes(img)

    def test_check_app_image(self):
        root = self.tmp.name
        good = os.path.join(root, "good.bin")
        with open(good, "wb") as f:
            f.write(self.app_image())
        self.assertIsNone(glowbug.check_app_image(good))
        cases = [("tiny", b"\x00" * 100, "too small"),
                 ("nomagic", self.app_image(magic=b"GLWB"), "GLWA"),
                 ("lowvec", self.app_image(reset=0x08000401), "reset vector"),
                 ("highvec", self.app_image(reset=0x0800F801), "reset vector"),
                 ("big", self.app_image(size=61441), "does not fit")]
        for name, data, needle in cases:
            p = os.path.join(root, name + ".bin")
            with open(p, "wb") as f:
                f.write(data)
            self.assertIn(needle, glowbug.check_app_image(p) or "", name)
        self.assertIsNone(glowbug.check_app_image(
            self._write("edge.bin", self.app_image(size=61440, reset=0x0800F7FF))))
        self.assertIn("unreadable", glowbug.check_app_image(os.path.join(root, "nope")))
        # the 1.4.x whole-flash image in the repo is refused (page 0 = bootloader)
        old = os.path.join(REPO, "firmware", "glowbug.bin")
        if os.path.exists(old):
            self.assertIn("GLWA", glowbug.check_app_image(old))
        self.assertEqual((glowbug.APP_FLASH_ADDR, glowbug.APP_MAGIC_OFFSET, glowbug.APP_MAGIC),
                         (0x08000800, 0xC0, b"GLWA"))

    def _write(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_rescue_refuses_non_app_and_flashes_at_0x08000800(self):
        calls = []
        patched = {k: getattr(glowbug, k) for k in
                   ("_find_firmware", "_dfu_present", "find_port", "PLIST_PATH")}
        which = glowbug.shutil.which
        run = glowbug.subprocess.run

        class R:
            returncode = 0
            stdout = "File downloaded successfully\n"
            stderr = ""

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return R()
        try:
            glowbug.shutil.which = lambda name: "/opt/homebrew/bin/dfu-util"
            glowbug.subprocess.run = fake_run
            glowbug._dfu_present = lambda: True
            glowbug.find_port = lambda: "/dev/cu.usbmodem1"
            glowbug.PLIST_PATH = os.path.join(self.tmp.name, "no.plist")
            bad = self._write("old.bin", self.app_image(magic=b"\x02\xb4qF"))
            glowbug._find_firmware = lambda: (bad, "1.4.17")
            out = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    glowbug.rescue()
            self.assertIn("Refusing to flash", str(cm.exception))
            self.assertIn("GLWA", str(cm.exception))
            self.assertEqual(calls, [])                      # dfu-util never ran
            good = self._write("app.bin", self.app_image())
            glowbug._find_firmware = lambda: (good, "2.0.0")
            with contextlib.redirect_stdout(out):
                glowbug.rescue()
            self.assertEqual(calls, [["dfu-util", "-a", "0", "-s", "0x08000800:leave",
                                      "-D", good]])
            self.assertIn("restored (fw 2.0.0)", out.getvalue())
        finally:
            glowbug.shutil.which = which
            glowbug.subprocess.run = run
            for k, v in patched.items():
                setattr(glowbug, k, v)


if __name__ == "__main__":
    unittest.main()


class PercentInTextTests(unittest.TestCase):
    """Regression (2026-09-15): user text is appended after the verb/screen
    prefix is formatted, so '%' in text never reaches a %-format."""

    def test_percent_in_line1_and_big(self):
        import glowbug
        lines, tr = glowbug.compose_text_lines(["3"], {"line1": "42% done", "line2": "%d%s"})
        self.assertEqual(lines, ["TEXT 3 42% done|%d%s"])
        self.assertFalse(tr)
        lines, _ = glowbug.compose_text_lines(["0", "4"], {"big": "100%"})
        self.assertEqual(lines, ["BIG 0 100%", "BIG 4 100%"])
        lines, _ = glowbug.compose_text_lines(["1"], {"line1": ""})
        self.assertEqual(lines, ["CLEAR 1"])
