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
    # Block is anchored to the MOST RECENT entry's hour (Bug 1 fix).
    # Both entries fall within the same hour (16:xx), so block_start=16:00, block_end=21:00.
    block_start = datetime(2026, 6, 29, 16, 0, 0, tzinfo=timezone.utc)
    block_end   = datetime(2026, 6, 29, 21, 0, 0, tzinfo=timezone.utc)
    now         = datetime(2026, 6, 29, 16, 30, 0, tzinfo=timezone.utc)
    entries = [
        {"timestamp": block_start + timedelta(minutes=10),
         "inp": 1000, "out": 500, "cw": 0, "cr": 0, "model": "claude-sonnet-4-6"},
        {"timestamp": block_start + timedelta(minutes=20),
         "inp": 2000, "out": 1000, "cw": 0, "cr": 0, "model": "claude-sonnet-4-6"},
    ]
    result = compute_block(entries, now)
    assert result["session_input_tokens"] == 3000
    assert result["session_output_tokens"] == 1500
    assert result["minutes_remaining"] == 270   # 4h 30m left (16:30 -> 21:00)
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
