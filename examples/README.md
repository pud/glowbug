# Glowbug examples

Small, runnable scripts that use the host API (README "API",
[PROTOCOL.md](../PROTOCOL.md)). Each one uses only flags and functions that
exist in `glowbug.py` 2.0.0, and every paint carries `--for` / `seconds=`,
so a dead script never leaves a stale light on the desk.

| Script | What it does |
|---|---|
| [`ci_light.sh`](ci_light.sh) | `ci_light.sh make test` — runs the command, then screen 3 goes green + `ding` or red + `fail` (with the exit code) for 30 s. Exits with the command's status. |
| [`notify_when_done.sh`](notify_when_done.sh) | `notify_when_done.sh -- cargo build --release` — when the command finishes, plays `ding` and shows the elapsed time and status on screen 3 for 60 s, red on a non-zero exit. |
| [`pomodoro.py`](pomodoro.py) | `python3 pomodoro.py [minutes]` — a 25-minute countdown on screen 5, amber breathing underglow, `fanfare` at the end. Stdlib + `import glowbug`. |

Both shell scripts take `GLOWBUG_SCREEN=<1-5>` to use a different screen,
and never change the wrapped command's exit status — a missing daemon
(`glowbug` exit 3) or an unplugged board is reported on stderr and that's
all.

One-liners worth stealing:

```sh
glowbug show 3 --color green --line1 "Build OK" --sound ding --for 5
glowbug led ug violet --mode pulse --for 600         # "something long is running"
glowbug text 5 "Deploying" "step 3 of 7" --for 20
glowbug events --filter enc,click,hold                # the knob, as JSON lines
glowbug palette --json                                # every color and sound name
```

From Python (`pip install glowbug` in your venv, or the `sys.path` line in
`pomodoro.py`):

```python
import glowbug
glowbug.show(3, color="green", line1="Build OK", sound="ding", seconds=5)
```
