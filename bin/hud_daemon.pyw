"""
Claude Code Status HUD - daemon

A single long-running process that:
  1. Renders an always-on-top, frameless, semi-transparent, fully draggable
     overlay "status light" that works across all monitors.
  2. Aggregates the live state of every Claude Code session (written by the
     hook scripts) and decides one overall status.
  3. Mirrors that status onto Logitech G hardware (G213 keyboard + mouse)
     via the LED SDK.
  4. Provides a system-tray icon for quick access (requires pystray + Pillow).
  5. Listens for remote session updates from other devices over WiFi/TCP
     (default port 51790) and merges them into the same aggregated view.

Status model (priority high -> low):
    permission  -> SOLID RED      ("Claude needs your approval")
    busy        -> PULSING AMBER  ("thinking / running tools")
    idle        -> SOLID GREEN    ("waiting for your next input")
    none        -> DIM GREY       (no live sessions; hardware released to G HUB)

CLI usage (writes a session file and exits — daemon picks it up within 150ms):
    pythonw hud_daemon.pyw working           # yellow  (busy)
    pythonw hud_daemon.pyw idle              # green   (idle)
    pythonw hud_daemon.pyw approval_needed   # red     (permission)
    pythonw hud_daemon.pyw clear             # remove the manual session

Pure standard library (tkinter + ctypes + winreg). No pip installs required
for core functionality. pystray + Pillow enable the system-tray icon.
Run with pythonw.exe so there is no console window.
"""

import os
import sys
import json
import time
import math
import socket
import winreg
import threading
import tkinter as tk
import tkinter.font as tkfont
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logi_led import LogiLED  # noqa: E402

# ----------------------------------------------------------------------------
# Paths / constants
# ----------------------------------------------------------------------------
HUD_DIR      = os.path.join(os.path.expanduser("~"), ".claude", "hud")
SESSIONS_DIR = os.path.join(HUD_DIR, "sessions")
CONFIG_PATH  = os.path.join(HUD_DIR, "config.json")
USAGE_PATH   = os.path.join(HUD_DIR, "usage.json")
LOG_PATH     = os.path.join(HUD_DIR, "hud.log")

BUSY_STALE_SECONDS       = 150
PERMISSION_STALE_SECONDS = 3600
PRUNE_SECONDS            = 6 * 3600

PRIORITY = {"permission": 3, "busy": 2, "idle": 1, "none": 0}

DEFAULT_CONFIG = {
    "position":       {"x": 80, "y": 80},
    "alpha":          0.93,
    "outputs":        {"overlay": True, "keyboard_mouse": True},
    "port":           51789,
    "colors": {
        "permission": [235, 45, 45],
        "busy":       [255, 150, 0],
        "idle":       [45, 205, 95],
        "none":       [95, 100, 110],
    },
    "hw_colors": {
        "permission": [255, 0, 0],
        "busy":       [255, 140, 0],
        "idle":       [0, 200, 60],
    },
    # Multi-device: remote session receiver
    "remote": {
        "enabled": True,
        "port":    51790,
    },
    # Which device_id is considered "primary" — affects overlay detail text.
    # "local" means this PC.  Set to a remote device_id (e.g. "mac") to
    # highlight that device instead.
    "primary_device": "local",
    # Friendly names for remote devices keyed by device_id.
    "devices": {},
    # Office popup shown when hovering / clicking the overlay.
    # trigger: "hover" | "click" | "none"
    "office_popup": {
        "trigger": "hover",
    },
}

CHROMA       = "#01020a"
PANEL_BG     = "#171a21"
PANEL_OUTLINE= "#2b303b"
PANEL_BG_RGB = (23, 26, 33)

# Connected-card design colours (match hud-overlay.html CSS variables)
DIVIDER_COL  = "#353c4a"   # --border2: line between HUD and usage sections
TEXT_FAINT_C = "#5a6275"   # --text-faint
TEXT_DIM_C   = "#9aa3b2"   # --text-dim
TRACK_BG_C   = "#232838"   # rgba(255,255,255,.07) blended onto --surface
USG_GREEN    = "#2dcd5f"   # --green
USG_AMBER    = "#ff9600"   # --amber
USG_RED      = "#eb2d2d"   # --red

_STARTUP_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "ClaudeStatusHUD"

STATE_CLI_MAP = {
    "working":         "busy",
    "idle":            "idle",
    "approval_needed": "permission",
    "clear":           None,
}


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _safe_sid(s):
    keep = [c if (c.isalnum() or c in "-_") else "_" for c in str(s)]
    return ("".join(keep) or "default")[:80]


# ----------------------------------------------------------------------------
# CLI state mode
# ----------------------------------------------------------------------------
def handle_cli_state(state_arg):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = os.path.join(SESSIONS_DIR, "cli_manual.json")
    if state_arg == "clear":
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    internal = STATE_CLI_MAP[state_arg]
    record = {
        "session_id": "cli_manual",
        "state":      internal,
        "ts":         time.time(),
        "event":      "cli",
        "tool":       "",
        "cwd":        "",
        "message":    f"CLI: {state_arg}",
        "device":     "local",
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
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


def _load_usage() -> dict | None:
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------------
# Windows startup registry
# ----------------------------------------------------------------------------
def get_startup_enabled():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, _STARTUP_REG_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def set_startup_enabled(enabled: bool):
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pw):
                pw = sys.executable
            winreg.SetValueEx(
                key, _STARTUP_REG_NAME, 0, winreg.REG_SZ,
                f'"{pw}" "{os.path.abspath(__file__)}"')
            log(f"startup enabled")
        else:
            try:
                winreg.DeleteValue(key, _STARTUP_REG_NAME)
                log("startup disabled")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except OSError as e:
        log(f"set_startup_enabled failed: {e}")


# ----------------------------------------------------------------------------
# Remote session receiver (HTTP on port 51790)
# ----------------------------------------------------------------------------
# Shared config reference so the handler knows where to write.
_remote_cfg: dict = {}


class _RemoteHandler(BaseHTTPRequestHandler):
    """Accepts POST /  with JSON body from remote hook scripts."""

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            data   = json.loads(body)

            device_id   = _safe_sid(data.get("device_id",   "remote"))
            device_name = str(data.get("device_name", device_id))
            sid         = _safe_sid(data.get("session_id", "default"))
            filename    = f"remote_{device_id}_{sid}.json"
            path        = os.path.join(SESSIONS_DIR, filename)

            # Register / update the friendly device name in config.
            _remote_cfg.setdefault("devices", {})[device_id] = device_name

            if data.get("state") == "remove":
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            else:
                record = {
                    "session_id":  sid,
                    "state":       data.get("state", "idle"),
                    "ts":          time.time(),
                    "event":       data.get("event", ""),
                    "tool":        data.get("tool",  ""),
                    "cwd":         data.get("cwd",   ""),
                    "message":     data.get("message", ""),
                    "device":      device_id,
                    "device_name": device_name,
                }
                os.makedirs(SESSIONS_DIR, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(record, fh)
                os.replace(tmp, path)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception as exc:
            log(f"remote receiver error: {exc}")
            self.send_response(400)
            self.end_headers()

    def log_message(self, *_):  # suppress access log noise
        pass


def start_remote_receiver(cfg: dict) -> HTTPServer | None:
    port = cfg.get("remote", {}).get("port", 51790)
    if not cfg.get("remote", {}).get("enabled", True):
        return None
    try:
        srv = HTTPServer(("0.0.0.0", port), _RemoteHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"remote receiver listening on 0.0.0.0:{port}")
        return srv
    except OSError as e:
        log(f"remote receiver failed to start on :{port} — {e}")
        return None


# ----------------------------------------------------------------------------
# Session-state aggregation
# ----------------------------------------------------------------------------
def aggregate_state(primary_device: str = "local"):
    """
    Scan per-session files.  Returns (state, summary_tuple, session_count,
    device_summary) where device_summary is {device_id: (state, count)}.
    """
    now = time.time()
    best      = "none"
    best_pri  = 0
    best_info = None
    count     = 0
    device_counts: dict[str, int]  = {}
    device_states: dict[str, str]  = {}

    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return "none", ("No sessions", ""), 0, {}

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
        ts    = data.get("ts", mtime)
        age   = now - ts
        dev   = data.get("device", "local")

        if state == "busy"       and age > BUSY_STALE_SECONDS:
            state = "idle"
        elif state == "permission" and age > PERMISSION_STALE_SECONDS:
            state = "idle"

        count += 1
        device_counts[dev] = device_counts.get(dev, 0) + 1
        # Track highest-priority state per device
        cur_pri = PRIORITY.get(device_states.get(dev, "none"), 0)
        if PRIORITY.get(state, 0) > cur_pri:
            device_states[dev] = state

        pri = PRIORITY.get(state, 1)
        if pri > best_pri:
            best_pri  = pri
            best      = state
            best_info = data

    if count == 0:
        return "none", ("No sessions", ""), 0, {}

    summary = _summary(best, best_info, count, device_counts, primary_device)
    dev_summary = {d: (device_states[d], device_counts[d]) for d in device_states}
    return best, summary, count, dev_summary


def read_all_sessions() -> list[dict]:
    """Return every live session dict (staleness-adjusted), sorted by state priority desc."""
    now     = time.time()
    results = []
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return results
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            mtime = os.path.getmtime(path)
            if now - mtime > PRUNE_SECONDS:
                continue
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            state = data.get("state", "idle")
            ts    = data.get("ts", mtime)
            age   = now - ts
            if state == "busy"       and age > BUSY_STALE_SECONDS:
                data["state"] = "idle"
            elif state == "permission" and age > PERMISSION_STALE_SECONDS:
                data["state"] = "idle"
            results.append(data)
        except (OSError, ValueError):
            continue
    results.sort(key=lambda d: PRIORITY.get(d.get("state", "none"), 0), reverse=True)
    return results


def _summary(state, info, count, device_counts: dict, primary_device: str):
    info = info or {}
    label = {
        "permission": "Needs your approval",
        "busy":       "Working…",
        "idle":       "Waiting for you",
        "none":       "No sessions",
    }.get(state, state)

    detail = ""
    if state == "busy" and info.get("tool"):
        detail = info["tool"]
    elif state == "permission" and info.get("tool"):
        detail = f"approve {info['tool']}?"
    if not detail:
        detail = info.get("title") or info.get("cwd", "")
        detail = os.path.basename(detail.rstrip("\\/")) if detail else ""

    # Show device prefix if the top state comes from a non-primary device.
    top_device = info.get("device", "local")
    if top_device and top_device != primary_device:
        dev_name = info.get("device_name") or top_device.upper()
        label = f"{dev_name}: {label}"

    n_devices = len(device_counts)
    sess = f"{count} session" + ("s" if count != 1 else "")
    dev_note = f"  ·  {n_devices} devices" if n_devices > 1 else ""
    line2 = sess + dev_note + (f"  ·  {detail}" if detail else "")
    return label, line2


# ----------------------------------------------------------------------------
# Monitor enumeration
# ----------------------------------------------------------------------------
def get_monitors():
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
    except Exception as e:
        log(f"EnumDisplayMonitors failed: {e}")
    if not monitors:
        u = ctypes.windll.user32
        monitors.append((0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)))
    return monitors


def set_dpi_aware():
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Hardware controller
# ----------------------------------------------------------------------------
class Hardware:
    def __init__(self, hw_colors, enabled):
        self.hw_colors = hw_colors
        self.enabled   = enabled
        self.led       = None
        self._applied  = None
        self.available = False
        if enabled:
            self._init()

    def _init(self):
        self.led       = LogiLED()
        self.available = self.led.available
        if not self.available:
            log(f"LED unavailable: {self.led.last_error}")

    def set_enabled(self, enabled):
        self.enabled = enabled
        if not enabled and self.led and self.led.available:
            self.led.release(); self._applied = None
        elif enabled and (self.led is None or not self.led.available):
            self._init(); self._applied = None

    def apply(self, state):
        if not self.enabled:
            return
        if state == self._applied:
            return
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
# Office popup window
# ----------------------------------------------------------------------------
class OfficePopup:
    """
    Floating panel shown above the HUD overlay.
    Displays one "cubicle" card per live Claude session.
    """

    CARD_W   = 200
    CARD_H   = 108
    COLS     = 2
    PAD      = 10
    HDR_H    = 48
    FTR_H    = 28

    C_BG      = "#0e1118"
    C_SURF    = "#13161f"
    C_BORDER  = "#2b303b"
    C_TEXT    = "#f4f5f7"
    C_DIM     = "#9aa3b2"
    C_FAINT   = "#5a6275"

    STATE_COL   = {"idle": "#2dcd5f", "busy": "#ff9600", "permission": "#eb2d2d", "none": "#5f646e"}
    STATE_LABEL = {"idle": "Idle", "busy": "Busy", "permission": "Approval!", "none": "Vacant"}

    def __init__(self, overlay: "Overlay"):
        self.ov        = overlay
        self._win      = None
        self._canvas   = None
        self._pinned   = False
        self.visible   = False
        self._hide_id  = None
        self._sessions: list[dict] = []

    # ── geometry ────────────────────────────────────────────
    def _geometry(self):
        ov_x = self.ov.root.winfo_x()
        ov_y = self.ov.root.winfo_y()
        n    = max(len(self._sessions), 1)
        rows = math.ceil(n / self.COLS)
        W    = self.COLS * self.CARD_W + (self.COLS + 1) * self.PAD
        H    = self.HDR_H + rows * (self.CARD_H + self.PAD) + self.PAD + self.FTR_H
        x    = ov_x - 8
        y    = ov_y - H - 12
        # clamp to owning monitor
        for l, t, r, b in get_monitors():
            if l <= ov_x < r and t <= ov_y < b:
                x = max(l + 4, min(x, r - W - 4))
                y = max(t + 4, y)
                break
        return x, y, W, H

    # ── build / draw ─────────────────────────────────────────
    def _build(self):
        x, y, W, H = self._geometry()
        self._win = tk.Toplevel(self.ov.root)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.97)
        self._win.configure(bg=self.C_BG)
        self._win.geometry(f"{W}x{H}+{x}+{y}")

        self._canvas = tk.Canvas(
            self._win, width=W, height=H,
            bg=self.C_BG, highlightthickness=0, bd=0,
        )
        self._canvas.pack(fill="both", expand=True)
        self._win.bind("<Enter>", self._on_enter)
        self._win.bind("<Leave>", self._on_leave)
        self._canvas.bind("<Button-1>", lambda e: self._toggle_pin())

    def _rrect(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
            x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1,
        ]
        return self._canvas.create_polygon(pts, smooth=True, **kw)

    @staticmethod
    def _draw_robot(c, hcx, hcy, state, scol):
        """
        Draw a mini Claude robot centred at (hcx, hcy).
        Spans roughly 22 px wide × 46 px tall (antenna to body bottom).
        state: "idle" | "busy" | "permission" | "none"
        scol:  hex colour for the antenna glow ball
        """
        blue      = "#4a5bd0"
        blue_dark = "#3848b8"

        # ── Antenna ──────────────────────────────────────────
        c.create_line(hcx, hcy - 11, hcx, hcy - 18,
                      fill=blue, width=2, capstyle="round")
        c.create_oval(hcx-4, hcy-22, hcx+4, hcy-14, fill=scol, outline="")

        # ── Head ─────────────────────────────────────────────
        c.create_oval(hcx-11, hcy-11, hcx+11, hcy+11,
                      fill=blue, outline=blue_dark, width=1)

        # ── Eyes ─────────────────────────────────────────────
        if state == "none":
            # sleepy closed lines
            c.create_line(hcx-8, hcy-2, hcx-3, hcy-2, fill="white", width=2)
            c.create_line(hcx+3, hcy-2, hcx+8, hcy-2, fill="white", width=2)
        elif state == "busy":
            # wide-open O_O eyes
            c.create_oval(hcx-9, hcy-6, hcx-2, hcy+1, fill="white", outline="")
            c.create_oval(hcx-8, hcy-5, hcx-3, hcy,   fill="#1a1a40", outline="")
            c.create_oval(hcx-7, hcy-5, hcx-6, hcy-4, fill="white",   outline="")
            c.create_oval(hcx+2, hcy-6, hcx+9, hcy+1, fill="white",   outline="")
            c.create_oval(hcx+3, hcy-5, hcx+8, hcy,   fill="#1a1a40", outline="")
            c.create_oval(hcx+4, hcy-5, hcx+5, hcy-4, fill="white",   outline="")
        else:
            # normal happy/worried eyes
            c.create_oval(hcx-9, hcy-5, hcx-2, hcy+2, fill="white", outline="")
            c.create_oval(hcx-8, hcy-4, hcx-3, hcy+1, fill="#1a1a40", outline="")
            c.create_oval(hcx-7, hcy-4, hcx-6, hcy-3, fill="white",   outline="")
            c.create_oval(hcx+2, hcy-5, hcx+9, hcy+2, fill="white",   outline="")
            c.create_oval(hcx+3, hcy-4, hcx+8, hcy+1, fill="#1a1a40", outline="")
            c.create_oval(hcx+4, hcy-4, hcx+5, hcy-3, fill="white",   outline="")

        # ── Mouth ────────────────────────────────────────────
        if state == "idle":
            # smile arc
            c.create_arc(hcx-5, hcy+1, hcx+5, hcy+9,
                         start=200, extent=140,
                         style="arc", outline="white", width=2)
        elif state == "busy":
            # open "O" mouth
            c.create_oval(hcx-3, hcy+3, hcx+3, hcy+8,
                          fill="white", outline="")
        elif state == "permission":
            # flat worried line
            c.create_line(hcx-5, hcy+6, hcx+5, hcy+6,
                          fill="white", width=2, capstyle="round")
        # "none" → no mouth (sad)

        # ── Body ─────────────────────────────────────────────
        bx, by = hcx-8, hcy+12
        c.create_rectangle(bx, by, bx+16, by+14,
                           fill=blue, outline=blue_dark, width=1)
        # little "C" badge
        c.create_text(hcx, by+7, text="C",
                      font=("Segoe UI", 7, "bold"), fill="white")

        # ── Arms ─────────────────────────────────────────────
        c.create_rectangle(bx-7, by+1, bx-1, by+9,
                           fill=blue, outline="")
        c.create_rectangle(bx+17, by+1, bx+23, by+9,
                           fill=blue, outline="")

    def _draw(self, W, H):
        c = self._canvas
        c.delete("all")
        # outer frame
        self._rrect(1, 1, W-1, H-1, 14,
                    fill=self.C_BG, outline=self.C_BORDER, width=1)
        self._draw_header(c, W)
        sess = self._sessions
        if sess:
            for i, s in enumerate(sess):
                col = i % self.COLS
                row = i // self.COLS
                cx  = self.PAD + col * (self.CARD_W + self.PAD)
                cy  = self.HDR_H + self.PAD + row * (self.CARD_H + self.PAD)
                self._draw_card(c, cx, cy, s)
        else:
            mid_y = self.HDR_H + (H - self.HDR_H - self.FTR_H) // 2
            c.create_text(W // 2, mid_y - 12, text="🏢  Office is empty",
                          font=("Segoe UI", 10), fill=self.C_FAINT, anchor="center")
            c.create_text(W // 2, mid_y + 10, text="Start Claude Code to see your bots",
                          font=("Segoe UI", 8), fill=self.C_FAINT, anchor="center")
        self._draw_footer(c, W, H)

    def _draw_header(self, c, W):
        c.create_line(0, self.HDR_H, W, self.HDR_H, fill=self.C_BORDER)
        # Office icon — drawn as a simple building silhouette
        bx, by = 10, 8
        c.create_rectangle(bx,    by+6, bx+18, by+26, fill="#4a5bd0", outline="")
        c.create_rectangle(bx+2,  by,   bx+8,  by+6,  fill="#4a5bd0", outline="")
        c.create_rectangle(bx+10, by+2, bx+16, by+6,  fill="#4a5bd0", outline="")
        for wx in (bx+2, bx+10):
            c.create_rectangle(wx, by+10, wx+4, by+16, fill=self.C_BG, outline="")
        c.create_text(34, 14, text="Claude's Digital Office",
                      font=("Segoe UI", 10, "bold"), anchor="w", fill=self.C_TEXT)
        n     = len(self._sessions)
        badge = f"{n} session{'s' if n != 1 else ''}"
        c.create_text(34, 30, text=badge,
                      font=("Segoe UI", 8), anchor="w", fill=self.C_DIM)
        # overall state dot + label top-right
        st  = self.ov.state
        col = self.STATE_COL.get(st, self.C_FAINT)
        lbl = {"idle": "All idle", "busy": "Working",
               "permission": "Needs approval", "none": "No sessions"}.get(st, st)
        c.create_oval(W-72, 17, W-63, 26, fill=col, outline="")
        c.create_text(W-57, 21, text=lbl,
                      font=("Segoe UI", 8, "bold"), anchor="w", fill=col)

    def _draw_card(self, c, x, y, sess):
        state = sess.get("state", "none")
        scol  = self.STATE_COL.get(state, self.C_FAINT)
        cwd   = sess.get("cwd", "") or ""
        name  = os.path.basename(cwd.rstrip("\\/")) or sess.get("session_id", "?")[:16]
        tool  = sess.get("tool", "") or ""
        ts    = sess.get("ts", time.time())
        age   = int(time.time() - ts)
        ago   = (f"{age}s" if age < 60 else
                 f"{age//60}m" if age < 3600 else
                 f"{age//3600}h") + " ago"

        CW, CH = self.CARD_W, self.CARD_H

        # card background
        self._rrect(x, y, x+CW, y+CH, 8,
                    fill=self.C_SURF,
                    outline=scol if state != "none" else self.C_BORDER,
                    width=1)
        # left accent bar
        c.create_rectangle(x+1, y+8, x+4, y+CH-8, fill=scol, outline="")

        # ── Text area to the right of the robot ──────────────
        tx = x + 50   # text start x

        # ── Claude robot (canvas primitives) ─────────────────
        robot_cx = x + 25
        robot_cy = y + 58
        self._draw_robot(c, robot_cx, robot_cy, state, scol)

        # ── Chat bubble — constrained to the robot zone so it never
        #    covers the text area that starts at tx.
        if state == "busy" and tool:
            bubble = (tool[:6] + "..") if len(tool) > 7 else tool
        elif state == "idle":
            bubble = "Ready!"
        elif state == "permission":
            bubble = "Approve!"
        else:
            bubble = ""

        if bubble:
            bx  = robot_cx - 10
            by  = robot_cy - 46    # above antenna tip
            # Clamp right edge to stay 4 px left of the text area.
            bw  = min(len(bubble) * 5 + 10, tx - bx - 4)
            bh  = 13
            c.create_rectangle(bx, by, bx+bw, by+bh,
                                fill="white", outline="", tags="bubble")
            c.create_text(bx + bw//2, by + bh//2, text=bubble,
                          font=("Segoe UI", 7), fill="#111", tags="bubble")
            c.create_polygon(bx+6, by+bh, bx+10, by+bh, bx+7, by+bh+5,
                             fill="white", outline="", tags="bubble")

        # session name — anchor nw so it flows downward and can use width
        # for soft-wrapping; show up to 40 chars before hard-truncating.
        name_display = name if len(name) <= 40 else name[:38] + ".."
        c.create_text(tx, y + 8, text=name_display,
                      font=("Segoe UI", 9, "bold"), anchor="nw", fill=self.C_TEXT,
                      width=CW - (tx - x) - 6)

        # state pill (moved down to clear two-line name room)
        lbl = self.STATE_LABEL.get(state, state)
        pw  = len(lbl) * 6 + 10
        c.create_rectangle(tx, y+44, tx+pw, y+56,
                           fill=scol + "22", outline=scol, width=1)
        c.create_text(tx + pw//2, y+50, text=lbl,
                      font=("Segoe UI", 7, "bold"), fill=scol, anchor="center")

        # detail line
        if state == "busy" and tool:
            detail = tool
        elif state == "permission" and tool:
            detail = f"Approve: {tool}?"
        elif state == "idle":
            detail = "Waiting for input"
        else:
            detail = ""
        if detail:
            c.create_text(tx, y+64, text=detail,
                          font=("Segoe UI", 7), anchor="nw", fill=self.C_DIM,
                          width=CW - (tx - x) - 6)

        # device badge (remote sessions)
        dev = sess.get("device", "local")
        if dev and dev != "local":
            dev_name = (sess.get("device_name") or dev.upper())[:8]
            c.create_text(x+CW-6, y+10, text=dev_name,
                          font=("Segoe UI", 7, "bold"), anchor="e", fill="#7a8aaa")

        # timestamp bottom-right
        c.create_text(x+CW-6, y+CH-6, text=ago,
                      font=("Segoe UI", 7), anchor="se", fill=self.C_FAINT)

    def _draw_footer(self, c, W, H):
        c.create_line(0, H-self.FTR_H, W, H-self.FTR_H, fill=self.C_BORDER)
        hint = ("📌 Pinned — click to unpin  ·  Esc to close"
                if self._pinned
                else "Hover to peek  ·  Click to pin")
        c.create_text(12, H-self.FTR_H+14, text=hint,
                      font=("Segoe UI", 8), anchor="w", fill=self.C_FAINT)
        c.create_text(W-12, H-self.FTR_H+14, text=time.strftime("%H:%M:%S"),
                      font=("Segoe UI", 8), anchor="e", fill=self.C_FAINT)

    # ── public API ────────────────────────────────────────────
    def show(self):
        self._sessions = read_all_sessions()
        if self._win is None or not self._win.winfo_exists():
            self._build()
        x, y, W, H = self._geometry()
        self._win.geometry(f"{W}x{H}+{x}+{y}")
        self._canvas.config(width=W, height=H)
        self._draw(W, H)
        self._win.deiconify()
        self.visible = True

    def refresh(self):
        if not self.visible:
            return
        if self._win is None or not self._win.winfo_exists():
            return
        self._sessions = read_all_sessions()
        x, y, W, H = self._geometry()
        self._win.geometry(f"{W}x{H}+{x}+{y}")
        self._canvas.config(width=W, height=H)
        self._draw(W, H)

    def hide(self):
        if self._pinned:
            return
        if self._win and self._win.winfo_exists():
            self._win.withdraw()
        self.visible = False

    def force_hide(self):
        self._pinned = False
        self.hide()

    def destroy(self):
        if self._win and self._win.winfo_exists():
            self._win.destroy()
        self._win = None

    # ── interaction ──────────────────────────────────────────
    def _toggle_pin(self):
        self._pinned = not self._pinned
        if not self._pinned:
            self.hide()
        else:
            self.refresh()

    def cancel_hide(self):
        if self._hide_id:
            self.ov.root.after_cancel(self._hide_id)
            self._hide_id = None

    def start_hide(self):
        self.cancel_hide()
        self._hide_id = self.ov.root.after(220, self._schedule_hide)

    def _schedule_hide(self):
        """Only hide if mouse has genuinely left both the HUD widget and this popup."""
        self._hide_id = None
        if self._pinned:
            return
        try:
            px = self.ov.root.winfo_pointerx()
            py = self.ov.root.winfo_pointery()
        except tk.TclError:
            return
        # Mouse still over HUD widget?
        hx = self.ov.root.winfo_x()
        hy = self.ov.root.winfo_y()
        if hx <= px <= hx + self.ov.W and hy <= py <= hy + self.ov.H:
            return
        # Mouse still over popup?
        if self._win and self._win.winfo_exists():
            wx = self._win.winfo_x()
            wy = self._win.winfo_y()
            ww = self._win.winfo_width()
            wh = self._win.winfo_height()
            if wx <= px <= wx + ww and wy <= py <= wy + wh:
                return
        self.hide()

    def _on_enter(self, _e):
        self.cancel_hide()

    def _on_leave(self, _e):
        self.start_hide()


# ----------------------------------------------------------------------------
# Overlay window
# ----------------------------------------------------------------------------
class Overlay:
    W, H  = 280, 148   # connected card: traffic-light + usage panel
    H_TOP = 47         # height of traffic-light section

    def __init__(self, cfg):
        self.cfg          = cfg
        self.state        = "none"
        self.summary      = ("No sessions", "")
        self.phase        = 0.0
        self._drag        = None
        self._tray_icon   = None
        self._dev_summary: dict = {}

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

        self.title_font  = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.detail_font = tkfont.Font(family="Segoe UI", size=8)
        self.ph_font     = tkfont.Font(family="Segoe UI", size=7,  weight="bold")
        self.usg_font    = tkfont.Font(family="Segoe UI", size=8,  weight="bold")

        # Usage animation state
        self.usage_fills:  list  = []
        self.usage_tracks: list  = []
        self.usage_vals:   list  = []
        self._usage_pcts:  list  = [0.0, 0.0, 0.0]
        self._usage_mtime: float = 0.0
        self._bar_anim:    list  = [None, None, None]

        self.popup = OfficePopup(self)

        self._build()
        self._bind()
        self._build_menu()

        self.root.deiconify()
        self.root.after(0,    self._tick)
        self.root.after(150,  self._poll)
        self.root.after(2000, self._keep_top)
        self.root.after(1500, self._popup_refresh)
        self.root.after(120,  self._hover_poll)

    # ---- drawing ----
    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
            x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _build(self):
        c = self.canvas
        TX1, TX2 = 73, 193  # track x-range (120px wide)

        # ── Outer card (single rounded rect for both sections) ──
        self._round_rect(1, 1, self.W - 1, self.H - 1, 16,
                         fill=PANEL_BG, outline=PANEL_OUTLINE, width=1)

        # Divider between traffic-light and usage panel
        c.create_line(1, self.H_TOP, self.W - 1, self.H_TOP,
                      fill=DIVIDER_COL, width=1)

        # ── Traffic-light section ──
        cx, cy = 36, self.H_TOP // 2
        self.glow    = c.create_oval(cx - 18, cy - 18, cx + 18, cy + 18,
                                     fill=PANEL_BG, outline="")
        self.led_dot = c.create_oval(cx - 11, cy - 11, cx + 11, cy + 11,
                                     fill="#555", outline="")
        self.title_item  = c.create_text(
            62, cy - 7, anchor="w", text="Starting…",
            fill="#f4f5f7", font=self.title_font)
        self.detail_item = c.create_text(
            62, cy + 9, anchor="w", text="",
            fill=TEXT_DIM_C, font=self.detail_font)

        # ── Usage panel section ──
        c.create_text(18, self.H_TOP + 16, anchor="w", text="API USAGE",
                      fill=TEXT_FAINT_C, font=self.ph_font)

        row_ctrs = [self.H_TOP + 37, self.H_TOP + 56, self.H_TOP + 75]
        row_lbls = ["Session", "Weekly", "Monthly"]

        self.usage_tracks = []
        self.usage_fills  = []
        self.usage_vals   = []

        for ry, lbl in zip(row_ctrs, row_lbls):
            c.create_text(18, ry, anchor="w", text=lbl,
                          fill=TEXT_DIM_C, font=self.usg_font)
            tr = c.create_rectangle(TX1, ry - 1, TX2, ry + 2,
                                    fill=TRACK_BG_C, outline="")
            self.usage_tracks.append(tr)
            fl = c.create_rectangle(TX1, ry - 1, TX1, ry + 2,
                                    fill=USG_GREEN, outline="")
            self.usage_fills.append(fl)
            vl = c.create_text(self.W - 16, ry, anchor="e", text="—",
                                fill=TEXT_DIM_C, font=self.usg_font)
            self.usage_vals.append(vl)

    def _set_dot(self, rgb, glow_alpha):
        hexc = "#%02x%02x%02x" % rgb
        self.canvas.itemconfig(self.led_dot, fill=hexc)
        gr = tuple(int(PANEL_BG_RGB[i] + (rgb[i]-PANEL_BG_RGB[i]) * glow_alpha)
                   for i in range(3))
        self.canvas.itemconfig(self.glow, fill="#%02x%02x%02x" % gr)

    # ---- usage panel ----
    def _usage_color(self, pct: float) -> str:
        if pct >= 85:
            return USG_RED
        if pct >= 60:
            return USG_AMBER
        return USG_GREEN

    def _refresh_usage(self):
        try:
            mtime = os.path.getmtime(USAGE_PATH)
        except OSError:
            return  # No usage file yet — bars stay at dashes
        if mtime <= self._usage_mtime:
            return
        self._usage_mtime = mtime
        data = _load_usage()
        if data is None:
            return

        session_pct = float(data.get("session_pct", 0))
        weekly_h    = float(data.get("weekly_h",    0))
        weekly_max  = float(data.get("weekly_max",  80))
        monthly_pct = float(data.get("monthly_pct", 0))
        weekly_pct  = (weekly_h / weekly_max * 100) if weekly_max > 0 else 0

        targets = [session_pct, weekly_pct, monthly_pct]
        labels  = [
            f"{session_pct:.0f}%",
            f"{weekly_h:.1f}h / {weekly_max:.0f}h",
            f"{monthly_pct:.0f}%",
        ]

        for i in range(3):
            old_pct = self._usage_pcts[i]
            new_pct = targets[i]
            col     = self._usage_color(new_pct)
            delay   = i * 45
            self.root.after(
                delay,
                lambda i=i, old=old_pct, new=new_pct, lbl=labels[i], c=col:
                    self._animate_bar_to(i, old, new, lbl, c),
            )
            self._usage_pcts[i] = new_pct

    def _animate_bar_to(self, idx: int, start_pct: float, end_pct: float,
                        label: str, color: str, duration_ms: int = 520):
        if self._bar_anim[idx] is not None:
            try:
                self.root.after_cancel(self._bar_anim[idx])
            except Exception:
                pass
            self._bar_anim[idx] = None

        TX1, TX2  = 73, 193
        track_w   = TX2 - TX1          # 120 px
        start_t   = time.time()
        val_color = (USG_RED   if end_pct >= 85 else
                     USG_AMBER if end_pct >= 60 else TEXT_DIM_C)
        ry        = self.H_TOP + 37 + idx * 19

        def _step():
            elapsed = (time.time() - start_t) * 1000
            t       = min(elapsed / duration_ms, 1.0)
            ease    = 1.0 - (1.0 - t) ** 3   # cubic ease-out
            pct     = start_pct + (end_pct - start_pct) * ease
            fill_x2 = TX1 + int(track_w * max(0.0, pct) / 100)
            self.canvas.coords(self.usage_fills[idx],
                               TX1, ry - 1, fill_x2, ry + 2)
            self.canvas.itemconfig(self.usage_fills[idx], fill=color)
            self.canvas.itemconfig(self.usage_vals[idx],
                                   text=label, fill=val_color)
            if t < 1.0:
                self._bar_anim[idx] = self.root.after(16, _step)
            else:
                self._bar_anim[idx] = None

        self._bar_anim[idx] = self.root.after(0, _step)

    # ---- animation ----
    def _tick(self):
        self.phase += 0.06
        base = tuple(self.cfg["colors"].get(self.state, [120, 120, 120]))
        if self.state == "busy":
            f    = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(self.phase * 1.7))
            rgb  = tuple(int(b * f) for b in base)
            glow = 0.25 + 0.35 * (0.5 + 0.5 * math.sin(self.phase * 1.7))
        elif self.state == "permission":
            f    = 0.85 + 0.15 * (0.5 + 0.5 * math.sin(self.phase * 2.4))
            rgb  = tuple(int(b * f) for b in base)
            glow = 0.35 + 0.25 * (0.5 + 0.5 * math.sin(self.phase * 2.4))
        elif self.state == "idle":
            rgb  = base; glow = 0.30
        else:
            rgb  = base; glow = 0.10
        self._set_dot(rgb, glow)
        self.root.after(33, self._tick)

    def _poll(self):
        primary = self.cfg.get("primary_device", "local")
        state, summary, _, dev_summary = aggregate_state(primary)
        self._dev_summary = dev_summary
        if state != self.state or summary != self.summary:
            self.state   = state
            self.summary = summary
            self.canvas.itemconfig(self.title_item,  text=summary[0])
            self.canvas.itemconfig(self.detail_item, text=summary[1])
            self.hw.apply(state)
        self._refresh_usage()
        self.root.after(150, self._poll)

    def _keep_top(self):
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
            if self.popup.visible and self.popup._win and self.popup._win.winfo_exists():
                self.popup._win.attributes("-topmost", True)
                self.popup._win.lift()
        except tk.TclError:
            pass
        self.root.after(2000, self._keep_top)

    def _popup_refresh(self):
        if self.popup.visible:
            self.popup.refresh()
        self.root.after(1500, self._popup_refresh)

    # ---- drag + popup trigger ----
    def _bind(self):
        for seq in ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.canvas.bind(seq, self._on_mouse)
        self.canvas.bind("<Button-3>", self._show_menu)
        self.canvas.bind("<Enter>", self._on_hover_enter)
        self.canvas.bind("<Leave>", self._on_hover_leave)
        self.root.bind("<Escape>", lambda _e: self.popup.force_hide())

    def _on_mouse(self, e):
        if e.type == tk.EventType.ButtonPress:
            self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        elif e.type == tk.EventType.Motion and self._drag:
            dx = e.x_root - self._drag[0]
            dy = e.y_root - self._drag[1]
            self.root.geometry(f"+{self._drag[2]+dx}+{self._drag[3]+dy}")
        elif e.type == tk.EventType.ButtonRelease and self._drag:
            dx = abs(e.x_root - self._drag[0])
            dy = abs(e.y_root - self._drag[1])
            if dx < 4 and dy < 4:
                # It was a click — toggle popup pin
                trigger = self.cfg.get("office_popup", {}).get("trigger", "hover")
                if trigger != "none":
                    self.popup._toggle_pin()
                    if self.popup._pinned:
                        self.popup.show()
            else:
                self.cfg["position"] = {"x": self.root.winfo_x(), "y": self.root.winfo_y()}
                save_config(self.cfg)
            self._drag = None

    def _on_hover_enter(self, _e):
        trigger = self.cfg.get("office_popup", {}).get("trigger", "hover")
        if trigger != "hover":
            return
        self.popup.cancel_hide()
        self.popup.show()

    def _on_hover_leave(self, _e):
        trigger = self.cfg.get("office_popup", {}).get("trigger", "hover")
        if trigger == "none":
            return
        self.popup.start_hide()

    def _hover_poll(self):
        """Fallback hover detection — catches cases where <Enter> is not re-fired
        after the popup appears/disappears (common on Windows overrideredirect windows)."""
        try:
            trigger = self.cfg.get("office_popup", {}).get("trigger", "hover")
            if trigger == "hover" and not self.popup.visible:
                px = self.root.winfo_pointerx()
                py = self.root.winfo_pointery()
                hx = self.root.winfo_x()
                hy = self.root.winfo_y()
                if hx <= px <= hx + self.W and hy <= py <= hy + self.H:
                    self.popup.cancel_hide()
                    self.popup.show()
        except tk.TclError:
            pass
        self.root.after(120, self._hover_poll)

    # ---- context menu ----
    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Snap to next monitor", command=self._next_monitor)
        self.menu.add_command(label="Reset position",       command=self._reset_position)
        self.menu.add_separator()

        self._kbvar = tk.BooleanVar(value=self.cfg["outputs"]["keyboard_mouse"])
        self.menu.add_checkbutton(label="Light keyboard + mouse",
                                  variable=self._kbvar, command=self._toggle_hw)
        self.menu.add_separator()

        # Office popup trigger submenu
        popup_cfg = self.cfg.get("office_popup", {})
        self._popup_trigger_var = tk.StringVar(value=popup_cfg.get("trigger", "hover"))
        popup_menu = tk.Menu(self.menu, tearoff=0)
        popup_menu.add_radiobutton(
            label="On hover (peek)",
            value="hover", variable=self._popup_trigger_var,
            command=self._set_popup_trigger,
        )
        popup_menu.add_radiobutton(
            label="Click only (pin/unpin)",
            value="click", variable=self._popup_trigger_var,
            command=self._set_popup_trigger,
        )
        popup_menu.add_radiobutton(
            label="Disabled",
            value="none", variable=self._popup_trigger_var,
            command=self._set_popup_trigger,
        )
        self.menu.add_cascade(label="Office popup", menu=popup_menu)
        self.menu.add_separator()

        # Primary device submenu — populated dynamically in _show_menu
        self._primary_menu = tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="Primary device", menu=self._primary_menu)
        self.menu.add_separator()

        self._startupvar = tk.BooleanVar(value=get_startup_enabled())
        self.menu.add_checkbutton(label="Run on Startup",
                                  variable=self._startupvar, command=self._toggle_startup)
        self.menu.add_separator()
        self.menu.add_command(label="Quit HUD", command=self._quit)

    def _show_menu(self, e):
        # Rebuild the primary-device submenu with current known devices.
        m = self._primary_menu
        m.delete(0, "end")
        current  = self.cfg.get("primary_device", "local")
        devices  = {"local": "This PC (local)"}
        devices.update(self.cfg.get("devices", {}))

        self._primary_var = tk.StringVar(value=current)
        for dev_id, dev_name in devices.items():
            m.add_radiobutton(
                label=dev_name,
                value=dev_id,
                variable=self._primary_var,
                command=lambda d=dev_id: self._set_primary(d),
            )

        self._startupvar.set(get_startup_enabled())
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _set_primary(self, dev_id: str):
        self.cfg["primary_device"] = dev_id
        save_config(self.cfg)
        log(f"primary device set to: {dev_id}")

    def _next_monitor(self):
        mons = get_monitors()
        if len(mons) < 2:
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        cur  = 0
        for i, (l, t, r, b) in enumerate(mons):
            if l <= x < r and t <= y <= b:
                cur = i; break
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

    def _toggle_startup(self):
        set_startup_enabled(bool(self._startupvar.get()))

    def _set_popup_trigger(self):
        trigger = self._popup_trigger_var.get()
        self.cfg.setdefault("office_popup", {})["trigger"] = trigger
        save_config(self.cfg)
        if trigger == "none":
            self.popup.force_hide()

    def _quit(self):
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.popup.destroy()
        self.hw.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ----------------------------------------------------------------------------
# System tray icon
# ----------------------------------------------------------------------------
def setup_tray(overlay: Overlay):
    try:
        import pystray
        from PIL import Image
    except ImportError:
        log("pystray/Pillow not installed — tray icon unavailable")
        return None

    ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traffic_light.ico")
    try:
        img = Image.open(ico)
    except Exception:
        img = Image.new("RGBA", (64, 64), (45, 205, 95, 255))

    def on_startup(icon, item):
        new_val = not get_startup_enabled()
        set_startup_enabled(new_val)
        overlay.root.after(0, lambda: overlay._startupvar.set(new_val))

    def on_quit(icon, item):
        icon.stop()
        overlay.root.after(0, overlay._quit)

    menu = pystray.Menu(
        pystray.MenuItem("Run on Startup", on_startup,
                         checked=lambda item: get_startup_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit HUD", on_quit),
    )

    icon = pystray.Icon("ClaudeStatusHUD", img, "Claude Status HUD", menu=menu)
    icon.run_detached()
    overlay._tray_icon = icon
    log("tray icon started")
    return icon


# ----------------------------------------------------------------------------
# Single-instance guard
# ----------------------------------------------------------------------------
def single_instance(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", int(port)))
        s.listen(1)
        return s
    except OSError:
        return None


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in STATE_CLI_MAP:
            os.makedirs(SESSIONS_DIR, exist_ok=True)
            handle_cli_state(arg)
            return
        log(f"unknown CLI arg '{arg}'. Valid: {', '.join(STATE_CLI_MAP)}")
        return

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    set_dpi_aware()
    cfg = load_config()
    if not os.path.exists(CONFIG_PATH):
        save_config(cfg)

    # Expose config to the remote receiver handler.
    _remote_cfg.update(cfg)

    guard = single_instance(cfg.get("port", 51789))
    if guard is None:
        log("another HUD instance is already running; exiting")
        return

    start_remote_receiver(cfg)

    log("HUD daemon starting")
    overlay = Overlay(cfg)
    setup_tray(overlay)
    overlay.run()


if __name__ == "__main__":
    main()
