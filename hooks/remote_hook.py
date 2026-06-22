"""
Claude Code -> Windows HUD bridge (remote / Mac side).

Install this on your Mac exactly like the local hud_hook.py, except it
POSTs state updates over WiFi to the Windows HUD daemon instead of writing
local session files.

Configuration — set these environment variables (or edit the defaults below):

    CLAUDE_HUD_URL          URL of the Windows HUD receiver
                            e.g.  http://192.168.1.10:51790
                            Default: http://hud.local:51790

    CLAUDE_HUD_DEVICE_ID    Short identifier for this machine (no spaces)
                            Default: mac

    CLAUDE_HUD_DEVICE_NAME  Human-readable display name shown in the overlay
                            Default: MacBook

Usage in ~/.claude/settings.json (Mac):
    {
      "hooks": {
        "SessionStart":     [{"hooks": [{"type": "command", "command": "python3 /path/to/remote_hook.py"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 /path/to/remote_hook.py"}]}],
        "PreToolUse":       [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 /path/to/remote_hook.py"}]}],
        "PostToolUse":      [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 /path/to/remote_hook.py"}]}],
        "Notification":     [{"hooks": [{"type": "command", "command": "python3 /path/to/remote_hook.py"}]}],
        "Stop":             [{"hooks": [{"type": "command", "command": "python3 /path/to/remote_hook.py"}]}],
        "SubagentStop":     [{"hooks": [{"type": "command", "command": "python3 /path/to/remote_hook.py"}]}],
        "SessionEnd":       [{"hooks": [{"type": "command", "command": "python3 /path/to/remote_hook.py"}]}]
      }
    }

Or run the Mac installer (setup steps are in setup-mac.html) which does all
of the above automatically.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error

# ── Configuration ────────────────────────────────────────────────────────────
HUD_URL     = os.environ.get("CLAUDE_HUD_URL",         "http://hud.local:51790")
DEVICE_ID   = os.environ.get("CLAUDE_HUD_DEVICE_ID",   "mac")
DEVICE_NAME = os.environ.get("CLAUDE_HUD_DEVICE_NAME", "MacBook")
TIMEOUT_SEC = 2  # never block Claude for more than this

# ── State mapping (mirrors hud_hook.py logic) ────────────────────────────────
BUSY_EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop"}


def decide_state(event: str, data: dict) -> str:
    if event in BUSY_EVENTS:
        return "busy"
    if event == "Stop":
        return "idle"
    if event == "SessionStart":
        return "idle"
    if event == "Notification":
        msg = str(data.get("message", "")).lower()
        if "waiting for your input" in msg:
            return "idle"
        return "permission"
    return "idle"


def safe_sid(s) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "_" for c in str(s)]
    return ("".join(keep) or "default")[:80]


def main():
    # Read hook payload from Claude Code.
    try:
        raw  = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        data = {}

    event = data.get("hook_event_name", "")
    sid   = safe_sid(data.get("session_id", "default"))

    payload = {
        "session_id":  sid,
        "device_id":   DEVICE_ID,
        "device_name": DEVICE_NAME,
        "state":       "remove" if event == "SessionEnd" else decide_state(event, data),
        "event":       event,
        "tool":        data.get("tool_name", ""),
        "cwd":         data.get("cwd", ""),
        "message":     data.get("message", ""),
        "ts":          time.time(),
    }

    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        HUD_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT_SEC)
    except Exception:
        pass  # never block or error-out Claude Code


if __name__ == "__main__":
    main()
    sys.exit(0)  # never write to stdout (would pollute Claude context)
