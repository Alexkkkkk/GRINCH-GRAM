---
name: Real-time price feed (free APIs)
description: How the bot sources real crypto prices and the sub-cent rounding pitfall
---

# Real-time price feed

The bot fetches real prices via free, no-key public APIs, cached by TTL:
- **CoinGecko** `simple/price?ids=<id>&vs_currencies=usd` for TON and major coins (id map in `price_feed.py`).
- **DexScreener** `latest/dex/tokens/<address>` for GRINCH (a TON jetton). Use the dedicated `Config.GRINCH_TOKEN_ADDRESS`, NOT `TON_WALLET` — they happen to share a value but are semantically different (wallet vs token contract). DexScreener fallback is restricted to base=="GRINCH"; unknown coins return None so `exchange._base_price()` falls back to static `BASE_PRICES`.

**Why:** mixing wallet and token address causes unknown symbols to silently get GRINCH/wallet-derived prices.

## Sub-cent rounding pitfall
**Rule:** any price/indicator display must use *adaptive* decimals based on magnitude, never a fixed 2.
**Why:** GRINCH trades ~$0.00027; `round(x, 2)` collapses price/EMA/BB to 0.0. Bug lived in `strategy.py analyze()`.
**How to apply:** `_pdigits()` in strategy.py and matching `fmtPrice()`/`_round()` ladders (>=100→2, >=1→4, >=0.01→6, else→8) in exchange.py and static/js/app.js — keep all three in sync.
