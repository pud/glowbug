# Glowbug

![Glowbug](docs/glowbug.png)

[![Buy a Glowbug](docs/buy.svg)](https://glowbug.dev)

## What is Glowbug?

Glowbug is a device that enables AI coding agents (Claude Code, Codex,
Cursor, etc.) to get your attention by beeping, flashing, changing color, and
writing messages on its 5 OLED screens.

Your computer talks to Glowbug using this open-source software.

## Installing Glowbug

First you need a Glowbug:

[![Buy a Glowbug](docs/buy.svg)](https://glowbug.dev)

Then the software. Any of these:

```text
Install glowbug from github.com/pud/glowbug        (tell Claude Code)
```

```sh
brew install pud-blip/tap/glowbug && glowbug install
```

```sh
pipx install glowbug && glowbug install
```

```sh
git clone https://github.com/pud/glowbug && cd glowbug && python3 glowbug.py install
```

Plug in the Glowbug and restart any agent sessions you already had open.
`glowbug status` tells you if it's healthy.

## How does it work?

```
Claude Code ──┐
Cursor ───────┤ hooks ──▶ glowbug.py (daemon) ──USB──▶ Glowbug
Codex ────────┤
Antigravity ──┘
```

Your tools already announce what they're doing. The daemon listens and sends
the device one line of text per change. The firmware draws it.

## What it can see

- No network code. Search the repo for `http`; there is nothing.
- The hook forwards eight fields: event name, session id, session title,
  working directory, tool name, error type, which tool, idle flag. Never your
  prompts, never tool arguments, never file contents.
- The device only ever gets a session's name and one status word.

## If something goes wrong

`glowbug rescue` puts the known-good firmware back over USB, even on a board
that has stopped talking (hold the knob while plugging in). Everything else:
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Requirements

- macOS, Python 3.9+ (the built-in one is fine)
- One or more of: Claude Code, Cursor 1.7+, Codex (with `features.hooks`
  on), Antigravity 2.0+
- A Glowbug

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```

## License

MIT -- see [LICENSE](LICENSE).
