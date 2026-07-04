---
name: Real health check for the trading loop
description: Why /health checks trader tick staleness instead of just returning 200 OK, and how it's wired.
---

A plain `/health` that returns `200 ok` as soon as Flask is up only proves the
web process is alive — it says nothing about whether the actual trading loop
(`Trader._loop` in `trader.py`, a background daemon thread) is still ticking.
Since the process holds all critical state in memory, a hung tick (e.g. a
blockchain RPC call that never times out, a stuck lock) would leave the
dashboard/API responsive while the bot silently stops managing positions —
and a naive health check would never catch it.

**How it's wired:** `Trader` tracks `self.last_tick_ts` (updated at the end of
every loop iteration, success or failure) and `self.last_tick_ok` (True/False
based on whether the iteration raised). `/health` in `app.py` reads these and
returns:
- `200 {"trader": "stopped"}` if the trader was never started/was stopped intentionally
- `200 {"trader": "starting"}` during the initial AI pretrain (before the first tick)
- `503 {"status": "unhealthy"}` if more than 90s (loop sleeps 15s, so this is
  6 missed ticks) have passed since the last tick — this is the "loop is
  actually hung" signal
- `200 {"status": "degraded"}` if the last tick completed but raised an
  exception (still ticking, but something is wrong — check logs)

**How to apply:** if the tick loop's sleep interval changes, update the 90s
staleness threshold in `/health` to stay a multiple of it (roughly 6x). Any
new long-running background loop added later (e.g. a second trading
strategy) should follow the same last-run-timestamp pattern rather than
inventing a new health signal.
