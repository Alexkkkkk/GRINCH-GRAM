---
name: GRINCH market tuning
description: Bugs fixed and optimizations applied for GRINCH/TON DeDust market (Jul 2026)
---

## GRINCH Token Facts (Jul 20, 2026)
- Contract: EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL ✅ in config
- Pool: EQDpVwTQr53cwgaT_VCFsmrleg5fBvStTjMrvyvprF_ROC9Z (1% fee CPMM) ✅ in config
- Liquidity: ~$42K, Volume 24h: ~$21K, MCap: $824K
- ATH: $0.001394 (Jul 8), ATL: $0.000123 (Jun 10) — 11x range in 39 days
- Age: 39 days (meme coin). Real ATR ~3-8%/15m candle, NOT 0.6%.
- 69 buys / 54 sells / 123 txn per day → thin order book

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
