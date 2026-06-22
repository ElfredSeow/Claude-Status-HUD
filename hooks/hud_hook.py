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

On Stop events, also scans ~/.claude/projects/**/*.jsonl to compute
session/weekly/monthly costs and writes ~/.claude/hud/usage.json.
"""

import os
import sys
import json
import time
import glob
from datetime import datetime, timezone, timedelta

HUD_DIR = os.path.join(os.path.expanduser("~"), ".claude", "hud")
SESSIONS_DIR = os.path.join(HUD_DIR, "sessions")
USAGE_PATH   = os.path.join(HUD_DIR, "usage.json")

BUSY_EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop"}

# Anthropic pricing (Sonnet 4.x, per token)
_PRICE_INPUT   = 3.00e-6
_PRICE_OUTPUT  = 15.0e-6
_PRICE_CACHE_W = 3.75e-6
_PRICE_CACHE_R = 0.30e-6
_CTX_WINDOW    = 200_000   # Sonnet context window in tokens


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
# Usage computation from JSONL files
# ---------------------------------------------------------------------------

def _token_cost(inp, out, cw, cr):
    return (inp * _PRICE_INPUT + out * _PRICE_OUTPUT +
            cw  * _PRICE_CACHE_W + cr * _PRICE_CACHE_R)


def _write_usage(data: dict, session_id: str):
    """
    Scan all ~/.claude/projects/**/*.jsonl to aggregate real token costs.
    Also estimates session context-window % from the most recent assistant
    message in the current session's JSONL file.
    Writes the result to USAGE_PATH atomically.
    """
    now         = datetime.now(timezone.utc)
    week_start  = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    weekly_cost  = 0.0
    monthly_cost = 0.0
    session_ctx_tokens = 0  # max(input + cache_read) seen in current session

    projects_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    # Only scan files touched in the last 32 days (covers full month + buffer)
    cutoff_mtime = time.time() - 32 * 86400
    session_file = f"{session_id}.jsonl"

    for path in glob.glob(os.path.join(projects_dir, "**", "*.jsonl"),
                          recursive=True):
        try:
            if os.path.getmtime(path) < cutoff_mtime:
                continue
        except OSError:
            continue

        is_current = (os.path.basename(path) == session_file)

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue

                    if rec.get("type") != "assistant":
                        continue
                    if rec.get("isSidechain", False):
                        continue

                    ts_raw = rec.get("timestamp", "")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(
                            ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    msg   = rec.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue

                    inp = int(usage.get("input_tokens", 0) or 0)
                    out = int(usage.get("output_tokens", 0) or 0)
                    cw  = int(usage.get("cache_creation_input_tokens", 0) or 0)
                    cr  = int(usage.get("cache_read_input_tokens", 0) or 0)

                    cost = _token_cost(inp, out, cw, cr)

                    if ts >= month_start:
                        monthly_cost += cost
                    if ts >= week_start:
                        weekly_cost += cost

                    # Context window estimate: context = input + cached reads
                    if is_current:
                        ctx = inp + cr
                        if ctx > session_ctx_tokens:
                            session_ctx_tokens = ctx

        except OSError:
            continue

    session_ctx_pct = min(session_ctx_tokens / _CTX_WINDOW * 100, 100.0)

    usage_data = {
        "session_ctx_pct": round(session_ctx_pct, 1),
        "weekly_cost":     round(weekly_cost,  4),
        "monthly_cost":    round(monthly_cost, 4),
        "ts":              time.time(),
    }

    try:
        os.makedirs(HUD_DIR, exist_ok=True)
        tmp = USAGE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(usage_data, fh)
        os.replace(tmp, USAGE_PATH)
    except OSError:
        pass


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

        # On Stop events, recompute usage stats from JSONL files
        if event == "Stop":
            try:
                _write_usage(data, sid)
            except Exception:
                pass

    except OSError:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
