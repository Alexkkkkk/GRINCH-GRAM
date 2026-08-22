---
name: AI engine v4.2 and trading parameter tuning
description: QuantumBrain v4 engine upgrades (v4.2–v4.4) and active-trading parameter tuning
---

## Effective trading parameters (DB overrides config.py defaults)

DB settings take priority via `settings_store.get_section("config")` at startup.
Key active-trading params currently in DB:

| Param | DB value | Purpose |
|-------|----------|---------|
| DCA_TARGET_PROFIT_PCT | 5.0 | Take profit target (very aggressive) |
| DCA_CASCADE_LEVEL1_PCT | 5.0 | Sell 50% at this % profit |
| DCA_CASCADE_LEVEL2_PCT | 12.0 | Sell remaining at this % profit |
| DCA_DROP_TRIGGER_PCT | 9.0 | Buy more after this % drop |
| DCA_REENTRY_COOLDOWN_SEC | 300 | Min seconds between DCA entries |
| AI_AUTONOMOUS_MIN_CONF | 36.0 | Min AI conf for autonomous trades |
| AI_FULL_RIGHTS_MIN_CONF | 0.0 | AI always has full rights |
| MIN_AI_CONFIDENCE | 50.0 | Entry gate |
| DCA_SMART_REENTRY_MIN_AI_CONF | 55.0 | |
| DCA_SMART_REENTRY_PULLBACK_PCT | 6.0 | |
| FAST_REENTRY_MIN_CONF | 55.0 | |
| FAST_REENTRY_PULLBACK_PCT | 4.0 | |
| SCALP_MIN_AI_CONF | 52.0 | |
| SCALP_TARGET_NET_PCT | 3.0 | |
| SCALP_TP_PCT | 5.0 | |

**Why:** "активнее торговлю в плюс" request — more entries, faster profit-taking.
**How to apply:** Always check DB values via `settings_store.load_settings()["config"]` before assuming config.py defaults are active.

## Scalp TP floor fix (trader.py)

`_targets(is_scalp=False)` — when `is_scalp=True`, uses `SCALP_TARGET_NET_PCT` as net floor
instead of `TARGET_NET_PCT` (13%). Without this, scalp exits require 13%+ gross, defeating scalping.
`is_scalp=True` is set when `_tp_override is not None` (scalp always passes tp_override).

## DCA cascade vs target alignment invariant

`DCA_CASCADE_LEVEL1_PCT` must be ≤ `DCA_TARGET_PROFIT_PCT` or cascade bypasses the target entirely
(cascade runs in the `if DCA_CASCADE_ENABLED` branch, target in `else`).
Current: both at 5%. If you change DCA_TARGET_PROFIT_PCT, update cascade levels too.

## DCA reentry cooldown

`DCA_REENTRY_COOLDOWN_SEC=300` + `_last_dca_entry_ts` in trader.__init__.
Prevents stacking multiple DCA buys in a single volatile tick when drop/conf thresholds are low.

## v4.4 additions (5 improvements)

**Ensemble disagreement**: `_ensemble_proba()` stores `self._last_disagreement = std(prob_up across slots)` as side-effect. In `_analyze_locked()`: if `disagreement > 0.12` → reduce `prob_up` by up to -10%. No extra predict calls (piggybacks on existing loop).

**OOD detector**: `_refit_all()` saves `_ood_mean`/`_ood_std` from `X_arr`. In `_analyze_locked()`: fraction of features > 3σ = `_ood_score`. If >25% → reduce `prob_up` up to -15%, exposed in result dict.

**Regime specialists**: `_fit_regime_specialists()` trains two lightweight RF Pipelines (80 trees, depth 6). `trend_slot` on `regime_enc >= 1` samples; `rev_slot` on `regime_enc <= 0`. Applied in `_analyze_locked()` as 20% blend. **Key invariant**: inference threshold `<= 0` for rev (not `<= -1`) must match training scope.

**Walk-forward weights**: `_update_weights_walkforward()` every 3rd retrain. 70/30 time-series split, `clone(slot.pipeline).fit(X_tr)` (no sample_weight — honest eval), then `slot.weight = 0.6*current + 0.4*wf_acc^2`.

**Confidence decay**: `_last_refit_ts` set in `_refit_all()`. In `_analyze_locked()`: model age >120 min → decay up to -10% confidence; if BUY confidence drops below `BUY_THRESHOLD*100` → flip to HOLD.

## v4.3 additions (AI engine improvements)

- **Online learning** — refit triggers on 1 confirmed trade (was 5), 60s cooldown
- **Adaptive horizon weights** — `[1.0, 1.5, 2.0, 2.5]` update per-trade by regime, normalized
- **3 volume features** — `vol_buy_sell_ratio`, `vwap_dev_10`, `vol_zscore` (~69 features total)
- **BUY calibration** — `IsotonicRegression` maps `prob_up` to real win-rate when ≥20 trades
- **Feature mismatch protection** — check at start of `_refit_all()`, clears buffer on size mismatch

## v4.2 additions (2GB server: LOW_MEMORY_MODE=0)

- `regime_enc` feature (numeric encoding of regime label) added to feature set
- Adaptive BUY thresholds by regime: UPTREND -0.04, BREAKOUT -0.03, SQUEEZE +0.04, RANGING +0.07, TRANSITION +0.06, VOLATILE +0.09, DOWNTREND +0.14
- EV-filter: blocks BUY when expected value ≤ 0, has priority over adaptive thresholds
- BUY_THRESHOLD: 0.46 → 0.43 (active trading mode)
