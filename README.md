# Glowbug

![Glowbug](docs/glowbug.png)

[![Buy a Glowbug](docs/buy.svg)](https://glowbug.dev)

## What is Glowbug?

Glowbug is a good lookin' device that sits next to your computer. It lets
your AI coding agents get your attention with beeps, bloops, lights, colors,
and 5 OLED displays.

With a glance you'll know what each agent is working on, and you'll know
instantly when an agent is finished or has a question for you.

## How do I set it up?

![Step 1 of 3](docs/pill-1.svg)

**[<ins>Buy a Glowbug device</ins>](https://glowbug.dev)**

<br>

![Step 2 of 3](docs/pill-2.svg)

**Install the software.** Pick one:

Tell your coding agent (Claude Code, etc.):
```
Install glowbug from github.com/pud/glowbug
```
Homebrew:
```sh
brew install pud-blip/tap/glowbug && glowbug install
```
pipx:
```sh
pipx install glowbug && glowbug install
```
From source:
```sh
git clone https://github.com/pud/glowbug
cd glowbug && python3 glowbug.py install
```

<br>

![Step 3 of 3](docs/pill-3.svg)

**Plug in the Glowbug.** Restart any agent sessions you already had open.
`glowbug status` tells you if it's healthy.

## How does it work?

```
Claude Code ──┐
Cursor ───────┤ hooks ──▶ glowbug.py (daemon) ──USB──▶ Glowbug
Codex ────────┤
Antigravity ──┘
```

The AI tools you use already announce what they're doing via hooks. Glowbug
listens to these hooks and tells the Glowbug device what to do
(what lights to light up, which colors, etc).

## What it can see

- No network code. Search the repo for `http`; there is nothing.
- The hook forwards eight fields: event name, session id, session title,
  working directory, tool name, error type, which tool, idle flag. Never your
  prompts, never tool arguments, never file contents.
- The device only ever gets a session's name and one status word.
- The physical Glowbug device is not connected to the internet.

## Troubleshooting

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
