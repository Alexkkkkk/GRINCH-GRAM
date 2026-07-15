---
name: Data fetch speed
description: Current polling/cache intervals across the bot's data-fetch pipeline, and safe limits when asked to make it faster again.
---

## Current intervals (as of 2026-07-15, after second speed pass)
- `trader.py` main tick loop: `_loop_stop_event.wait(timeout=8)` (was 30 → 15 → 8s)
- `wallet_manager.py` `POLL_SEC = 10` (was 30s)
- `wallet_tracker.py` `POLL_SEC = 5` (was 30 → 15 → 8 → 5s)
- `exchange.py` `_OHLCV_TTL = 25`, `_OHLCV_BACKOFF = 15` (was 180/120 → 60/45 → 25/15)
- `price_feed.py` cache `ttl=4`, prefetch loop `wait(timeout=3)` (was 6/5s)
- `ai_engine.py` `ANALYZE_CACHE_TTL = 7` (was 12s)
- `app.py` `_CANDLES_CACHE_TTL = 2` (was 4s)
- Frontend polling: `templates/index.html` chart `refresh` 3000ms, `advLoadStatus` 5000ms; `templates/user_dash.html` `loadStatus` 6000ms.

**Why:** user asked twice ("сделай насколько сможешь получение данных быстрей") to minimize latency between real market state and what the bot/dashboard sees. Each round tightened every stage of the pipeline (trade tick → external API polling → cache TTLs → dashboard refresh) rather than just one knob.

**How to apply:** if asked again, the next safe squeeze points are price_feed prefetch (currently 3s, bounded by CoinGecko/DexScreener free-tier rate limits — don't go below ~2s) and GeckoTerminal OHLCV TTL (currently 25s; the underlying candle granularity is minute/15-aggregate so going much below ~15s buys nothing). Always mirror any change made in the Replit repo to the VPS's live checkout too (see vps-git-drift.md) — they are NOT kept in sync by the deploy pipeline.
