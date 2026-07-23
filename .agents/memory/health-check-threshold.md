---
name: Health check threshold
description: /health endpoint threshold and tick staleness logic in app.py
---

# Health Check Threshold

## Rule
`/health` reports "unhealthy" when `last_tick_ts` age > **180 seconds** (changed from 90s on 2026-07-23).

**Why:** TonCenter/TonAPI calls inside `_tick_dca()` legitimately take 60–120s (pool data, balance queries). The 90s threshold fired false "stalled" alarms even when the loop was healthy and completing normally.

**How to apply:** Do not lower the threshold back to 90s without first profiling what is blocking the tick. If ticks regularly exceed 120s, investigate `_check_large_sell_dca` / `_get_balance_cached` for missing timeouts.

## Related
- `app.py` line ~874: `if age > 180:`
- `trader.py` `_loop`: exponential backoff added for consecutive errors (max 16s, M3 fix)
