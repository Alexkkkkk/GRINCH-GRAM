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

## FEE_PCT semantics — IMPORTANT
**Rule:** `FEE_PCT` is the **per-side** fee, charged on BOTH entry and exit. Full round-trip cost = `2×FEE_PCT`.
**Why:** `_close_trade` computes `fee = (entry+exit)*amount*FEE_PCT/100` (both sides), and `_targets` floors TP at `2×FEE_PCT+0.5`. These are only self-consistent under the per-side reading. An earlier comment wrongly called it "full cycle", which would have double-counted.
**How to apply:** if you ever change the fee model, update BOTH `_close_trade` and `_targets` together, and the config comment.

## Profit target & trailing-stop coupling — IMPORTANT
**Rule:** the progressive trailing-stop stage thresholds (`TRAIL_BREAKEVEN_AT`/`STAGE2_AT`/`STAGE3_AT`/`STAGE4_AT` in config) must be scaled together with `TARGET_NET_PCT`, keeping them a monotonic ladder strictly below the TP floor (`TARGET_NET_PCT + FEE_ROUND_TRIP`).
**Why:** `_targets` floors gross TP at `TARGET_NET_PCT + 0.6%`, but the trailing stages tighten to 2% once profit passes STAGE4_AT. If the stages stay low (e.g. STAGE4_AT=20) while the target is raised (e.g. 50), any pullback near +20–25% snaps the position shut and it can never reach the new target — raising TARGET_NET_PCT alone is silently ineffective.
**How to apply:** when changing the profit target, rescale all four TRAIL_*_AT thresholds proportionally (default ladder was 5/10/15/20 for a 20% target → 12.5/25/37.5/45 for 50%).

## Demo profitability honesty
`exchange._fake_ohlcv()` is a pure random walk — no structural alpha, so positive expectancy cannot be guaranteed in demo. Risk controls limit losses only. The settings card carries a `.cfg-note` stating this; don't claim guaranteed profit.

`/api/config` POST validates/clamps all numeric inputs via the local `num(key, lo, hi)` helper (rejects NaN/non-numeric with 400).
