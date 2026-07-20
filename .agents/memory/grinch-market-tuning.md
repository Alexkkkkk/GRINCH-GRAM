---
name: GRINCH market tuning
description: Bugs fixed and optimizations applied for GRINCH/TON DeDust market (Jul 2026)
---

## GRINCH Token Facts (Jul 20, 2026, updated 20.07 ~18:00 UTC)
- Contract: EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL ✅ in config
- Pool: EQDpVwTQr53cwgaT_VCFsmrleg5fBvStTjMrvyvprF_ROC9Z (1% fee CPMM) ✅ in config
- Liquidity: ~$42K, Volume 24h: ~$27K, MCap: $852K
- ATR(15m) = **4.67%** (was 2.225% — nearly 2× higher; use 4.67% for calibrations)
- ATR(1h) expected ≈ 9-10% (√4 × ATR_15m)
- Range 25h = 39.2% (min $0.000676, max $0.000941)
- 103 buys / 89 sells per 24h → slight buy bias, thin order book
- Key whale: UQBEHbcA… bought 4056 TON (5 tx) — largest active accumulator

## ATR Recalibration (20.07.2026 ~18:00 UTC)
All parameters recalibrated from ATR(15m)=2.225% → 4.67%:
- SMART_TP_TIGHT_TRAIL_PCT: 7.0% → **10.0%** (1×ATR_1h)
- TRAIL_STAGE2_PCT: 10.0% → **17.0%** (2×ATR_1h≈18%, compromise 17%)
- TRAIL_STAGE3_PCT: 7.5% → **12.0%** (1.5×ATR_1h≈13.5%, compromise 12%)
- TRAILING_STOP_PCT: 9.0% → **11.0%** (survive 1h-candle noise)
- DCA_DROP_TRIGGER_PCT: 8% → **10%** (above 1h ATR noise)
- DCA_PULLBACK_WAIT_PCT: 10% → **13%** (33% of 25h range)
- DCA_SMART_REENTRY_PULLBACK_PCT: 4% → **7%** (p75 new ATR_15m)
- DCA_ADAPTIVE_FAST_MOVE_PCT: 4% → **6%** (1.3×ATR_15m)
- PROFIT_PROTECT_DROP_PCT: 5.0% → **8.0%** (p50 1h TR new)
- SCALP_TRAIL_PCT: 4.0% → **7.0%** (0.75×ATR_1h)
- SCALP_MAX_ATR_PCT: 5.5% → **8.0%** (scalp active when ATR<8%)
- FAST_REENTRY_PULLBACK_PCT: 4.0% → **7.0%** (real pullback after TP)
**Why:** Previous calibration used ATR(15m)=2.225% which was ~2× lower than live data.
With tighter stops the bot was getting whipsawed by normal 1h-candle noise.

## Bugs Fixed (Jul 20, 2026)

### 1. Heiken Ashi — non-recursive (strategy.py)
**Was:** `ha_open = (df["open"].shift(1) + df["close"].shift(1)) / 2`
**Fixed:** Proper recursive loop: `ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2`
**Why:** Non-recursive HA uses raw OHLC, producing different candles than canonical Nishi HA. Degrades trend signal quality on GRINCH's high-momentum moves.

### 2. ATR Wilder's smoothing (ai_engine.py)
**Was:** `tr.rolling(14).mean()` (simple moving average)
**Fixed:** `tr.ewm(com=13, adjust=False).mean()` (Wilder's EMA, α=1/14)
**Why:** Simple rolling(14) over-reacts to recent ATR spikes. Wilder's smoothing matches TradingView/DexScreener and strategy.py. Consistent training features.

### 3. /api/performance auth bypass (app.py)
**Was:** Not in `_PUBLIC_EXACT` → returned 401 "Требуется вход"
**Fixed:** Added to `_PUBLIC_EXACT` set
**Why:** Read-only stats endpoint needed by dashboard widgets without login

### 4. _PRICE_MAX_STALE too long (dedust_client.py)
**Was:** 120 seconds
**Fixed:** 60 seconds
**Why:** GRINCH meme coin moves 3-8%/candle; 2-minute stale price makes min-out calculations unreliable. Price feed refreshes every ~30s, so 60s = 2× buffer is enough.

### 5. Pool impact guard (dedust_client.py)
**Added:** Warning when single trade > 3% of TON pool reserve (~1,800+ TON at current pool)
**Why:** Low-liquidity $42K pool can cause significant slippage on large single swaps.

## VPS Deploy Method
- GitHub → via Replit gitPush callback (NOT shell git)
- VPS: SCP individual files + `docker compose up -d --build`
- Port: HOST:80 → CONTAINER:3000 (NOT localhost:3000 on VPS host!)
- Verify inside container: `docker exec bot-bot-1 grep -c ...`
