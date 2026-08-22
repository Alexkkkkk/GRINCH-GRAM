---
name: On-chain GRINCH/TON reserve price
description: How to read the accurate pool reserve price on-chain, and why raw account balance is wrong
---

# On-chain GRINCH/TON price source

The dashboard GRINCH/TON ("GRAM") price prefers REAL pool reserves read on-chain,
falling back to DexScreener priceNative. Reserves come from the pool contract's
`get_pool_data` get-method via TonCenter `runGetMethod`.

**Rule:** read reserves from `get_pool_data`, NOT from the pool's raw TON account balance.

**Why:** the pool contract's TON *account balance* (TonAPI `/v2/accounts/{pool}`) includes
~190 TON of gas/rent on top of the real reserve. Using it overstated the TON reserve and
made the price ~2.9% too high vs DeDust. `get_pool_data` returns the true reserve and matches
DeDust/DexScreener within ~0.5%. (The GRINCH jetton reserve is identical either way; only the
TON side is contaminated.)

**How to apply:**
- DeDust CPMM-v2 `get_pool_data` stack: index 9 = TON reserve (nano), index 10 = GRINCH reserve
  (nano); both 9-decimal, so price = stack[9]/stack[10]. Validate `stack[i][0]=='num'` and
  sanity-range the price (1e-6..1e-1) before trusting positional indices.
- pytoniq LiteBalancer (trust_level=1) `pool.get_reserves` FAILS here: "remote get method result
  is not provable, use run_get_method_local". So use TonCenter `runGetMethod` (server-side exec),
  which works (exit 0). Other typed SDK get-methods exit 11 on this 1%-pool — `get_pool_data` works.
- `_pool_reserves()` (raw balance) in dedust_client stays as-is for the swap engine's min-out math
  (it's tolerated there under the slippage buffer); do NOT repurpose it for display.
- price_feed.py must lazy-import dedust_client (dedust_client imports price_feed → circular); the
  on-chain reader here calls TonCenter via `requests` directly, avoiding the import entirely.
