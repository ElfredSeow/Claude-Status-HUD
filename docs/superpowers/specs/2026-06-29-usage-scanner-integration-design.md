# Usage Scanner Integration Design
**Date:** 2026-06-29  
**Status:** Approved

## Problem

The current HUD state tracker is inaccurate across all three displayed metrics (token counts, cost figures, context window %). Root causes:

1. **Hardcoded Sonnet 4.x pricing** — never detects actual model; wrong for Opus/Haiku/other Sonnet versions
2. **No deduplication** — JSONL entries can be double-counted
3. **Missing token field variants** — only reads snake_case fields; misses camelCase variants Claude Code sometimes writes
4. **Hook-only computation** — cost is only calculated on `Stop` events; crashes lose all data for that session
5. **No 5-hour session block grouping** — doesn't match Claude's actual billing window
6. **Simplistic context window estimate** — `input + cache_read` is not the full picture

## Goal

Replace the hook-based JSONL scanning with a continuous background scanner thread inside the daemon that uses the accurate methods from [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor).

Display 5-hour session block data (cost, tokens, time remaining, burn rate) instead of weekly/monthly cost and context window %.

## Architecture

### Responsibility Split (after)

| Component | Responsibility |
|-----------|---------------|
| `hooks/hud_hook.py` | State only: busy / idle / permission |
| `bin/hud_daemon.pyw` | State aggregation + 60s scanner thread |
| `~/.claude/hud/usage.json` | Written by scanner every 60s |
| `hud-overlay.html` | Displays session block snapshot |

### Data Flow

```
~/.claude/projects/**/*.jsonl
        ↓  every 60s (background thread in daemon)
  scanner: deduplicate → extract tokens → detect model → price → 5h block
        ↓
~/.claude/hud/usage.json
        ↓  daemon main loop
  hud-overlay.html display update
```

## Scanner Logic

### Token Extraction
Try multiple field locations per JSONL entry to handle Claude Code schema variations:

```
Primary:   message.usage.{input,output}_tokens
Fallback:  data.usage.{input,output}_tokens → data.{input,output}_tokens → 0
CamelCase: inputTokens / outputTokens / prompt_tokens / completion_tokens
Cache:     cache_creation_input_tokens / cacheCreationInputTokens
           cache_read_input_tokens    / cacheReadInputTokens
```

Only process entries where `type == "assistant"` and `isSidechain != true`.

### Deduplication
Build a `seen: set[str]` across all files. Key = `f"{message_id}:{request_id}"`. Skip any entry whose key is already in `seen`.

### Model Detection
Read `message.model` → fallback `data.model` → fallback `"unknown"` per entry. Use the model of the most recent entry in the block as the displayed model name.

### Model-Aware Pricing (per 1M tokens)

| Model family | Input | Output | Cache write | Cache read |
|---|---|---|---|---|
| claude-opus-4 / claude-opus-4-5 | $15.00 | $75.00 | $18.75 | $1.50 |
| claude-sonnet-4 / claude-sonnet-4-6 | $3.00 | $15.00 | $3.75 | $0.30 |
| claude-haiku-4 / claude-haiku-4-5 | $0.80 | $4.00 | $1.00 | $0.08 |
| claude-opus-3 | $15.00 | $75.00 | $18.75 | $1.50 |
| claude-sonnet-3-5 | $3.00 | $15.00 | $3.75 | $0.30 |
| claude-haiku-3-5 | $1.00 | $5.00 | $1.25 | $0.10 |
| unknown | $0.00 | $0.00 | $0.00 | $0.00 |

Match by checking if the model string starts with the family prefix (case-insensitive).

### 5-Hour Session Block Algorithm
1. Collect all deduplicated entries from the last 6 hours (generous window)
2. Sort by timestamp ascending
3. Find the most recent "block start": round the timestamp of the first entry in a continuous run down to the nearest hour → `block_start`
4. `block_end = block_start + timedelta(hours=5)`
5. Filter entries: `block_start <= entry.timestamp < block_end`
6. Sum: `session_tokens`, `session_cost` across filtered entries
7. Burn rate: `session_cost / elapsed_minutes * 60` (cost per hour at current pace)
8. Time remaining: `max(0, block_end - now)`

### Output Schema (`~/.claude/hud/usage.json`)

```json
{
  "session_cost": 0.042,
  "session_tokens": 14200,
  "session_input_tokens": 11000,
  "session_output_tokens": 3200,
  "block_start": "2026-06-29T14:00:00+00:00",
  "block_end":   "2026-06-29T19:00:00+00:00",
  "minutes_remaining": 183,
  "burn_rate_per_hour": 0.014,
  "model": "claude-sonnet-4-6",
  "scanned_at": "2026-06-29T16:17:00+00:00"
}
```

## Hook Changes

Remove from `hud_hook.py`:
- The entire `_compute_usage()` / `_scan_jsonl()` function (lines that scan `~/.claude/projects/`)
- All writes to `usage.json` from the hook
- Hardcoded pricing constants (`_PRICE_INPUT`, `_PRICE_OUTPUT`, etc.)
- `_CTX_WINDOW` constant and context window calculation

Keep in `hud_hook.py`:
- All event → state mapping (`decide_state()`)
- Session file writing (`~/.claude/hud/sessions/<id>.json`)

## Display Changes

### Remove
- Weekly cost
- Monthly cost
- Context window %

### Add
| Field | Example | Source |
|-------|---------|--------|
| Session Cost | `$0.042` | `usage.json.session_cost` |
| Tokens Used | `14,200` | `usage.json.session_tokens` |
| Time Remaining | `3h 03m` | `usage.json.minutes_remaining` |
| Burn Rate | `$0.014/hr` | `usage.json.burn_rate_per_hour` |
| Model | `sonnet-4-6` | `usage.json.model` (truncated) |
| Last Updated | `12s ago` | `now - usage.json.scanned_at` |

### Fallback
If `usage.json` is missing or `scanned_at` is more than 3 minutes old, display `--` for all usage fields. This makes scanner failure visible rather than silently showing stale numbers.

## Error Handling
- Malformed JSONL lines: skip and continue (log to `hud.log`)
- Missing `~/.claude/projects/` directory: write empty/zero snapshot, retry next cycle
- Scanner thread crash: log exception, restart thread after 10s backoff
- `usage.json` write failure: log and continue (don't crash daemon)

## Out of Scope
- Multi-account JSONL separation (single user setup)
- Historical session blocks beyond the current 5h window
- Limit detection (Claude rate limit messages)
- Weekly/monthly rollup totals
