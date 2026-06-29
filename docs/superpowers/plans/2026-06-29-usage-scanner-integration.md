# Usage Scanner Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hook-based JSONL cost scanner with a continuous background thread in the daemon that uses accurate token extraction, deduplication, model-aware pricing, and 5-hour session blocks — and update the overlay to display those session block metrics.

**Architecture:** A new pure-Python module `bin/usage_scanner.py` contains all scanning logic (no tkinter dependencies, making it unit-testable). The daemon imports it, starts it as a daemon thread on startup, and reads its output (`~/.claude/hud/usage.json`) every 150ms to update the display. The hook script is trimmed to state-tracking only.

**Tech Stack:** Python 3.11+ stdlib only (glob, json, os, threading, datetime). No new dependencies.

## Global Constraints

- All timestamps in scanner logic must use UTC (`datetime.now(timezone.utc)`).
- Atomic writes for `usage.json`: write to `.tmp` then `os.replace()`.
- Dedup key fallback order: `{mid}:{rid}` → `{mid}:noreq` → `noid:{rid}` → `syn:{ts}:{inp}:{out}`.
- Burn rate = 0.0 when elapsed_minutes == 0 (no division by zero).
- Model display: strip `"claude-"` prefix in overlay only; store full ID in JSON.
- Stale threshold: `scanned_at` older than 180 seconds → append `[stale]` to "last updated" text.
- Scanner interval: 60 seconds.
- JSONL lookback window: 6 hours (for entries), 7 days (for file mtime pre-filter).
- Only process entries where `type == "assistant"` and `isSidechain != True`.

---

## File Map

| Action | File | Purpose |
|--------|------|---------|
| **Create** | `bin/usage_scanner.py` | Pure scanner logic: token extraction, dedup, pricing, 5h block, atomic write, scan loop |
| **Create** | `tests/test_usage_scanner.py` | Unit tests for all scanner functions |
| **Modify** | `hooks/hud_hook.py` | Remove lines 37-42 (pricing), 122-241 (`_token_cost`/`_write_usage`), 299-302 (Stop call) |
| **Modify** | `bin/hud_daemon.pyw` | Import scanner, add thread startup in `main()`, update `Overlay.W/H`, `_build()`, `_refresh_usage()` |

---

## Task 1: Write failing tests for scanner logic

**Files:**
- Create: `tests/test_usage_scanner.py`

**Interfaces:**
- Produces: test suite that fails (functions not yet defined) — confirms tests are wired correctly

- [ ] **Step 1: Create `tests/__init__.py`** (empty, makes tests a package)

```python
# tests/__init__.py
```

- [ ] **Step 2: Create `tests/test_usage_scanner.py`**

```python
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
from usage_scanner import (
    extract_tokens, dedup_key, detect_model, get_pricing,
    price_entry, compute_block, write_usage_atomic, scan_once,
)


def test_extract_tokens_primary_location():
    rec = {"type": "assistant", "message": {"usage": {
        "input_tokens": 100, "output_tokens": 50,
        "cache_creation_input_tokens": 10, "cache_read_input_tokens": 5,
    }}}
    assert extract_tokens(rec) == (100, 50, 10, 5)


def test_extract_tokens_camelcase():
    rec = {"type": "assistant", "message": {"usage": {
        "inputTokens": 200, "outputTokens": 80,
        "cacheCreationInputTokens": 20, "cacheReadInputTokens": 8,
    }}}
    assert extract_tokens(rec) == (200, 80, 20, 8)


def test_extract_tokens_missing_fields_returns_zero():
    rec = {"type": "assistant", "message": {}}
    assert extract_tokens(rec) == (0, 0, 0, 0)


def test_dedup_key_both_ids():
    rec = {"message_id": "abc", "request_id": "xyz"}
    assert dedup_key(rec) == "abc:xyz"


def test_dedup_key_missing_request_id():
    rec = {"message_id": "abc"}
    assert dedup_key(rec) == "abc:noreq"


def test_dedup_key_missing_message_id():
    rec = {"request_id": "xyz"}
    assert dedup_key(rec) == "noid:xyz"


def test_dedup_key_both_missing_uses_synthetic():
    rec = {
        "timestamp": "2026-06-29T10:00:00Z",
        "message": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    }
    key = dedup_key(rec)
    assert key.startswith("syn:")
    assert "2026-06-29T10:00:00Z" in key


def test_detect_model_from_message():
    rec = {"message": {"model": "claude-sonnet-4-6"}}
    assert detect_model(rec) == "claude-sonnet-4-6"


def test_detect_model_fallback_unknown():
    rec = {"message": {}}
    assert detect_model(rec) == "unknown"


def test_get_pricing_sonnet():
    pi, po, pcw, pcr = get_pricing("claude-sonnet-4-6")
    assert pi == 3.00
    assert po == 15.00
    assert pcw == 3.75
    assert pcr == 0.30


def test_get_pricing_opus():
    pi, po, pcw, pcr = get_pricing("claude-opus-4-5")
    assert pi == 15.00
    assert po == 75.00


def test_get_pricing_unknown_returns_zero():
    rates = get_pricing("some-unknown-model")
    assert all(r == 0.0 for r in rates)


def test_price_entry_sonnet_input_only():
    cost = price_entry(1_000_000, 0, 0, 0, "claude-sonnet-4-6")
    assert abs(cost - 3.00) < 0.0001


def test_compute_block_empty_entries():
    now = datetime.now(timezone.utc)
    result = compute_block([], now)
    assert result["session_cost"] == 0.0
    assert result["session_tokens"] == 0
    assert result["burn_rate_per_hour"] == 0.0
    assert "scanned_at" in result


def test_compute_block_burn_rate_zero_when_elapsed_zero():
    # Block starts exactly at now (elapsed = 0) → burn rate must be 0
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    entries = [{"timestamp": now, "inp": 100, "out": 50, "cw": 0, "cr": 0, "model": "claude-sonnet-4-6"}]
    result = compute_block(entries, now)
    assert result["burn_rate_per_hour"] == 0.0


def test_compute_block_5h_window_correct():
    # Entries span 2 hours inside a block; verify token sums and time_remaining
    block_start = datetime(2026, 6, 29, 14, 0, 0, tzinfo=timezone.utc)
    block_end   = datetime(2026, 6, 29, 19, 0, 0, tzinfo=timezone.utc)
    now         = datetime(2026, 6, 29, 16, 30, 0, tzinfo=timezone.utc)
    entries = [
        {"timestamp": block_start + timedelta(minutes=10),
         "inp": 1000, "out": 500, "cw": 0, "cr": 0, "model": "claude-sonnet-4-6"},
        {"timestamp": block_start + timedelta(hours=2),
         "inp": 2000, "out": 1000, "cw": 0, "cr": 0, "model": "claude-sonnet-4-6"},
    ]
    result = compute_block(entries, now)
    assert result["session_input_tokens"] == 3000
    assert result["session_output_tokens"] == 1500
    assert result["minutes_remaining"] == 150   # 2h 30m left
    assert result["block_start"] == block_start.isoformat()
    assert result["block_end"]   == block_end.isoformat()


def test_compute_block_uses_most_recent_model():
    now = datetime(2026, 6, 29, 16, 30, 0, tzinfo=timezone.utc)
    entries = [
        {"timestamp": datetime(2026, 6, 29, 14, 10, 0, tzinfo=timezone.utc),
         "inp": 100, "out": 50, "cw": 0, "cr": 0, "model": "claude-opus-4-5"},
        {"timestamp": datetime(2026, 6, 29, 15, 0, 0, tzinfo=timezone.utc),
         "inp": 200, "out": 80, "cw": 0, "cr": 0, "model": "claude-sonnet-4-6"},
    ]
    result = compute_block(entries, now)
    assert result["model"] == "claude-sonnet-4-6"


def test_write_usage_atomic_creates_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "usage.json")
        data = {"session_cost": 0.042, "session_tokens": 14200}
        write_usage_atomic(data, path)
        assert os.path.exists(path)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["session_cost"] == 0.042
        assert not os.path.exists(path + ".tmp")


def test_scan_once_empty_dir_returns_zero_snapshot():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = scan_once(projects_dir=tmpdir)
        assert result["session_cost"] == 0.0
        assert result["session_tokens"] == 0
        assert "scanned_at" in result


def test_scan_once_deduplication_counts_once():
    """Same message_id:request_id in two files must only be counted once."""
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_dir = os.path.join(tmpdir, "proj")
        os.makedirs(jsonl_dir)
        now = datetime.now(timezone.utc)
        entry = json.dumps({
            "type": "assistant",
            "timestamp": now.isoformat(),
            "message_id": "msg1",
            "request_id": "req1",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 1000, "output_tokens": 500,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                },
            },
        })
        for i in range(2):
            with open(os.path.join(jsonl_dir, f"session{i}.jsonl"), "w") as f:
                f.write(entry + "\n")

        result = scan_once(projects_dir=tmpdir)
        assert result["session_input_tokens"] == 1000   # counted once, not twice
        assert result["session_output_tokens"] == 500


def test_scan_once_skips_non_assistant_entries():
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_dir = os.path.join(tmpdir, "proj")
        os.makedirs(jsonl_dir)
        now = datetime.now(timezone.utc)
        user_entry = json.dumps({
            "type": "user",
            "timestamp": now.isoformat(),
            "message": {"usage": {"input_tokens": 9999, "output_tokens": 9999}},
        })
        with open(os.path.join(jsonl_dir, "session.jsonl"), "w") as f:
            f.write(user_entry + "\n")
        result = scan_once(projects_dir=tmpdir)
        assert result["session_tokens"] == 0


def test_scan_once_skips_sidechain_entries():
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_dir = os.path.join(tmpdir, "proj")
        os.makedirs(jsonl_dir)
        now = datetime.now(timezone.utc)
        entry = json.dumps({
            "type": "assistant",
            "isSidechain": True,
            "timestamp": now.isoformat(),
            "message": {"model": "claude-sonnet-4-6", "usage": {
                "input_tokens": 9999, "output_tokens": 9999,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            }},
        })
        with open(os.path.join(jsonl_dir, "session.jsonl"), "w") as f:
            f.write(entry + "\n")
        result = scan_once(projects_dir=tmpdir)
        assert result["session_tokens"] == 0
```

- [ ] **Step 3: Run tests to verify they fail with ImportError**

```
cd c:\Users\manfr\Downloads\Claude-Status-HUD
python -m pytest tests/test_usage_scanner.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'usage_scanner'`

- [ ] **Step 4: Commit**

```bash
git add tests/__init__.py tests/test_usage_scanner.py
git commit -m "test: add failing unit tests for usage scanner logic"
```

---

## Task 2: Implement `bin/usage_scanner.py`

**Files:**
- Create: `bin/usage_scanner.py`

**Interfaces:**
- Produces:
  - `extract_tokens(rec: dict) -> tuple[int, int, int, int]` — (inp, out, cw, cr)
  - `dedup_key(rec: dict) -> str`
  - `detect_model(rec: dict) -> str`
  - `get_pricing(model: str) -> tuple[float, float, float, float]` — (input, output, cache_write, cache_read) per 1M
  - `price_entry(inp, out, cw, cr, model) -> float` — USD
  - `compute_block(entries: list[dict], now: datetime) -> dict` — usage.json payload
  - `write_usage_atomic(data: dict, path: str) -> None`
  - `scan_once(projects_dir: str) -> dict`
  - `scanner_loop(projects_dir, usage_path, interval) -> None` — infinite loop, called in thread

- [ ] **Step 1: Create `bin/usage_scanner.py`**

```python
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
import traceback
from datetime import datetime, timezone, timedelta

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

    # Round the earliest UTC timestamp down to the nearest whole hour.
    earliest     = entries[0]["timestamp"]
    block_start  = earliest.replace(minute=0, second=0, microsecond=0)
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

    elapsed_minutes = (now - block_start).total_seconds() / 60
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
        except Exception:
            traceback.print_exc()
        time.sleep(interval)
```

- [ ] **Step 2: Run the tests — expect them all to pass**

```
cd c:\Users\manfr\Downloads\Claude-Status-HUD
python -m pytest tests/test_usage_scanner.py -v
```

Expected: all tests GREEN.

- [ ] **Step 3: Commit**

```bash
git add bin/usage_scanner.py tests/test_usage_scanner.py tests/__init__.py
git commit -m "feat: add usage_scanner module with accurate token extraction and 5h block logic"
```

---

## Task 3: Add scanner thread to `bin/hud_daemon.pyw`

**Files:**
- Modify: `bin/hud_daemon.pyw`

**Interfaces:**
- Consumes: `usage_scanner.scanner_loop` (from Task 2)
- Produces: daemon starts a background daemon thread on startup that calls `scanner_loop()` every 60s

- [ ] **Step 1: Add the import at the top of `bin/hud_daemon.pyw`**

Find the existing `sys.path.insert` line (line 44) and add the import below it:

```python
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logi_led import LogiLED          # noqa: E402
from usage_scanner import scanner_loop  # noqa: E402
```

- [ ] **Step 2: Add `_start_scanner_thread()` function**

Add this function after the `log()` function (after line 127 in the original):

```python
def _start_scanner_thread() -> threading.Thread:
    """Start the background JSONL scanner as a daemon thread."""
    t = threading.Thread(target=scanner_loop, daemon=True, name="usage-scanner")
    t.start()
    log("usage scanner thread started")
    return t
```

- [ ] **Step 3: Call `_start_scanner_thread()` from `main()`**

Find the `main()` function. After `start_remote_receiver(cfg)` (line 1559 in the original), add:

```python
    start_remote_receiver(cfg)
    _start_scanner_thread()       # ADD THIS LINE

    log("HUD daemon starting")
```

- [ ] **Step 4: Commit**

```bash
git add bin/hud_daemon.pyw
git commit -m "feat: start background usage scanner thread in daemon on startup"
```

---

## Task 4: Strip JSONL scanning from `hooks/hud_hook.py`

**Files:**
- Modify: `hooks/hud_hook.py`

**Interfaces:**
- Consumes: nothing removed from public API — hook still reads stdin, writes session files
- Produces: leaner hook that only tracks state (no JSONL scanning, no usage.json writes)

- [ ] **Step 1: Remove the pricing constants and `_CTX_WINDOW` (lines 37-42)**

Delete these lines from `hooks/hud_hook.py`:

```python
# Anthropic pricing (Sonnet 4.x, per token)
_PRICE_INPUT   = 3.00e-6
_PRICE_OUTPUT  = 15.0e-6
_PRICE_CACHE_W = 3.75e-6
_PRICE_CACHE_R = 0.30e-6
_CTX_WINDOW    = 200_000   # Sonnet context window in tokens
```

- [ ] **Step 2: Remove the `import glob` line** (no longer needed after removing the scanner)

The `import glob` at line 27 can be removed since `_write_usage` used it.

- [ ] **Step 3: Remove the entire usage computation section (lines 122-241)**

Delete everything from the comment `# ---------------------------------------------------------------------------` through `_save_state(state)` at line 241:

```python
# ---------------------------------------------------------------------------
# Usage computation from JSONL files
# ---------------------------------------------------------------------------

def _token_cost(inp, out, cw, cr):
    ...

def _write_usage(data: dict, session_id: str):
    ...
```

- [ ] **Step 4: Remove the `_write_usage` call on Stop events (lines 299-302)**

Find in `main()`:

```python
        # On Stop events, recompute usage stats from JSONL files
        if event == "Stop":
            try:
                _write_usage(data, sid)
            except Exception:
                pass
```

Delete those 5 lines entirely.

- [ ] **Step 5: Update the module docstring**

Change line 19-20 in the docstring from:
```
On Stop events, also scans ~/.claude/projects/**/*.jsonl to compute
session/weekly/monthly costs and writes ~/.claude/hud/usage.json.
```
to:
```
State only — usage metrics are computed by the background scanner thread
in hud_daemon.pyw, not here.
```

- [ ] **Step 6: Verify the hook still works by running it with a test payload**

```
echo {"hook_event_name": "UserPromptSubmit", "session_id": "test123", "cwd": "C:/tmp"} | python hooks/hud_hook.py
```

Expected: exits silently (exit code 0), creates `~/.claude/hud/sessions/test123.json`.

- [ ] **Step 7: Commit**

```bash
git add hooks/hud_hook.py
git commit -m "refactor: remove JSONL scanning from hook — state tracking only"
```

---

## Task 5: Update daemon display for session block data

**Files:**
- Modify: `bin/hud_daemon.pyw`

**Interfaces:**
- Consumes:
  - `usage.json` fields: `session_cost`, `session_tokens`, `minutes_remaining`, `burn_rate_per_hour`, `model`, `scanned_at` (from Task 2 schema)
- Produces:
  - Overlay shows: Cost ($X.XXX), Tokens (14.2K), Left (3h 03m), compact footer line with burn rate / model / last updated

- [ ] **Step 1: Resize the overlay widget — change `W, H` class attributes**

Find in `Overlay` class (line 969):
```python
class Overlay:
    W, H  = 280, 148   # connected card: traffic-light + usage panel
    H_TOP = 47         # height of traffic-light section
```

Change to:
```python
class Overlay:
    W, H  = 280, 165   # connected card: traffic-light + usage panel
    H_TOP = 47         # height of traffic-light section
```

- [ ] **Step 2: Update `_build()` — change row labels and add footer text item**

Find in `_build()` (around line 1086):
```python
        row_ctrs = [self.H_TOP + 37, self.H_TOP + 56, self.H_TOP + 75]
        row_lbls = ["Session", "Weekly", "Ctx"]
```

Change to:
```python
        row_ctrs = [self.H_TOP + 37, self.H_TOP + 56, self.H_TOP + 75]
        row_lbls = ["Cost", "Tokens", "Left"]
```

Then find the end of `_build()` — after the for loop that creates `usage_vals`, add:

```python
        # Compact footer: burn rate · model · last updated
        self._usage_footer = c.create_text(
            self.W // 2, self.H_TOP + 97, anchor="center", text="",
            fill=TEXT_FAINT_C, font=self.ph_font,
        )
```

- [ ] **Step 3: Replace `_refresh_usage()` entirely**

Find the entire `_refresh_usage` method (lines 1121-1197) and replace it with:

```python
    def _refresh_usage(self):
        """Read usage.json (written by scanner thread) and update the display."""
        from datetime import datetime, timezone as _tz

        usage = _load_usage()

        if usage is None:
            for i in range(3):
                self.root.after(
                    i * 45,
                    lambda i=i: self._animate_bar_to(
                        i, self._usage_pcts[i], 0.0, "—", USG_GREEN),
                )
            self.canvas.itemconfig(self._usage_footer, text="", fill=TEXT_FAINT_C)
            return

        # ── Stale / last-updated label ──────────────────────────────────────
        scanned_at_str = usage.get("scanned_at", "")
        age_label = ""
        stale = False
        if scanned_at_str:
            try:
                scanned_at = datetime.fromisoformat(scanned_at_str)
                age_s = int((datetime.now(_tz.utc) - scanned_at).total_seconds())
                if age_s < 60:
                    age_label = f"{age_s}s ago"
                elif age_s < 3600:
                    age_label = f"{age_s // 60}m ago"
                else:
                    age_label = f"{age_s // 3600}h ago"
                if age_s > 180:
                    stale = True
                    age_label += " [stale]"
            except (ValueError, AttributeError):
                age_label = "?"

        # ── Values ──────────────────────────────────────────────────────────
        session_cost      = float(usage.get("session_cost", 0))
        session_tokens    = int(usage.get("session_tokens", 0))
        minutes_remaining = int(usage.get("minutes_remaining", 0))
        burn_rate         = float(usage.get("burn_rate_per_hour", 0))
        model             = str(usage.get("model", "unknown"))
        model_display     = model.removeprefix("claude-") if model != "unknown" else "—"

        cost_label   = f"${session_cost:.3f}"
        tokens_label = _fmt_tokens(session_tokens)
        h, m = divmod(minutes_remaining, 60)
        time_label = f"{h}h {m:02d}m" if h > 0 else f"{m}m"

        # Time-left bar: % of 5h window that has elapsed
        elapsed_min = 300 - minutes_remaining
        time_pct    = min(elapsed_min / 300 * 100, 100.0)
        time_color  = (USG_RED   if minutes_remaining < 30  else
                       USG_AMBER if minutes_remaining < 60  else USG_GREEN)

        targets = [0.0, 0.0, time_pct]
        labels  = [cost_label, tokens_label, time_label]
        colors  = [USG_GREEN, USG_GREEN, time_color]

        for i in range(3):
            old_pct = self._usage_pcts[i]
            new_pct = targets[i]
            delay   = i * 45
            self.root.after(
                delay,
                lambda i=i, old=old_pct, new=new_pct, lbl=labels[i], col=colors[i]:
                    self._animate_bar_to(i, old, new, lbl, col),
            )
            self._usage_pcts[i] = new_pct

        # ── Footer compact line ──────────────────────────────────────────────
        burn_str = f"${burn_rate:.3f}/hr" if burn_rate > 0 else "$0/hr"
        footer   = f"⚡ {burn_str}  ·  {model_display}  ·  {age_label}"
        self.canvas.itemconfig(
            self._usage_footer,
            text=footer,
            fill=USG_AMBER if stale else TEXT_FAINT_C,
        )
```

- [ ] **Step 4: Remove the now-unused `_usage_mtime` field from `__init__`**

Find in `Overlay.__init__` (around line 1015):
```python
        self._usage_mtime: float = 0.0
```

Delete that line.

- [ ] **Step 5: Start the daemon and verify the overlay renders correctly**

```
pythonw bin\hud_daemon.pyw
```

Check:
- Overlay shows "Cost", "Tokens", "Left" row labels (not "Session", "Weekly", "Ctx")
- Footer line appears at the bottom of the usage panel
- After 60s, values update from the scanner

- [ ] **Step 6: Commit**

```bash
git add bin/hud_daemon.pyw
git commit -m "feat: update overlay display to show 5h session block metrics (cost, tokens, time left)"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| Remove JSONL scanning from hook | Task 4 |
| Scanner reads `~/.claude/projects/**/*.jsonl` every 60s | Task 2 (`scan_once` + `scanner_loop`) |
| Multi-location token extraction (snake_case + camelCase) | Task 2 (`extract_tokens`) |
| Dedup with fallback key | Task 2 (`dedup_key`) |
| Model detection per entry | Task 2 (`detect_model`) |
| Model-aware pricing table | Task 2 (`get_pricing`) |
| Cost per entry using per-entry model | Task 2 (`price_entry`) |
| 5h block: round earliest UTC timestamp to hour | Task 2 (`compute_block`) |
| Burn rate = 0 when elapsed = 0 | Task 2 (`compute_block`) |
| `minutes_remaining` correct | Task 2 (`compute_block`) |
| Atomic write (`.tmp` + `os.replace`) | Task 2 (`write_usage_atomic`) |
| Scanner thread in daemon (daemon=True) | Task 3 |
| Overlay shows Cost / Tokens / Left rows | Task 5 |
| Footer: burn rate, model (truncated), last updated | Task 5 (`_refresh_usage`) |
| Stale: `[stale]` shown when scanned_at > 3min | Task 5 (`_refresh_usage`) |
| Missing usage.json → show `—` | Task 5 (`_refresh_usage`) |
| Widget resized to H=165 | Task 5 |

**Placeholder scan:** None found.

**Type consistency:**
- `compute_block` expects `entries` with keys `timestamp, inp, out, cw, cr, model` — `scan_once` builds exactly these dicts. ✓
- `scanner_loop` calls `scan_once` and `write_usage_atomic` — both defined in same module. ✓
- `_refresh_usage` reads `_load_usage()` which reads `usage.json` — same JSON written by `write_usage_atomic`. ✓
- `_usage_footer` canvas item created in `_build()`, referenced in `_refresh_usage()` — same `self._usage_footer` attribute. ✓
- `_usage_mtime` field deleted from `__init__` in Task 5, removed from `_refresh_usage` in same task — no dangling references remain. ✓
