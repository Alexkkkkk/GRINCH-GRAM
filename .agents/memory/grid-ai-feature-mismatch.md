---
name: GridAI DCA feature count mismatch
description: _dca_model trained on 5 features but get_dca_confidence() was calling with 7 — causes silent fallback to 25% every tick.
---

# GridAI DCA Feature Mismatch

## The Rule
`GridAI._dca_model` is trained in `_train()` using only `_make_features(atr_pct, regime)` → 5 features.  
`get_dca_confidence()` MUST NOT pass `extra=[...]` to `_make_features` or sklearn raises `X has N features, but StandardScaler is expecting 5`.

**Why:** The experience JSON stores only `{atr_pct, regime, …}` — `drawdown_pct` and `price_vs_center_pct` are runtime values, never in training data, so the model can't be trained with them.

**How to apply:** Keep `drawdown_pct` as a post-prediction multiplier (already done: `prob *= 0.6` when `drawdown_pct > 35`). Never add runtime-only context to `_make_features` unless it's also stored in `record_fill()` and used in `_train()`.

## Fix Applied (2026-07-31)
Removed `extra=[drawdown_pct, price_vs_center_pct]` from the `_make_features` call inside `get_dca_confidence()`.  
File: `grid_ai.py` line ~91.  
Deployed via `docker cp` to `bot-bot-1`. Committed to VPS `/opt/bot` git locally. Git push to origin/main pending (no remote credentials).
