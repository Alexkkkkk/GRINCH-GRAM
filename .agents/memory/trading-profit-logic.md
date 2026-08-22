---
name: Profit-oriented trading logic (TON/GRINCH focus)
description: Risk-management design of the trade engine and the fee-semantics gotcha
---

# Profit-oriented trade engine

The bot is focused only on the TON ecosystem: dropdown + `BASE_PRICES` are GRINCH and TON only. GRINCH demo fallback price is ~0.00027 (sub-cent), so anything price-derived must work at 8 decimals.

## Sub-cent ATR pitfall
`regime["atr"]` is `round(atr, 2)` → **always 0.0 for GRINCH**. Use `regime["atr_pct"]` (percent of price) instead, exposed from `ai_engine._detect_regime`. `trader._targets()` converts it back to a ratio for dynamic SL/TP.

## Trade decision flow (trader._tick)
Ensemble: strategy+AI must agree, OR AI alone if `conf >= AI_OVERRIDE_CONFIDENCE`. Then BUY-only quality gates block entry on: DOWNTREND regime (if `TREND_FILTER`), `RSI >= RSI_OVERBOUGHT`, `conf < MIN_AI_CONFIDENCE`, or anomaly. Position size scales 0.5×–1.0× of `TRADE_AMOUNT` by AI confidence. Dynamic ATR targets: SL=`ATR_SL_MULT×ATR`, TP=`ATR_TP_MULT×ATR` (R:R ~1:2), TP floored at `2×FEE_PCT+0.5%`. Trailing stop only raises SL and only once in profit (never lowers).

## FEE_PCT semantics + exact net floor — IMPORTANT
**Rule:** `FEE_PCT` is the **per-side** fee on BOTH entry and exit. `_close_trade` computes `fee = (entry+exit)*amount*FEE_PCT/100`. To net X% after fees you need gross `g = (X + 2*FEE_PCT)/(1 - FEE_PCT/100)`, NOT the flat `X + 2*FEE_PCT`.
**Why:** the flat `2*FEE_PCT` ignores the fee on the *exit* notional's gain (the `g/100*FEE_PCT` term), so e.g. 22% gross nets only ~19.78% at FEE_PCT=1, silently under a 20% target. `Config.required_gross_pct()` is the single source of truth — use it for every floor (`_targets` min TP, `_close_all_trades` SELL gate, ONLY_PROFIT arming floor, liquidator default).
**How to apply:** never hardcode a gross floor; call `Config.required_gross_pct()`. If you change the fee model, update both that helper and `_close_trade` together. `/api/config` POST must recompute `FEE_ROUND_TRIP` when `fee_pct` changes.

## ONLY_PROFIT_EXIT mode — design
**Rule:** when `Config.ONLY_PROFIT_EXIT` is true, `_check_stop_loss_take_profit` uses a dedicated branch (not the classic SL/TP ladder): initial `sl=0` (no downside stop ever); trade is HELD until profit ≥ `required_gross_pct()`, then the stop ARMS at `max(2%-trail-from-peak, floor_price=entry*(1+floor/100))` and never drops below the floor. Once armed (`stop_loss>0`), the exit check `price<=stop_loss` runs EVERY tick regardless of current profit — else a locked gain can be "forgotten" on a dip. AI SELL signals in `_close_all_trades` are also gated by the floor.
**Why:** user wanted "only in profit, minimum 20% net" — guarantees every automated exit ≥ target net (except unavoidable market-gap slippage). Tradeoff: a bag can be held indefinitely if price never reaches the floor (no stop-loss). Buys reserve `GAS_RESERVE_TON` + buy-gas and reject if `spendable < MIN_STAKE_TON` (no dust trades).
**How to apply:** the classic-mode trailing ladder (`TRAIL_*_AT`) only matters when ONLY_PROFIT_EXIT is false.

## settings.json overrides config.py — IMPORTANT
**Rule:** `settings.json` (loaded at config.py import + by the liquidator) OVERRIDES config.py defaults at runtime, including `liquidator.sell_rise_pct`, `TAKE_PROFIT_PCT`, `FEE_PCT`. Editing config.py alone is insufficient — update settings.json too, or the persisted value wins.
**Why:** a stale `sell_rise_pct`/`TAKE_PROFIT_PCT` in settings.json silently re-introduced a below-target floor after config.py was fixed.

## Profit target & trailing-stop coupling — IMPORTANT
**Rule:** the progressive trailing-stop stage thresholds (`TRAIL_BREAKEVEN_AT`/`STAGE2_AT`/`STAGE3_AT`/`STAGE4_AT` in config) must be scaled together with `TARGET_NET_PCT`, keeping them a monotonic ladder strictly below the TP floor (`TARGET_NET_PCT + FEE_ROUND_TRIP`).
**Why:** `_targets` floors gross TP at `TARGET_NET_PCT + 0.6%`, but the trailing stages tighten to 2% once profit passes STAGE4_AT. If the stages stay low (e.g. STAGE4_AT=20) while the target is raised (e.g. 50), any pullback near +20–25% snaps the position shut and it can never reach the new target — raising TARGET_NET_PCT alone is silently ineffective.
**How to apply:** when changing the profit target, rescale all four TRAIL_*_AT thresholds proportionally (default ladder was 5/10/15/20 for a 20% target → 12.5/25/37.5/45 for 50%).

## Demo profitability honesty
`exchange._fake_ohlcv()` is a pure random walk — no structural alpha, so positive expectancy cannot be guaranteed in demo. Risk controls limit losses only. The settings card carries a `.cfg-note` stating this; don't claim guaranteed profit.

`/api/config` POST validates/clamps all numeric inputs via the local `num(key, lo, hi)` helper (rejects NaN/non-numeric with 400).
