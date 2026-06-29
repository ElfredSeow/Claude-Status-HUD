"""
Background JSONL usage scanner for the Claude Status HUD.

Reads ~/.claude/projects/**/*.jsonl every SCAN_INTERVAL seconds, computes the
current 5-hour session block using accurate token extraction, deduplication,
and model-aware pricing, and writes the result atomically to usage.json.

No tkinter or UI dependencies — designed to be imported and unit-tested
independently of the daemon.
"""

import os
import glob
import json
import time
import traceback as _traceback
from datetime import datetime, timezone, timedelta

_LOG_PATH = os.path.join(os.path.expanduser("~"), ".claude", "hud", "hud.log")

def _log_error(msg: str) -> None:
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{__import__('time').strftime('%H:%M:%S')} [scanner] {msg}\n")
    except OSError:
        pass

HUD_DIR     = os.path.join(os.path.expanduser("~"), ".claude", "hud")
USAGE_PATH  = os.path.join(HUD_DIR, "usage.json")
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
SCAN_INTERVAL = 60  # seconds

# Pricing per 1M tokens: (input, output, cache_write, cache_read)
# Matched by checking model.startswith(prefix), longest prefix wins.
_PRICING: list[tuple[str, tuple[float, float, float, float]]] = [
    ("claude-opus-4-5",   (15.00, 75.00, 18.75, 1.50)),
    ("claude-opus-4",     (15.00, 75.00, 18.75, 1.50)),
    ("claude-opus-3",     (15.00, 75.00, 18.75, 1.50)),
    ("claude-sonnet-4-6", (3.00,  15.00, 3.75,  0.30)),
    ("claude-sonnet-4",   (3.00,  15.00, 3.75,  0.30)),
    ("claude-sonnet-3-5", (3.00,  15.00, 3.75,  0.30)),
    ("claude-haiku-4-5",  (0.80,  4.00,  1.00,  0.08)),
    ("claude-haiku-4",    (0.80,  4.00,  1.00,  0.08)),
    ("claude-haiku-3-5",  (1.00,  5.00,  1.25,  0.10)),
]


def extract_tokens(rec: dict) -> tuple[int, int, int, int]:
    """Return (input, output, cache_write, cache_read) from a JSONL entry.

    Tries multiple field locations to handle Claude Code schema variations:
    primary message.usage, then data.usage, then camelCase aliases.
    """
    msg   = rec.get("message") or {}
    data  = rec.get("data") or {}
    usage = msg.get("usage") or data.get("usage") or {}

    def _get(*keys: str) -> int:
        for k in keys:
            for src in (usage, rec, msg, data):
                v = src.get(k)
                if v is not None:
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        pass
        return 0

    inp = _get("input_tokens",              "inputTokens",              "prompt_tokens")
    out = _get("output_tokens",             "outputTokens",             "completion_tokens")
    cw  = _get("cache_creation_input_tokens", "cacheCreationInputTokens")
    cr  = _get("cache_read_input_tokens",    "cacheReadInputTokens")
    return inp, out, cw, cr


def dedup_key(rec: dict) -> str:
    """Compute a stable deduplication key for a JSONL entry.

    Primary: {message_id}:{request_id}
    Fallback when a field is absent:
      - request_id missing → {message_id}:noreq
      - message_id missing → noid:{request_id}
      - both missing       → syn:{timestamp}:{inp}:{out}
    """
    mid = str(rec.get("message_id") or rec.get("messageId") or "").strip()
    rid = str(rec.get("request_id") or rec.get("requestId") or "").strip()

    if mid and rid:
        return f"{mid}:{rid}"
    if mid:
        return f"{mid}:noreq"
    if rid:
        return f"noid:{rid}"

    ts = rec.get("timestamp", "")
    inp, out, _, _ = extract_tokens(rec)
    return f"syn:{ts}:{inp}:{out}"


def detect_model(rec: dict) -> str:
    """Extract the model name from a JSONL entry (lowercased)."""
    msg  = rec.get("message") or {}
    data = rec.get("data") or {}
    for src in (msg, rec, data):
        m = src.get("model")
        if m:
            return str(m).lower()
    return "unknown"


def get_pricing(model: str) -> tuple[float, float, float, float]:
    """Return (input, output, cache_write, cache_read) price per 1M tokens.

    Matches by longest prefix first. Returns (0,0,0,0) for unknown models.
    """
    m = model.lower()
    for prefix, rates in _PRICING:
        if m.startswith(prefix):
            return rates
    return (0.0, 0.0, 0.0, 0.0)


def price_entry(inp: int, out: int, cw: int, cr: int, model: str) -> float:
    """Calculate cost in USD for a single JSONL entry."""
    pi, po, pcw, pcr = get_pricing(model)
    return (inp * pi + out * po + cw * pcw + cr * pcr) / 1_000_000


def compute_block(entries: list[dict], now: datetime) -> dict:
    """Compute the current 5-hour session block from a list of pre-filtered entries.

    Each entry dict must have keys:
      timestamp (datetime, UTC), inp, out, cw, cr, model (str)

    now must be a UTC-aware datetime.
    Returns a dict matching the usage.json output schema.
    """
    if not entries:
        return _empty_snapshot(now)

    entries = sorted(entries, key=lambda e: e["timestamp"])

    # Anchor the block to the MOST RECENT entry so the current active session is always shown.
    latest_entry = entries[-1]["timestamp"]
    block_start  = latest_entry.replace(minute=0, second=0, microsecond=0)
    block_end    = block_start + timedelta(hours=5)

    block_entries = [e for e in entries if block_start <= e["timestamp"] < block_end]
    if not block_entries:
        return _empty_snapshot(now)

    total_inp   = sum(e["inp"] for e in block_entries)
    total_out   = sum(e["out"] for e in block_entries)
    total_cost  = sum(
        price_entry(e["inp"], e["out"], e["cw"], e["cr"], e["model"])
        for e in block_entries
    )
    latest_model = block_entries[-1]["model"]

    elapsed_minutes = min((now - block_start).total_seconds() / 60, 300.0)
    burn_rate       = (total_cost / elapsed_minutes * 60) if elapsed_minutes > 0 else 0.0
    minutes_remaining = max(0, int((block_end - now).total_seconds() / 60))

    return {
        "session_cost":         round(total_cost, 6),
        "session_tokens":       total_inp + total_out,
        "session_input_tokens": total_inp,
        "session_output_tokens": total_out,
        "block_start":          block_start.isoformat(),
        "block_end":            block_end.isoformat(),
        "minutes_remaining":    minutes_remaining,
        "burn_rate_per_hour":   round(burn_rate, 6),
        "model":                latest_model,
        "scanned_at":           now.isoformat(),
    }


def _empty_snapshot(now: datetime) -> dict:
    return {
        "session_cost":          0.0,
        "session_tokens":        0,
        "session_input_tokens":  0,
        "session_output_tokens": 0,
        "block_start":           None,
        "block_end":             None,
        "minutes_remaining":     0,
        "burn_rate_per_hour":    0.0,
        "model":                 "unknown",
        "scanned_at":            now.isoformat(),
    }


def scan_once(projects_dir: str = PROJECTS_DIR) -> dict:
    """Scan all JSONL files and return the current 5-hour session block snapshot."""
    now     = datetime.now(timezone.utc)
    cutoff  = now - timedelta(hours=6)
    mtime_cutoff = (now - timedelta(days=7)).timestamp()
    seen    = set()
    entries = []

    pattern = os.path.join(projects_dir, "**", "*.jsonl")
    for path in glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(path) < mtime_cutoff:
                continue
        except OSError:
            continue

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
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
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    if ts < cutoff:
                        continue

                    key = dedup_key(rec)
                    if key in seen:
                        continue
                    seen.add(key)

                    model       = detect_model(rec)
                    inp, out, cw, cr = extract_tokens(rec)
                    entries.append({
                        "timestamp": ts,
                        "inp": inp, "out": out, "cw": cw, "cr": cr,
                        "model": model,
                    })
        except OSError:
            continue

    return compute_block(entries, now)


def write_usage_atomic(data: dict, path: str = USAGE_PATH) -> None:
    """Write data to path atomically via a .tmp intermediate file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def scanner_loop(
    projects_dir: str = PROJECTS_DIR,
    usage_path:   str = USAGE_PATH,
    interval:     int = SCAN_INTERVAL,
) -> None:
    """Infinite scan loop. Runs in a daemon thread inside hud_daemon.pyw.

    Scans every `interval` seconds. Exceptions are logged and execution continues.
    """
    while True:
        try:
            data = scan_once(projects_dir)
            write_usage_atomic(data, usage_path)
        except Exception as exc:
            _log_error(f"scan error: {exc}")
            _log_error(_traceback.format_exc())
        time.sleep(interval)
