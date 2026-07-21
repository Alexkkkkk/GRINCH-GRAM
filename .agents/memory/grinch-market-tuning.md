---
name: GRINCH market tuning
description: Market calibration params for GRINCH/TON DeDust (updated each analysis session)
---

## GRINCH Token Facts (updated 21.07.2026, 100×15m + 100×1h candles)
- Contract: EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL ✅ in config
- Pool: EQDpVwTQr53cwgaT_VCFsmrleg5fBvStTjMrvyvprF_ROC9Z (1% fee CPMM) ✅ in config
- GeckoTerminal uses THIS pool address (NOT EQDpVwTQrSsB… — that gives 404)
- Liquidity: ~$43.5K, Volume 24h: ~$10.6K, MCap: $819K
- Price: ~$0.000818
- ATR(14, 15m) = **3.24%** (was 4.67% on 20.07 — came down with lower vol)
- ATR(14, 1h)  = **4.87%**
- Range 53h = 43.8% ($0.000682 – $0.000982)
- Top pump: +23.6% in single 15m bar; 6 bars >5%, 2 bars >10% per 53h
- Avg vol/bar: $335; avg ret 15m: +0.24%, StdDev 3.94%
- Buy/sell 24h: 56/50 = 1.12 (slight bulls); 1h: 2/5 = 0.40 (sellers short-term)

## ATR Calibration History
| Date       | ATR(15m) | ATR(1h) | Notes |
|------------|----------|---------|-------|
| 20.07.2026 | 4.67%    | ~9-10%  | Spike day, high vol |
| 21.07.2026 | **3.24%**| **4.87%** | Normal vol day, updated params |

## Key Derived Limits (21.07.2026)
- **4×ATR(15m) = 13.0%** → minimum TRAILING_STOP_PCT (below = noise stop-outs!)
- **2×ATR(1h)  = 9.7%**  → minimum trail at each stage; SHORT_TRAIL_PCT floor
- **3×ATR(1h)  = 14.6%** → minimum TP target (covers noise + DEX fee)

## Code Parameters Updated 21.07.2026
- TRAILING_STOP_PCT: 11% → **13%** (critical: 11% was BELOW 4×ATR noise floor!)
- SHORT_TRAIL_PCT: 9% → **10%** (2×ATR(1h)=9.7%)
- PROFIT_PROTECT_DROP_PCT: 8% → **9%** (≈p75 of 1h ATR)
- PROFIT_BIAS_PCT (ai_engine): 0.025 → **0.030** (≥1×ATR(15m)=3.24%)
- config.py/ai_advisor.py/ai_engine.py comments updated with fresh ATR values

**Why:** Every time vol regime shifts, ATR-derived trail params must be rechecked.
If TRAILING_STOP_PCT < 4×ATR(15m), normal candle noise will trigger stop-outs.

## Real OHLCV Fix (21.07.2026)
- trader.py: replaced get_ohlcv() (returns _fake_ohlcv in DeDust mode) with get_real_ohlcv(tf="minute", aggregate=15) at all 5 call sites
- Fallback chain: get_real_ohlcv(15m) or get_real_ohlcv(1h) or []
- AI now trains on real GeckoTerminal 15m candles; ai_conf went from 0.0 to real values

## VPS Deploy Method (verified 21.07.2026)
- Push to GitHub via SSH deploy key (key added to repo, remote = git@github.com:...)
- SCP individual changed files → root@2.27.25.126:/opt/bot/
- docker compose restart → check health → docker logs --tail=30
- VPS cron deploy.sh runs every 3 min (git reset to origin/main + rebuild)
- Port: HOST:80 → CONTAINER:3000

## Bugs Fixed (Jul 20, 2026)
### Heiken Ashi — non-recursive (strategy.py)
**Fixed:** Proper recursive loop for ha_open.
**Why:** Non-recursive HA degrades trend signal quality on GRINCH momentum moves.

### ATR Wilder's smoothing (ai_engine.py)
**Fixed:** tr.ewm(com=13, adjust=False) instead of tr.rolling(14).mean()
**Why:** Matches TradingView/DexScreener; consistent training features.
