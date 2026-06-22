"""
Claude Traffic Light — launcher window.

A small GUI app that shows in the Windows taskbar. Use it to start/stop the
HUD daemon and toggle run-on-startup.  Pin the desktop shortcut (created by
create-shortcut.ps1) to the taskbar for one-click access.
"""

import os
import sys
import socket
import subprocess
import winreg
import time
import tkinter as tk
import tkinter.font as tkfont

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
START_CMD   = os.path.join(PROJECT_DIR, "start-hud.cmd")
STOP_CMD    = os.path.join(PROJECT_DIR, "stop-hud.cmd")
ICON_PATH   = os.path.join(SCRIPT_DIR, "traffic_light.ico")
DAEMON_PY   = os.path.join(SCRIPT_DIR, "hud_daemon.pyw")

HUD_PORT = 51789
_STARTUP_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_NAME = "ClaudeStatusHUD"

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BG          = "#171a21"
BG_CARD     = "#1e2230"
BORDER      = "#2b303b"
TEXT        = "#f4f5f7"
TEXT_DIM    = "#9aa3b2"
GREEN       = "#2dcd5f"
AMBER       = "#ff9600"
RED         = "#eb2d2d"
GREY        = "#5f646e"
BTN_START   = "#163a22"
BTN_STOP    = "#3a1616"
BTN_ACTIVE  = "#0d2617"
BTN_STA_ACT = "#2d1010"
BTN_TXT     = "#c8ffd8"
BTN_STP_TXT = "#ffc8c8"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pythonw():
    pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pw if os.path.exists(pw) else sys.executable


def is_running() -> bool:
    """True if the HUD daemon is alive (listening on its guard port)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        s.connect(("127.0.0.1", HUD_PORT))
        s.close()
        return True
    except OSError:
        return False


def start_hud():
    """Launch the daemon in the background via start-hud.cmd."""
    subprocess.Popen(
        ["cmd", "/c", START_CMD],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def stop_hud():
    """Kill any running daemon process."""
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" "
         "| Where-Object { $_.CommandLine -like '*hud_daemon.pyw*' } "
         "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def get_startup() -> bool:
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(k, _STARTUP_NAME)
        winreg.CloseKey(k)
        return True
    except OSError:
        return False


def set_startup(enabled: bool):
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            cmd = f'"{_pythonw()}" "{DAEMON_PY}"'
            winreg.SetValueEx(k, _STARTUP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, _STARTUP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(k)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# App window
# ---------------------------------------------------------------------------

class LauncherApp:
    W, H = 290, 198

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Claude Traffic Light")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.root.geometry(f"{self.W}x{self.H}")

        # Put window in the centre of the primary screen.
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - self.W) // 2
        y = (sh - self.H) // 2
        self.root.geometry(f"+{x}+{y}")

        # Traffic-light icon (taskbar + title bar).
        try:
            self.root.iconbitmap(ICON_PATH)
        except Exception:
            pass

        # Fonts
        self.font_head  = tkfont.Font(family="Segoe UI", size=12, weight="bold")
        self.font_label = tkfont.Font(family="Segoe UI", size=9)
        self.font_small = tkfont.Font(family="Segoe UI", size=8)
        self.font_btn   = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        self._build()
        self._poll()

    # ---- UI construction --------------------------------------------------

    def _build(self):
        root = self.root

        # Header row: icon dot + title
        header = tk.Frame(root, bg=BG, pady=12)
        header.pack(fill="x", padx=16)

        self.dot = tk.Canvas(header, width=14, height=14, bg=BG,
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot_oval = self.dot.create_oval(1, 1, 13, 13, fill=GREY, outline="")

        title_lbl = tk.Label(header, text="Claude Traffic Light",
                             font=self.font_head, bg=BG, fg=TEXT)
        title_lbl.pack(side="left")

        # Divider
        tk.Frame(root, height=1, bg=BORDER).pack(fill="x")

        # Status row
        status_row = tk.Frame(root, bg=BG, pady=10)
        status_row.pack(fill="x", padx=16)
        tk.Label(status_row, text="HUD status:", font=self.font_label,
                 bg=BG, fg=TEXT_DIM).pack(side="left")
        self.status_lbl = tk.Label(status_row, text="Checking…",
                                   font=self.font_label, bg=BG, fg=TEXT_DIM)
        self.status_lbl.pack(side="left", padx=(6, 0))

        # Buttons row
        btn_row = tk.Frame(root, bg=BG)
        btn_row.pack(fill="x", padx=16, pady=(0, 2))

        self.btn_start = self._make_btn(
            btn_row, "▶  Start HUD", BTN_START, BTN_ACTIVE, BTN_TXT,
            self._on_start,
        )
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 6))

        self.btn_stop = self._make_btn(
            btn_row, "■  Stop HUD", BTN_STOP, BTN_STA_ACT, BTN_STP_TXT,
            self._on_stop,
        )
        self.btn_stop.pack(side="left", expand=True, fill="x")

        # Divider
        tk.Frame(root, height=1, bg=BORDER).pack(fill="x", pady=(10, 0))

        # Startup toggle
        startup_row = tk.Frame(root, bg=BG, pady=10)
        startup_row.pack(fill="x", padx=14)

        self._startup_var = tk.BooleanVar(value=get_startup())
        cb = tk.Checkbutton(
            startup_row,
            text=" Run on Startup",
            variable=self._startup_var,
            command=self._on_startup_toggle,
            bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
            selectcolor="#252b38",
            font=self.font_label,
            bd=0, highlightthickness=0,
            cursor="hand2",
        )
        cb.pack(side="left")

        # Footer hint
        tk.Label(root, text="Right-click the overlay for more options",
                 font=self.font_small, bg=BG, fg="#5a6070").pack(pady=(0, 8))

    def _make_btn(self, parent, text, bg, active_bg, fg, cmd):
        btn = tk.Label(
            parent, text=text, bg=bg, fg=fg,
            font=self.font_btn, padx=8, pady=6,
            cursor="hand2", relief="flat",
        )
        btn.bind("<Button-1>", lambda e: cmd())
        btn.bind("<Enter>", lambda e: btn.config(bg=active_bg))
        btn.bind("<Leave>", lambda e: btn.config(bg=bg))
        return btn

    # ---- Actions ----------------------------------------------------------

    def _on_start(self):
        self.status_lbl.config(text="Starting…", fg=AMBER)
        self.root.after(100, start_hud)
        self.root.after(1800, self._refresh_status)

    def _on_stop(self):
        self.status_lbl.config(text="Stopping…", fg=AMBER)
        self.root.after(100, stop_hud)
        self.root.after(1500, self._refresh_status)

    def _on_startup_toggle(self):
        set_startup(self._startup_var.get())

    # ---- Status polling ---------------------------------------------------

    def _refresh_status(self):
        running = is_running()
        if running:
            self.status_lbl.config(text="Running", fg=GREEN)
            self.dot.itemconfig(self._dot_oval, fill=GREEN)
        else:
            self.status_lbl.config(text="Stopped", fg=GREY)
            self.dot.itemconfig(self._dot_oval, fill=GREY)

    def _poll(self):
        self._refresh_status()
        self.root.after(2000, self._poll)

    # ---- Main loop --------------------------------------------------------

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------

def main():
    app = LauncherApp()
    app.run()


if __name__ == "__main__":
    main()
