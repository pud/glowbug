#!/usr/bin/env python3
"""Offline tests for the pure helpers in tools/conformance.py — no board,
no daemon, no sockets. The hardware checks themselves need a Glowbug.

    python3 -m unittest tests.test_conformance_offline -v
"""

import base64
import os
import random
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, REPO)
import conformance as C  # noqa: E402


class PagePattern(unittest.TestCase):
    def test_page_is_one_padded_page(self):
        s = C.page_b64(0, 0)
        self.assertEqual(len(s), 172)
        self.assertTrue(s.endswith("="))
        self.assertEqual("=", s[-1])
        self.assertNotIn("=", s[:-1])
        self.assertEqual(len(base64.b64decode(s)), 128)

    def test_frame_is_four_pages(self):
        s = C.frame_b64(5, 3)
        self.assertEqual(len(s), 684)
        raw = base64.b64decode(s)
        self.assertEqual(len(raw), 512)
        self.assertEqual(raw, b"".join(C.page_bytes(5, p, 3) for p in range(4)))

    def test_pattern_moves_and_differs(self):
        self.assertNotEqual(C.page_bytes(0, 0, 0), C.page_bytes(1, 0, 0))
        self.assertNotEqual(C.page_bytes(0, 0, 0), C.page_bytes(0, 1, 0))
        self.assertNotEqual(C.page_bytes(0, 0, 0), C.page_bytes(0, 0, 1))
        self.assertEqual(C.page_bytes(7, 2, 4), C.page_bytes(7, 2, 4))   # deterministic

    def test_b64_junk_alphabet_and_length(self):
        for n in (171, 173, 683, 685):
            s = C.b64_junk(n)
            self.assertEqual(len(s), n)
            self.assertTrue(all(c in C.B64_ALPHABET for c in s))


class Parsers(unittest.TestCase):
    INFO = ("EVT INFO FW 2.0.0 PROTO 4 HW 5 LEDS 10 GLASS 5 UG 5 SCREENS 5 W 128 H 32 PAGES 4 "
            "FONTS 7,16,24 NOTES 32 TONEMAX 5000 LINE 1024 SLOTS 32 STACK 3120 HEAP 0 TXDROP 0 UP 42")

    def test_parse_info(self):
        d = C.parse_info(self.INFO)
        self.assertEqual(d["FW"], "2.0.0")
        self.assertEqual(d["PROTO"], 4)
        self.assertEqual(d["FONTS"], "7,16,24")
        self.assertEqual(d["STACK"], 3120)
        self.assertEqual(d["UP"], 42)
        for k, v in C.INFO_EXPECT.items():
            self.assertEqual(d[k], v, k)
        self.assertIsNone(C.parse_info("EVT ECHO x"))
        self.assertIsNone(C.parse_info("EVT INFO FW 2.0.0 PROTO"))    # odd pairs

    def test_parse_own_round_trip(self):
        line = C.own_line(0x3FF, 0x1F, 1, 0)
        self.assertEqual(line, "EVT OWN LED 3FF GLASS 1F SOUND 1 ENC 0")
        self.assertEqual(C.parse_own(line), {"led": 0x3FF, "glass": 0x1F, "sound": 1, "enc": 0})
        self.assertEqual(C.own_line(), "EVT OWN LED 000 GLASS 00 SOUND 0 ENC 0")
        self.assertEqual(C.parse_own(C.own_line(0x091, 0x0A)), {"led": 0x91, "glass": 0xA, "sound": 0, "enc": 0})
        self.assertIsNone(C.parse_own("EVT OWN LED 3ff GLASS 1F SOUND 1 ENC 0"))   # lower-case hex is not the wire
        self.assertIsNone(C.parse_own("OK OWN"))

    def test_parse_launchctl_pid(self):
        out = '{\n\t"LimitLoadToSessionType" = "Aqua";\n\t"Label" = "dev.glowbug.daemon";\n\t"PID" = 4242;\n};\n'
        self.assertEqual(C.parse_launchctl_pid(out), 4242)
        self.assertIsNone(C.parse_launchctl_pid('{\n\t"Label" = "dev.glowbug.daemon";\n};\n'))
        self.assertIsNone(C.parse_launchctl_pid(""))


class FuzzGenerators(unittest.TestCase):
    def test_is_dangerous_segment(self):
        for bad in (b"DFU", b"RESET", b"RESET now", b" RESET", b"SAVE", b"SAVE x",
                    b"RESET\x00junk", b"DFU\x00tail", b"SAVE\x00"):
            self.assertTrue(C.is_dangerous_segment(bad), bad)
        for ok in (b"", b" DFU", b"DFUX", b"RESETX", b"SAVEME", b"dfu", b"reset", b"RESET\tx",
                   b"\x00RESET", b"ECHO RESET", b"LED 0 SET red", b"HELLO"):
            self.assertFalse(C.is_dangerous_segment(ok), ok)

    def test_scrub_raw_keeps_length_and_terminators(self):
        chunk = b"abc\nRESET\r\nDFU\nSAVE 1\rxyz\x00RESET\nRESET\x00q"
        out = C.scrub_raw(chunk)
        self.assertEqual(len(out), len(chunk))
        self.assertEqual([i for i, b in enumerate(out) if b in (0x0A, 0x0D)],
                         [i for i, b in enumerate(chunk) if b in (0x0A, 0x0D)])
        for seg in out.replace(b"\r", b"\n").split(b"\n"):
            self.assertFalse(C.is_dangerous_segment(seg), seg)
        self.assertTrue(out.startswith(b"abc\n"))

    def test_fuzz_lines_are_terminated_bounded_and_safe(self):
        rng = random.Random(1234)
        lines = list(C.fuzz_lines(rng, 4000))
        self.assertEqual(len(lines), 4000)
        saw_nul = saw_cr = saw_lf = saw_long = False
        for line in lines:
            self.assertTrue(line.endswith((b"\n", b"\r")), line[-4:])
            body = line.rstrip(b"\r\n")
            self.assertLessEqual(len(body), 2000 + 1200)      # mutants may pad a token
            saw_nul |= b"\x00" in body
            saw_cr |= line.endswith(b"\r")
            saw_lf |= line.endswith(b"\n")
            saw_long |= len(body) > 1023
            for seg in body.replace(b"\r", b"\n").split(b"\n"):
                self.assertFalse(C.is_dangerous_segment(seg), seg)
        self.assertTrue(saw_nul and saw_cr and saw_lf and saw_long)

    def test_fuzz_is_reproducible_per_seed(self):
        a = list(C.fuzz_lines(random.Random(7), 50))
        b = list(C.fuzz_lines(random.Random(7), 50))
        self.assertEqual(a, b)
        self.assertNotEqual(a, list(C.fuzz_lines(random.Random(8), 50)))


class CheckTable(unittest.TestCase):
    def test_names_unique_and_subsets_known(self):
        names = [n for n, _, _ in C.CHECKS]
        self.assertEqual(len(names), len(set(names)))
        for n in C.BOOTSTRAP + C.FINAL + C.QUICK:
            self.assertIn(n, names)
        self.assertEqual(names[:3], list(C.BOOTSTRAP))
        self.assertEqual(names[-2:], list(C.FINAL))
        self.assertTrue(all(m["doc"] for _, _, m in C.CHECKS))

    def test_select_checks_quick_and_only(self):
        p = C.build_parser()
        quick = [n for n, _, _ in C.select_checks(p.parse_args(["--quick"]))]
        self.assertEqual(quick, [n for n in [x for x, _, _ in C.CHECKS] if n in C.QUICK])
        only = [n for n, _, _ in C.select_checks(p.parse_args(["--only", "tone,blit"]))]
        self.assertEqual(only, ["hello", "reply", "info", "tone", "blit", "settings_restore"])
        skipped = [n for n, _, _ in C.select_checks(p.parse_args(["--skip", "fuzz,reset"]))]
        self.assertNotIn("fuzz", skipped)
        self.assertNotIn("reset", skipped)
        self.assertIn("throughput", skipped)
        with self.assertRaises(SystemExit):
            C.select_checks(p.parse_args(["--only", "nosuch"]))

    def test_same_matches_strings_and_regexes(self):
        import re
        self.assertTrue(C.Ctx.same(["OK LED"], ("OK LED",)))
        self.assertTrue(C.Ctx.same(["EVT INFO FW 2.0.0"], (re.compile(r"^EVT INFO "),)))
        self.assertFalse(C.Ctx.same(["OK LED", "x"], ("OK LED",)))
        self.assertFalse(C.Ctx.same([], ("OK LED",)))


if __name__ == "__main__":
    unittest.main()
