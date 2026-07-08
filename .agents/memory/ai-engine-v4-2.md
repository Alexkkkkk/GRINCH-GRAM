---
name: AI Engine v4.2 upgrades
description: Changes made when migrating to 2GB server — LOW_MEMORY_MODE off, regime as feature, adaptive thresholds, LightGBM enabled.
---

## Changes made for 2GB server migration

**LOW_MEMORY_MODE default**: changed from `"1"` to `"0"` in `ai_engine.py` (line ~62) and `Dockerfile`. Full 7-model ensemble now runs by default (RF+ET+GB+HGB+XGB+LGB+MLP, REPLAY_SIZE=800).

**LightGBM**: added to `requirements.txt`; was blocked only by LOW_MEMORY_MODE import guard (no other issue). Activates automatically when LOW_MEMORY_MODE=0.

**regime_enc feature** (v4.2): vectorized market regime added to `_build_features()` and `_make_dataset()` feature list. Encoding: SQUEEZE=1, VOLATILE=-1, UPTREND=2, DOWNTREND=-2, RANGING=0. **Precedence order matches `_detect_regime()`**: SQUEEZE → VOLATILE → UPTREND → DOWNTREND → RANGING. If you ever change `_detect_regime()` priorities, update `regime_enc` np.where chain to match, or model features will diverge from runtime regime gating.

**Adaptive thresholds** (v4.2): after all boosts, `_eff_buy_thr` adjusts BUY_THRESHOLD by regime (UPTREND: -0.04, BREAKOUT: -0.03, RANGING: +0.07, VOLATILE: +0.09, DOWNTREND: +0.14). `_ev_blocked` flag ensures EV-filter HOLDs are never re-enabled by the adaptive threshold HOLD→BUY path. The re-enable branch only applies to `ai_signal == "HOLD"` (not SELL).

**Why EV-filter takes priority**: EV filter is a profitability guardrail that blocks BUY when expected value ≤ 0 over confirmed trade history. Adaptive thresholds are a market-context filter. EV must always win.
