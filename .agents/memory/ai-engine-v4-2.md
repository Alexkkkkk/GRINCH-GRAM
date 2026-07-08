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

## v4.4 additions (5 improvements)

**Ensemble disagreement**: `_ensemble_proba()` stores `self._last_disagreement = std(prob_up across slots)` as side-effect. In `_analyze_locked()`: if `disagreement > 0.12` → reduce `prob_up` by up to -10%. No extra predict calls (piggybacks on existing loop).

**OOD detector**: `_refit_all()` saves `_ood_mean`/`_ood_std` from `X_arr`. In `_analyze_locked()`: fraction of features > 3σ = `_ood_score`. If >25% → reduce `prob_up` up to -15%, exposed in result dict.

**Regime specialists**: `_fit_regime_specialists()` trains two lightweight RF Pipelines (80 trees, depth 6). `trend_slot` on `regime_enc >= 1` samples; `rev_slot` on `regime_enc <= 0`. Applied in `_analyze_locked()` as 20% blend. **Key invariant**: inference threshold `<= 0` for rev (not `<= -1`) must match training scope.

**Walk-forward weights**: `_update_weights_walkforward()` every 3rd retrain. 70/30 time-series split, `clone(slot.pipeline).fit(X_tr)` (no sample_weight — honest eval), then `slot.weight = 0.6*current + 0.4*wf_acc^2`.

**Confidence decay**: `_last_refit_ts` set in `_refit_all()`. In `_analyze_locked()`: model age >120 min → decay up to -10% confidence; if BUY confidence drops below `BUY_THRESHOLD*100` → flip to HOLD.

## v4.3 additions (AI engine improvements)

**Adaptive horizon weights**: `self._horizon_weights` updated per `feedback()` call. UPTREND/BREAKOUT win → boost long horizons (8,13); RANGING/VOLATILE win → boost short (3,5). After each update: **normalize to preserve sum = sum(HORIZON_WEIGHTS_DEFAULT=7.0)** to prevent saturation. `_make_dataset()` uses `list(self._horizon_weights)` instead of constant.

**Volume features** (3 new): `vol_buy_sell_ratio` (buy/sell vol ratio 10 bars), `vwap_dev_10` (10-bar rolling VWAP deviation), `vol_zscore` (z-score vs 50-bar mean). Added to `_build_features()` after CVD section; `bull_vol`/`bear_vol` are in scope there.

**Online learning**: `_new_confirms >= 1` (was 5) triggers refit. Cooldown: `_last_online_refit_ts` — not more often than 60s to prevent refit-storm.

**BUY calibration**: `_buy_calibrator` = `IsotonicRegression` fit on confirmed_X→win/loss. Applied in `_analyze_locked()` to prob_up *scalar only* after `_ensemble_proba()`. Binary (win=1/loss=0), not 3-class — matches confirmed_y={1,-1}. Activates at ≥20 confirmed trades.

**Feature compat guard**: at the **very start** of `_refit_all()` (before any np.array construction), checks confirmed_X[0] shape vs replay_X[0] shape. Clears confirmed buffer + resets `_buy_calibrator` if mismatch.

**try/except around `_refit_all()`** in `_analyze_locked()` — prevents retrain error from killing predictions.
