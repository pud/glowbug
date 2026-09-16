#!/usr/bin/env bash
# notify_when_done.sh — run a long command; when it finishes, play "ding"
# and show the elapsed time and status on screen 3 for 60 s: green on
# success, red on a non-zero exit. Then the board hands the screen back
# by itself (--for).
#
#   examples/notify_when_done.sh -- cargo build --release
#   examples/notify_when_done.sh -- rsync -a big/ backup/
#   GLOWBUG_SCREEN=1 examples/notify_when_done.sh -- sleep 90
#
# Exits with the command's own status. Glowbug problems (daemon not
# running = exit 3, board unplugged, ...) go to stderr and never change it.
set -u
[ "${1-}" = "--" ] && shift
if [ $# -eq 0 ]; then
  echo "usage: $0 -- <command> [args...]" >&2
  exit 2
fi
screen="${GLOWBUG_SCREEN:-3}"

start=$(date +%s)
"$@"
status=$?
secs=$(( $(date +%s) - start ))

if [ "$secs" -ge 3600 ]; then
  elapsed=$(printf '%dh%02dm' $((secs / 3600)) $((secs % 3600 / 60)))
elif [ "$secs" -ge 60 ]; then
  elapsed=$(printf '%dm%02ds' $((secs / 60)) $((secs % 60)))
else
  elapsed="${secs}s"
fi

# 21 characters per line; the daemon clips anything longer.
what="$(basename -- "$1")"
if [ "$status" -eq 0 ]; then
  color=green
  line1="Done in $elapsed"
else
  color=red
  line1="Failed ($status) $elapsed"
fi
glowbug show "$screen" --color "$color" --line1 "$line1" --line2 "$what" \
  --sound ding --for 60 >/dev/null \
  || echo "glowbug: result not shown (exit $?)" >&2
exit "$status"
