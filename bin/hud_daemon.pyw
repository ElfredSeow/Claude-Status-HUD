"""
Claude Code Status HUD - daemon

A single long-running process that:
  1. Renders an always-on-top, frameless, semi-transparent, fully draggable
     overlay "status light" that works across all monitors.
  2. Aggregates the live state of every Claude Code session (written by the
     hook scripts) and decides one overall status.
  3. Mirrors that status onto Logitech G hardware (G213 keyboard + mouse)
     via the LED SDK.

Status model (priority high -> low):
    permission  -> SOLID RED      ("Claude needs your approval")
    busy        -> PULSING AMBER  ("thinking / running tools")
    idle        -> SOLID GREEN    ("waiting for your next input")
    none        -> DIM GREY       (no live sessions; hardware released to G HUB)

Pure standard library (tkinter + ctypes). No pip installs required.
Run with pythonw.exe so there is no console window.
"""

import os
import sys
import json
import time
import math
import socket
import tkinter as tk
import tkinter.font as tkfont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logi_led import LogiLED  # noqa: E402

# ----------------------------------------------------------------------------
# Paths / constants
# ----------------------------------------------------------------------------
HUD_DIR = os.path.join(os.path.expanduser("~"), ".claude", "hud")
SESSIONS_DIR = os.path.join(HUD_DIR, "sessions")
CONFIG_PATH = os.path.join(HUD_DIR, "config.json")
LOG_PATH = os.path.join(HUD_DIR, "hud.log")

# A "busy" session that hasn't sent an event in this long is treated as idle
# (covers terminals closed without a clean SessionEnd).
BUSY_STALE_SECONDS = 150
# A "permission" session goes stale more slowly (you may take a while to answer).
PERMISSION_STALE_SECONDS = 3600
# Session files older than this are deleted outright.
PRUNE_SECONDS = 6 * 3600

PRIORITY = {"permission": 3, "busy": 2, "idle": 1, "none": 0}

DEFAULT_CONFIG = {
    "position": {"x": 80, "y": 80},
    "alpha": 0.93,
    "outputs": {"overlay": True, "keyboard_mouse": True},
    "port": 51789,
    "colors": {
        "permission": [235, 45, 45],
        "busy":       [255, 150, 0],
        "idle":       [45, 205, 95],
        "none":       [95, 100, 110],
    },
    # Hardware colours can differ slightly so they read well on RGB LEDs.
    "hw_colors": {
        "permission": [255, 0, 0],
        "busy":       [255, 140, 0],
        "idle":       [0, 200, 60],
    },
}

CHROMA = "#01020a"  # transparent-key colour for rounded corners
PANEL_BG = "#171a21"
PANEL_OUTLINE = "#2b303b"


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(HUD_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        log(f"save_config failed: {e}")


# ----------------------------------------------------------------------------
# Session-state aggregation
# ----------------------------------------------------------------------------
def aggregate_state():
    """Scan per-session files and return (state, summary, session_count)."""
    now = time.time()
    best = "none"
    best_pri = 0
    best_info = None
    count = 0
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return "none", "No sessions", 0

    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            mtime = os.path.getmtime(path)
            if now - mtime > PRUNE_SECONDS:
                os.remove(path)
                continue
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue

        state = data.get("state", "idle")
        ts = data.get("ts", mtime)
        age = now - ts

        # Downgrade stale active states so an orphaned terminal can't get stuck.
        if state == "busy" and age > BUSY_STALE_SECONDS:
            state = "idle"
        elif state == "permission" and age > PERMISSION_STALE_SECONDS:
            state = "idle"

        count += 1
        pri = PRIORITY.get(state, 1)
        if pri > best_pri:
            best_pri = pri
            best = state
            best_info = data

    if count == 0:
        return "none", "No sessions", 0

    summary = _summary(best, best_info, count)
    return best, summary, count


def _summary(state, info, count):
    info = info or {}
    label = {
        "permission": "Needs your approval",
        "busy": "Working…",
        "idle": "Waiting for you",
        "none": "No sessions",
    }.get(state, state)

    detail = ""
    if state == "busy" and info.get("tool"):
        detail = info["tool"]
    elif state == "permission" and info.get("tool"):
        detail = f"approve {info['tool']}?"
    if not detail:
        detail = info.get("title") or info.get("cwd", "")
        detail = os.path.basename(detail.rstrip("\\/")) if detail else ""

    sess = f"{count} session" + ("s" if count != 1 else "")
    line2 = f"{sess}" + (f"  ·  {detail}" if detail else "")
    return label, line2


# ----------------------------------------------------------------------------
# Monitor enumeration (multi-monitor support, via Win32)
# ----------------------------------------------------------------------------
def get_monitors():
    """Return list of (left, top, right, bottom) for every display."""
    import ctypes
    from ctypes import wintypes

    monitors = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.POINTER(wintypes.RECT), ctypes.c_double,
    )

    def _cb(hmon, hdc, lprc, lparam):
        r = lprc.contents
        monitors.append((r.left, r.top, r.right, r.bottom))
        return 1

    try:
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(_cb), 0)
    except Exception as e:  # noqa: BLE001
        log(f"EnumDisplayMonitors failed: {e}")
    if not monitors:
        u = ctypes.windll.user32
        monitors.append((0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)))
    return monitors


def set_dpi_aware():
    import ctypes
    try:
        # Per-monitor-v2 so coordinates are real pixels on mixed-DPI setups.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Hardware controller (auto-reinitialises so 'none' can release G HUB)
# ----------------------------------------------------------------------------
class Hardware:
    def __init__(self, hw_colors, enabled):
        self.hw_colors = hw_colors
        self.enabled = enabled
        self.led = None
        self._applied = None
        self.available = False
        if enabled:
            self._init()

    def _init(self):
        self.led = LogiLED()
        self.available = self.led.available
        if not self.available:
            log(f"LED unavailable: {self.led.last_error}")

    def set_enabled(self, enabled):
        self.enabled = enabled
        if not enabled and self.led and self.led.available:
            self.led.release()
            self._applied = None
        elif enabled and (self.led is None or not self.led.available):
            self._init()
            self._applied = None

    def apply(self, state):
        if not self.enabled:
            return
        if state == self._applied:
            return
        # Re-init if a previous 'none' released control back to G HUB.
        if (self.led is None or not self.led.available) and state != "none":
            self._init()
        self._applied = state

        if state == "none":
            if self.led and self.led.available:
                self.led.release()
            return
        if not (self.led and self.led.available):
            return
        if state == "busy":
            self.led.pulse(self.hw_colors["busy"], interval_ms=1400)
        elif state == "permission":
            self.led.set_color(self.hw_colors["permission"])
        elif state == "idle":
            self.led.set_color(self.hw_colors["idle"])

    def shutdown(self):
        if self.led and self.led.available:
            self.led.release()


# ----------------------------------------------------------------------------
# Overlay window
# ----------------------------------------------------------------------------
class Overlay:
    W, H = 232, 70

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = "none"
        self.summary = ("No sessions", "")
        self.phase = 0.0
        self._drag = None

        self.hw = Hardware(
            {k: tuple(v) for k, v in cfg["hw_colors"].items()},
            cfg["outputs"]["keyboard_mouse"],
        )

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass
        self.root.attributes("-alpha", float(cfg.get("alpha", 0.93)))
        self.root.configure(bg=CHROMA)

        x = cfg["position"]["x"]
        y = cfg["position"]["y"]
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=self.W, height=self.H,
            bg=CHROMA, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.title_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.detail_font = tkfont.Font(family="Segoe UI", size=8)

        self._build()
        self._bind()
        self._build_menu()

        self.root.deiconify()
        self.root.after(0, self._tick)
        self.root.after(150, self._poll)
        self.root.after(2000, self._keep_top)

    # ---- drawing ----
    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _build(self):
        c = self.canvas
        self._round_rect(1, 1, self.W - 1, self.H - 1, 16,
                         fill=PANEL_BG, outline=PANEL_OUTLINE, width=1)
        cx, cy = 36, self.H // 2
        self.glow = c.create_oval(cx - 22, cy - 22, cx + 22, cy + 22,
                                  fill=PANEL_BG, outline="")
        self.led_dot = c.create_oval(cx - 13, cy - 13, cx + 13, cy + 13,
                                     fill="#555", outline="")
        self.title_item = c.create_text(
            66, cy - 10, anchor="w", text="Starting…",
            fill="#f4f5f7", font=self.title_font)
        self.detail_item = c.create_text(
            66, cy + 11, anchor="w", text="", fill="#9aa3b2",
            font=self.detail_font)

    def _set_dot(self, rgb, glow_alpha):
        hexc = "#%02x%02x%02x" % rgb
        self.canvas.itemconfig(self.led_dot, fill=hexc)
        gr = tuple(int(PANEL_BG_RGB[i] + (rgb[i] - PANEL_BG_RGB[i]) * glow_alpha)
                   for i in range(3))
        self.canvas.itemconfig(self.glow, fill="#%02x%02x%02x" % gr)

    # ---- animation + state ----
    def _tick(self):
        self.phase += 0.06
        base = tuple(self.cfg["colors"].get(self.state, [120, 120, 120]))
        if self.state == "busy":
            # breathing amber
            f = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(self.phase * 1.7))
            rgb = tuple(int(b * f) for b in base)
            glow = 0.25 + 0.35 * (0.5 + 0.5 * math.sin(self.phase * 1.7))
        elif self.state == "permission":
            # solid red with a slow attention glow
            f = 0.85 + 0.15 * (0.5 + 0.5 * math.sin(self.phase * 2.4))
            rgb = tuple(int(b * f) for b in base)
            glow = 0.35 + 0.25 * (0.5 + 0.5 * math.sin(self.phase * 2.4))
        elif self.state == "idle":
            rgb = base
            glow = 0.30
        else:  # none
            rgb = base
            glow = 0.10
        self._set_dot(rgb, glow)
        self.root.after(33, self._tick)

    def _poll(self):
        state, summary, _ = aggregate_state()
        if state != self.state or summary != self.summary:
            self.state = state
            self.summary = summary
            self.canvas.itemconfig(self.title_item, text=summary[0])
            self.canvas.itemconfig(self.detail_item, text=summary[1])
            self.hw.apply(state)
        self.root.after(150, self._poll)

    def _keep_top(self):
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except tk.TclError:
            pass
        self.root.after(2000, self._keep_top)

    # ---- interaction ----
    def _bind(self):
        for seq in ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.canvas.bind(seq, self._on_mouse)
        self.canvas.bind("<Button-3>", self._show_menu)

    def _on_mouse(self, e):
        if e.type == tk.EventType.ButtonPress:
            self._drag = (e.x_root, e.y_root,
                          self.root.winfo_x(), self.root.winfo_y())
        elif e.type == tk.EventType.Motion and self._drag:
            dx = e.x_root - self._drag[0]
            dy = e.y_root - self._drag[1]
            nx = self._drag[2] + dx
            ny = self._drag[3] + dy
            self.root.geometry(f"+{nx}+{ny}")
        elif e.type == tk.EventType.ButtonRelease and self._drag:
            self.cfg["position"] = {"x": self.root.winfo_x(),
                                    "y": self.root.winfo_y()}
            save_config(self.cfg)
            self._drag = None

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Snap to next monitor",
                              command=self._next_monitor)
        self.menu.add_command(label="Reset position",
                              command=self._reset_position)
        self.menu.add_separator()
        self._kbvar = tk.BooleanVar(value=self.cfg["outputs"]["keyboard_mouse"])
        self.menu.add_checkbutton(label="Light keyboard + mouse",
                                  variable=self._kbvar,
                                  command=self._toggle_hw)
        self.menu.add_separator()
        self.menu.add_command(label="Quit HUD", command=self._quit)

    def _show_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _next_monitor(self):
        mons = get_monitors()
        if len(mons) < 2:
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        cur = 0
        for i, (l, t, r, b) in enumerate(mons):
            if l <= x < r and t <= y <= b:
                cur = i
                break
        l, t, r, b = mons[(cur + 1) % len(mons)]
        nx, ny = l + 60, t + 60
        self.root.geometry(f"+{nx}+{ny}")
        self.cfg["position"] = {"x": nx, "y": ny}
        save_config(self.cfg)

    def _reset_position(self):
        self.root.geometry("+80+80")
        self.cfg["position"] = {"x": 80, "y": 80}
        save_config(self.cfg)

    def _toggle_hw(self):
        on = bool(self._kbvar.get())
        self.cfg["outputs"]["keyboard_mouse"] = on
        save_config(self.cfg)
        self.hw.set_enabled(on)
        if on:
            self.hw.apply(self.state)

    def _quit(self):
        self.hw.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


PANEL_BG_RGB = (23, 26, 33)


# ----------------------------------------------------------------------------
def single_instance(port):
    """Bind a localhost port so only one daemon runs. Returns the socket."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", int(port)))
        s.listen(1)
        return s
    except OSError:
        return None


def main():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    set_dpi_aware()
    cfg = load_config()
    if not os.path.exists(CONFIG_PATH):
        save_config(cfg)

    guard = single_instance(cfg.get("port", 51789))
    if guard is None:
        log("another HUD instance is already running; exiting")
        return

    log("HUD daemon starting")
    Overlay(cfg).run()


if __name__ == "__main__":
    main()
