#!/usr/bin/env python3
"""pomodoro.py — a 25-minute focus timer on your Glowbug.

Screen 5 counts down, the underglow breathes amber, and a fanfare plays at
the end. Every paint is sent with seconds= (the board's own FOR timer), so
if this script dies the screen clears within 5 s and the underglow within
40 s — nothing is left for you to release by hand.

    python3 examples/pomodoro.py          # 25 minutes
    python3 examples/pomodoro.py 5        # a 5-minute break
"""
import os
import sys
import time

try:
    import glowbug                                   # pip/uv install glowbug
except ImportError:                                  # or the installed copy
    sys.path.insert(0, os.path.expanduser("~/.glowbug"))
    import glowbug

SCREEN = 5
MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 25


def main():
    end = time.time() + MINUTES * 60
    led_at = 0
    try:
        while True:
            left = max(0, int(round(end - time.time())))
            # Re-sending the pulse every tick would restart its breath, so the
            # underglow is (re)claimed every 30 s with a 40 s lease instead.
            if time.time() - led_at >= 30:
                glowbug.led("ug", "amber", mode="pulse", period=2000, seconds=40)
                led_at = time.time()
            glowbug.text(SCREEN, "%d:%02d" % divmod(left, 60), "focus", seconds=5)
            if left == 0:
                break
            time.sleep(1)
        glowbug.led("ug", "green", mode="pulse", seconds=10)
        glowbug.text(SCREEN, "Done!", "take a break", seconds=10)
        glowbug.sound("fanfare")
    except glowbug.GlowbugError as e:                # no daemon, no board, ...
        sys.exit("glowbug: %s (%s)" % (e.message, e.code))
    except KeyboardInterrupt:
        glowbug.release("led:ug", "glass:%d" % SCREEN)


if __name__ == "__main__":
    main()
