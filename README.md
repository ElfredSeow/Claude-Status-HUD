# Claude Code Status HUD

A local, always-on-top **status light** for your Claude Code sessions — shown both
as a draggable on-screen overlay and on your **Logitech G213 keyboard + Logitech
mouse** (via the Logitech LED SDK / G HUB).

## Status colors

| State | Meaning | Overlay | Keyboard + mouse |
|-------|---------|---------|------------------|
| **Working** | Claude is thinking / running tools | pulsing amber | pulsing amber |
| **Needs approval** | Paused on a permission prompt | solid red | solid red |
| **Idle** | Finished; waiting for your next input | solid green | solid green |
| **No sessions** | No live Claude Code sessions | dim grey | released to your normal G HUB lighting |

Multiple sessions are aggregated by priority: **red > amber > green**. If *any*
session needs approval the light is red; if any is working it's amber; otherwise green.

## The overlay

- **Always on top**, frameless, semi-transparent, rounded.
- **Fully draggable** — left-click anywhere on it and drag, across **any monitor**.
  Position is remembered between runs.
- **Right-click menu**: snap to next monitor, reset position, toggle the
  keyboard/mouse lighting on/off, quit.

## How it works

```
Claude Code hooks ──► hooks/hud_hook.py ──► ~/.claude/hud/sessions/<id>.json
                                                      │
                                          bin/hud_daemon.pyw (always running)
                                          ├─ draws the overlay + animations
                                          └─ drives keyboard+mouse via the LED SDK
```

- `bin/hud_daemon.pyw` — the long-running overlay + hardware driver (pure stdlib:
  tkinter + ctypes; single-instance guarded on port 51789).
- `bin/logi_led.py` — ctypes wrapper around `LogitechLedEnginesWrapper.dll`
  (targets `LOGI_DEVICETYPE_ALL`, so keyboard and mouse stay in sync). Degrades to
  a no-op if G HUB / the DLL is unavailable, so the overlay always works.
- `hooks/hud_hook.py` — registered for 8 Claude Code hook events; writes a tiny
  per-session state file. Silent and non-blocking (never writes stdout, always exits 0).
- `bin/install.py` — merges the hooks into `~/.claude/settings.json` (with backup).

## Run it

The daemon is already running. To start it manually (e.g. after a reboot):

```
start-hud.cmd      # start (no console window)
stop-hud.cmd       # stop
```

### Auto-start on login (optional)

Not enabled by default. To turn it on, create a shortcut to
`C:\Python313\pythonw.exe "C:\Users\manfr\claude-status-hud\bin\hud_daemon.pyw"`
in your Startup folder (`shell:startup`), or run `start-hud.cmd` from Task Scheduler.

## Requirements (already satisfied on this machine)

- Python 3.13 (tkinter included).
- Logitech **G HUB** running, with *Allow Games & Applications to control
  illumination* enabled (default). Needed only for the keyboard/mouse light.

## Configuration

`~/.claude/hud/config.json` (created on first run) — position, alpha, colors,
hardware colors, port, and which outputs are enabled. Edit and restart the daemon.

## Uninstall

```
python bin\install.py --uninstall   # remove hooks from settings.json (keeps a backup)
stop-hud.cmd                         # stop the daemon
```
