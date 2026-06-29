"""
Claude Code -> Status HUD bridge.

Registered for every relevant hook event. Reads the hook JSON from stdin,
maps the event to one of the HUD states, and writes a tiny per-session file
that the HUD daemon reads. Must be fast, silent, and never block Claude:
it prints nothing to stdout and always exits 0.

Event -> state mapping:
    SessionStart      -> idle        (register the session)
    UserPromptSubmit  -> busy        (you sent a prompt; Claude is working)
    PreToolUse        -> busy        (about to run a tool)
    PostToolUse       -> busy        (still working)
    SubagentStop      -> busy        (a subagent finished; main agent continues)
    Notification      -> permission  (needs approval) / idle (waiting for input)
    Stop              -> idle        (finished; waiting for your next input)
    SessionEnd        -> remove the session file

State only — usage metrics are computed by the background scanner thread
in hud_daemon.pyw, not here.
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

HUD_DIR = os.path.join(os.path.expanduser("~"), ".claude", "hud")
SESSIONS_DIR = os.path.join(HUD_DIR, "sessions")
USAGE_PATH   = os.path.join(HUD_DIR, "usage.json")
STATE_PATH   = os.path.join(HUD_DIR, "state.json")

BUSY_EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop"}


def safe_session_id(sid):
    keep = [c if (c.isalnum() or c in "-_") else "_" for c in str(sid)]
    return ("".join(keep) or "default")[:80]


def decide_state(event, data):
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


# ---------------------------------------------------------------------------
# State tracker helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict):
    try:
        os.makedirs(HUD_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def _week_key(now: datetime) -> str:
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return week_start.date().isoformat()


def _update_session_time(sid: str, event: str):
    """Track session start/last-seen timestamps in state.json."""
    state = _load_state()
    now = datetime.now(timezone.utc)
    wk = _week_key(now)
    if state.get("week_start") != wk:
        state["week_start"] = wk
        state["weekly_tokens"] = 0

    sessions = state.setdefault("sessions", {})

    if event == "SessionEnd":
        sessions.pop(sid, None)
    elif event == "SessionStart":
        sessions[sid] = {"start_ts": time.time(), "last_ts": time.time()}
    else:
        if sid in sessions:
            sessions[sid]["last_ts"] = time.time()
        else:
            # Missed SessionStart — treat now as the start
            sessions[sid] = {"start_ts": time.time(), "last_ts": time.time()}

    state["ts"] = time.time()
    _save_state(state)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        data = {}

    event = data.get("hook_event_name", "")
    sid = safe_session_id(data.get("session_id", "default"))
    path = os.path.join(SESSIONS_DIR, f"{sid}.json")

    try:
        os.makedirs(SESSIONS_DIR, exist_ok=True)

        # Always update session timing in state.json
        try:
            _update_session_time(sid, event)
        except Exception:
            pass

        if event == "SessionEnd":
            try:
                os.remove(path)
            except OSError:
                pass
            return

        # Preserve cwd from the previous record when the current event omits it
        new_cwd = data.get("cwd", "")
        if not new_cwd:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    new_cwd = json.load(fh).get("cwd", "")
            except (OSError, ValueError):
                pass

        record = {
            "session_id": sid,
            "state": decide_state(event, data),
            "ts": time.time(),
            "event": event,
            "tool": data.get("tool_name", ""),
            "cwd": new_cwd,
            "message": data.get("message", ""),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, path)

    except OSError:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
