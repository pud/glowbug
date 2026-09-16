#!/usr/bin/env bash
# ci_light.sh — run a build/test command, then light a Glowbug screen with
# the result: green + "ding" on success, red (blinking) + "fail" with the
# exit code on failure. The light holds for 30 s and the board hands the
# screen back by itself (--for), so a dead script never leaves a stale
# result on the desk.
#
#   examples/ci_light.sh make test
#   examples/ci_light.sh -- npm run build        # a leading "--" is fine
#   GLOWBUG_SCREEN=5 examples/ci_light.sh pytest -q
#
# Exits with the build command's own status. Glowbug problems (daemon not
# running = exit 3, board unplugged, ...) go to stderr and never fail the
# build — the light is best-effort.
set -u
[ "${1-}" = "--" ] && shift
if [ $# -eq 0 ]; then
  echo "usage: $0 [--] <command> [args...]" >&2
  exit 2
fi
screen="${GLOWBUG_SCREEN:-3}"

"$@"
status=$?

# The daemon clips each text line to 21 characters (and says so in the
# reply); the command's name is enough for line 2.
what="$(basename -- "$1")"
if [ "$status" -eq 0 ]; then
  glowbug show "$screen" --color green --line1 "Build OK" --line2 "$what" \
    --sound ding --for 30 >/dev/null \
    || echo "glowbug: result not shown (exit $?)" >&2
else
  glowbug show "$screen" --color red --mode blink --line1 "Build FAILED" \
    --line2 "$what exit $status" --sound fail --for 30 >/dev/null \
    || echo "glowbug: result not shown (exit $?)" >&2
fi
exit "$status"
