---
name: Coin info + DEX trade feed (free APIs)
description: How GRINCH market stats and the live DEX trades feed are sourced and the orientation pitfall
---

# Coin info card + DEX trades feed

Free, no-key sources used by `coin_info.py`:
- **DexScreener** `dex/tokens/<addr>` → market stats (price, 24h change, volume, liquidity, mcap, 24h buy/sell counts, logo) **and** the most-liquid pool address. Filter pairs to where `baseToken.address == GRINCH addr` before picking by liquidity — the token can appear as quote in unrelated pools.
- **GeckoTerminal** `networks/ton/pools/<pool>/trades` → individual swaps (up to ~130).
- **CoinGecko** `coins/markets` → stats for TON and other major coins (no per-trade feed).

The per-trade feed is GRINCH-only (a jetton with one canonical pool); major coins have no single pool.

## Multi-exchange aggregation (all TON exchanges)
`coin_info.exchanges(base)` builds a cross-exchange price view:
- **GRINCH (jetton):** DexScreener `dex/tokens/<addr>` — each pair = one TON DEX (DeDust, STON.fi). Filter to `baseToken.address == GRINCH addr`.
- **TON / majors (CEX+DEX):** CoinGecko `coins/<id>/tickers` — ~100 tickers across Binance/OKX/Bybit/etc. Use `converted_last.usd` (price) and `converted_volume.usd` (volume).

**Dedup pitfall:** an exchange appears in many tickers (different quote pairs). Keep **one row per exchange, the highest-volume ticker** — build a `name -> best_by_volume` map, THEN sort. Deduping by first-seen during raw iteration keeps a low-volume outlier pair, which inflated TON spread to ~1.3% and gave a false "РАСХОЖДЕНИЕ"; correct dedup gives ~0% / "КОНСЕНСУС".

`_aggregate()`: liquidity-weighted (DEX) or volume-weighted (CEX) avg price; spread% = (max-min)/min*100; AI signal thresholds spread ≥1.5→АРБИТРАЖ, ≥0.4→РАСХОЖДЕНИЕ, else КОНСЕНСУС. Endpoint `/api/coin/exchanges`.

## Trade-side orientation pitfall
**Rule:** decide buy/sell by matching the GRINCH **token address** against each trade's `from_token_address`/`to_token_address` — NOT by GeckoTerminal's `kind` field or assumed token position.
**Why:** `kind` + positional mapping (buy→to_token, sell→from_token) only works when GRINCH is the pool's base token; a differently-oriented pool silently mislabels sides, amounts and prices. With address matching: GRINCH as `to` = buy (GRINCH received), GRINCH as `from` = sell.
**How to apply:** in `_fetch_trades`; keep a positional fallback only for when neither address matches. GRINCH amount/price come from the GRINCH side; the other side is the TON/quote amount.
