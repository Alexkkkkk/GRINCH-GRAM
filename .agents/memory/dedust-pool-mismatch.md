---
name: DeDust pool address mismatch (GRINCH)
description: The DeDust factory computes a non-existent TON/GRINCH pool address; real liquidity lives in a pool with a non-standard 1% fee that must be pinned by address.
---

# DeDust pool mismatch — factory returns an empty pool, real pool must be pinned

The bot used to resolve the swap pool via `Factory.get_pool(PoolType.VOLATILE, [native, jetton(GRINCH)])`
against the mainnet factory `EQBfBWT7X2BHg9tXAxzhz2aKiNTU1tpt5NsiK0uSDW_YAJ67`. That returns the **canonical**
pool address for the standard fee tier, which is `nonexist` on-chain (0 balance) — so swaps silently bounced.

**The real pool is `EQDpVwTQr53cwgaT_VCFsmrleg5fBvStTjMrvyvprF_ROC9Z`.** It was created with a **non-standard 1% fee
(CPMM v2)**, so its address does NOT match what the factory computes for the default tier. Fix: pin it via
`Config.GRINCH_POOL_ADDRESS` and build `Pool.create_from_address(CoreAddress(addr))` in `_get_pool` instead of
`Factory.get_pool`.

## The pool IS GRINCH / native-TON (despite the "GRAM" label)

The counter-asset's on-chain address is `EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c` — the all-zeroes
**native-TON sentinel** DeDust uses for Toncoin. Confirmed identical across two independent indexers (June 2026):
GeckoTerminal labels it `TON / Toncoin`; DexScreener labels it `GRAM / Gram`; the DeDust web UI shows `GRINCH/GRAM`.
**Same zero-address, different display names** — the asset is native TON. Price math agrees: ~10.62K units ×
~$1.585 = ~$16.8K ≈ TON price. The earlier "GRAM is a scam jetton / 100-token dust" conclusion was WRONG and is retracted.

**Why this matters for routing:** because the counter-asset is native TON, swaps route through the **native vault**
(buy: TON→native vault→pool) and the **GRINCH jetton vault** (sell: GRINCH→jetton vault→pool). The current code is correct.

## SDK get-methods fail (exit 11) on this pool — expected, not a blocker

The installed `dedust` SDK is older than this CPMM-v2 1%-fee contract, so typed get-methods
(`get_assets`/`get_reserves`/`estimate_swap_out`) return exit code 11 on-chain (confirmed via liteserver, TonCenter,
TonAPI — don't keep retrying them). Swap **execution** does not need them; price comes from the external feed
(CoinGecko/DexScreener/GeckoTerminal). Slippage protection is now wired: min-out is derived from external USD
prices (TON & GRINCH), buffered by `Config.SLIPPAGE_PCT` (clamped 0.1..50, default 5), passed as the vaults'
`create_swap_payload(limit=...)`. Trades **abort** (no funds sent) if a fresh price is unavailable (price feed
`get(base, max_stale=120)` rejects indefinitely-stale cache). **Why:** the pool has no usable on-chain estimate, so
an external-price min-out is the only slippage guard — never send `limit=0` with real funds.

**How to apply:** keep the pool pinned by address; trust the zero-address = native TON; use native vault for buys,
GRINCH jetton vault for sells; ignore exit-11 get-method failures; validate end-to-end with a small (1 TON) test trade
(a misrouted native swap bounces back, so it's safe to probe).
