---
name: DeDust pool address mismatch (GRINCH)
description: The bot's SDK resolves a non-existent TON/GRINCH pool; real liquidity is in a different pool the current factory does not return.
---

# DeDust pool mismatch — swaps target an empty pool

The bot resolves the swap pool via `Factory.get_pool(PoolType.VOLATILE, [native, jetton(GRINCH)])`,
which asks the **current** DeDust mainnet factory `EQBfBWT7X2BHg9tXAxzhz2aKiNTU1tpt5NsiK0uSDW_YAJ67`
its `get_pool_address` get-method.

On-chain facts (verified June 2026 via two independent reliable transports — pytoniq liteserver AND
TonCenter HTTP, both exit=0, agree):
- Canonical [TON, GRINCH] VOLATILE pool = `EQAWWVObmIiaTfzF3tBeKg2IzCkCsLm6xh7N27dlqWuvSO2q` → **`nonexist`** on-chain (TonAPI), 0 balance. Asset order does not matter; STABLE = `EQAn1Bxi...` (also empty).
- The REAL liquid TON/GRINCH pool that people actually trade = `EQDpVwTQr53cwgaT_VCFsmrleg5fBvStTjMrvyvprF_ROC9Z` → `active`, interface `dedust_v2_cpmm`, ~10751 TON balance, ~$33.6k liquidity, ~$62k/24h volume (DexScreener + TonAPI). DexScreener shows its quote as the zero address `EQAAA...M9c` ("Zero Address") which is DeDust's native-TON sentinel (the 10751 TON balance confirms the native side is real TON).

**Conclusion:** the real pool is NOT the one the current factory computes — it was deployed under a
different/legacy factory, so `Factory.get_pool` will never reach it. The bot points at an empty pool,
so swaps bounce/refund. This is a deeper cause of "bounced sells" than the gas shortage.

**Why:** DeDust pool addresses are deterministic per (factory, type, sorted assets). A liquid pool whose
address differs from the current factory's `get_pool_address` output means it lives on another factory.

**How to apply / fix:** do NOT trust `Factory.get_pool` for GRINCH. Pin the real pool address
`EQDpVwTQr53cwgaT_VCFsmrleg5fBvStTjMrvyvprF_ROC9Z` (and its matching native/jetton vaults), or resolve the
pool from DexScreener/DeDust indexer instead of the factory get-method. Verify any pinned address is
`active` with `dedust_v2_cpmm` before trading real funds.
