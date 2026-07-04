---
name: AI inference caching and smarter retrain gating
description: How QuantumBrain's 7-model ensemble avoids wasted CPU on unchanged candle data
---

`AIEngine.analyze()` in `ai_engine.py` used to fully recompute 80+ features and run
all 7 models (RF/ET/GB/HGB/XGB/LGB/MLP) + regime/pattern/momentum/breakout/pump
detectors on every trader tick (15s), even when the underlying OHLCV candles had not
changed (candles refresh on a slower cadence via the exchange cache). It also fully
refit all 7 models (`_refit_all`) every `RETRAIN_EVERY` ticks regardless of whether new
data had actually arrived.

**Why:** retraining a 400-tree XGB + 500-iter LGB + 300-tree RF ensemble repeatedly on
the exact same dataset is pure wasted CPU — it produces an identical model. Likewise,
returning the identical prediction dict from the identical input is safe to cache.

**Fix (deterministic, zero behavior change on trading decisions):**
- Added a result cache in `AIEngine` keyed on a `(len(ohlcv), last_bar_timestamp,
  last_bar_close)` fingerprint (`_last_candle_key` / `_last_result` /
  `_last_result_ts`), short TTL (`ANALYZE_CACHE_TTL`). If the fingerprint and TTL match
  and there's no pending confirmed-trade retrain (`_new_confirms < 5`), `analyze()`
  returns the cached dict instantly instead of recomputing.
- `should_retrain` now additionally requires `data_changed` (candle fingerprint differs
  from the fingerprint at last retrain) — so it never refits twice on the same candle
  set, while confirmed-trade-triggered retrains (`_new_confirms >= 5`) still fire
  immediately as before.

**How to apply:** if adding new per-tick heavy computation to `ai_engine.py` or
`strategy.py`, gate it the same way — check whether the input data actually changed
before redoing expensive work, rather than gating purely on a tick/time counter.
