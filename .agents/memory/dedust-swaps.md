---
name: DeDust swap min-out & settlement verification
description: Why TON→GRINCH buys bounced (min-out from USD cross-rate, not gas) and how swap success must be confirmed on-chain.
---

# DeDust swaps: pool-native min-out + honest settlement

## Buys bounced because min-out came from a USD cross-rate, NOT the pool
Every buy reverted (`exit_code 65535` + bounce): TON left the wallet, no GRINCH
arrived. Root cause was the slippage `min_out`, computed as
`expected_grinch = ton_amount * ton_usd / grinch_usd` — a **cross-rate of two
unrelated USD sources** (TON from CoinGecko, GRINCH `priceUsd` from DexScreener).
That ratio runs **~6% richer** than this specific **1%-fee** DeDust pool's real
TON↔GRINCH price, so even a 5% slippage buffer produced a `min_out` the pool could
never deliver (e.g. demanded 3101.73 GRINCH for 0.942 TON; pool delivers ~3045 after
the 1% fee) → pool reverts every buy.

**Fix:** derive min-out from the **pool's own TON-denominated price**:
`price_feed.get_grinch_ton_price()` reads DexScreener `priceNative` for the pinned
pool (`Config.GRINCH_POOL_ADDRESS`); buy `expected = ton/price`, sell
`expected = grinch*price`, then apply `SLIPPAGE_PCT` (5). USD cross-rate is fallback
only, and the fallback must filter to a **TON-quoted** pair or return None (else it
prices off a foreign market). After fix: min_out ~2899, pool delivers ~3021 → passes.

**Why:** on-chain `get_reserves` returns **exit 11** ("not provable" via liteserver),
so reserves can't be read on-chain and we can't quote the pool directly — but a
pool-native price is available off-chain via DexScreener `priceNative` /
GeckoTerminal `base_token_price_quote_token`. Decimals (9) and the swap amount were
always correct; gas was NOT the cause — a successful buy on the same wallet/native
vault used only ~0.25 TON. Keep buy gas ~0.4 TON as harmless margin (excess refunds),
but if a buy bounces, suspect **min-out feed-vs-pool skew first**, not gas/decimals.
**How to apply:** any min-out for a specific pool must use that pool's native ratio;
never cross-multiply prices from two different USD feeds for execution limits.

## "ok" must mean settled, not broadcast
`wallet.transfer` only **broadcasts**; the swap can still bounce afterward. Returning
`ok:True` right after transfer (and `force_sell_now` returning `ok:True`
unconditionally) is how the bot lied ("ты несвапнула а сказала что свапнула").
Confirm by **polling the GRINCH balance** after transfer (same open provider, ~7s
interval, ~75s cap) and only report success if it increased (buy) / decreased (sell)
by ≥50% of expected; else `ok:False` with an honest RU error.

**Why:** real-money bot; over-claiming success is the worst failure mode.
**How to apply:** balance-delta settlement is **wallet-global**, so swaps MUST be
serialized (a single `threading.Lock` around buy/sell) or concurrent trades corrupt
the check. Known remaining limits (not yet fixed): a genuinely successful swap that
settles >75s reads as failed (under-claims, the safe direction); manual sell blocks
its Flask request up to ~75s. A more robust future fix is tx/trace-based confirmation
instead of balance polling.
