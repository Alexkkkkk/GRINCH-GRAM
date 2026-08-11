---
name: GridAI v5 architecture
description: All 10 v5 upgrades, FEAT_DIM change, new public API, DB table, injection points in grid_trader.
---

## FEAT_DIM: 20 → 40
Block 1 (5): ATR base; Block 2 (8): context; Block 3 (7): v4 extended; Block 4 (20): market+MTF v5.
Old experience entries get 0.0 defaults for blocks 4 — backward compatible at model level (models always retrained in-memory from experience).

## New public API on GridAI
- `set_market_context(mkt: dict)` — inject RSI/MACD/BB/vol/order_flow/pump_score each tick
- `set_mtf_context(mtf: dict)` — inject trend_4h/trend_1d
- `check_trap_exit(regime, drawdown, price, center) → dict` — trap detector; returns action=EXIT/REDUCE/HOLD

## Injection in grid_trader.py
`_inject_market_context_to_grid_ai()` called in `_tick()` after `update_regime()`. Reads last 3 DB ticks for RSI/vol/MACD, uses coin_info for order flow, derives MTF from last 20 ticks' regime history.

## PostgreSQL persistence
New table `bot_grid_experience` in db_store.py (schema already added). Functions: `grid_experience_insert`, `grid_experience_load`, `grid_experience_count`. Load order: DB first → JSON fallback → auto-migrate JSON→DB on first run.

## Profit-weighted training
`_compute_sample_weights()`: profitable fills get weight × (1 + profit_pct/5), capped at 3×. Losing fills get 0.1×. Combined with time-decay.

## Out-of-fold meta-stacking
`_train()` uses `TimeSeriesSplit(n_splits=3)` for OOF predictions before fitting meta-Ridge. Replaces in-sample stacking of v4.

## Backtest validation
`_backtest_validate()` runs TimeSeriesSplit ExtraTrees cross-val, computes R² and direction_accuracy. `self._models_validated = True` only if R²≥-0.5 AND dir_acc≥45%. Exit/Vol models only used when validated.

## Why profit-weight matters
v4 learned to predict the step that was used — even if that step caused a loss. v5 down-weights losing fills to near-zero so the model only reproduces profitable behaviors.

## Trap detector thresholds
confidence≥50 → action=REDUCE (freeze BUY); confidence≥75 → action=EXIT (emit socketio `grid_trap_alert`). Contributes: consecutive_losses, drawdown, regime, RSI+order_flow, MTF both-down, 10-trade winrate.
