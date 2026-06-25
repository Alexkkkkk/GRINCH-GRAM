---
name: DeDust pool address mismatch (GRINCH)
description: The bot's SDK resolves a non-existent TON/GRINCH pool; real liquidity is in a different pool the current factory does not return.
---

# DeDust pool mismatch — swaps target an empty pool

The bot resolves the swap pool via `Factory.get_pool(PoolType.VOLATILE, [native, jetton(GRINCH)])`,
which asks the **current** DeDust mainnet factory `EQBfBWT7X2BHg9tXAxzhz2aKiNTU1tpt5NsiK0uSDW_YAJ67`
its `get_pool_address` get-method.

On-chain facts (verified June 2026 via liteserver + TonCenter HTTP + TonAPI):
- Canonical [TON, GRINCH] VOLATILE pool = `EQAWWVObmIiaTfzF3tBeKg2IzCkCsLm6xh7N27dlqWuvSO2q` → **`nonexist`** on-chain, 0 balance. STABLE = `EQAn1Bxi...` (also empty). **There is NO TON/GRINCH pool with liquidity.**
- The only "liquid" GRINCH pool `EQDpVwTQr...` does NOT pair GRINCH with native TON. Its actual jetton reserves (TonAPI /accounts/{pool}/jettons) are: GRINCH + **"GRAM AT GRAMEVENT.ORG" / "GRAM AIRDROP"** scam jetton at `EQATJHRV_GEHvn0VPXn5v31CLQpixUnBxWrMTdEEoJzGNtcT` (raw `0:13247455...`). The ~10751 TON on the pool account is just gas/storage, NOT a reserve. DexScreener mislabels the quote as zero-address "GRAM".
- That GRAM AIRDROP jetton has **0 pairs, 0 liquidity, 0 TON exit** — worthless. The GRINCH/GRAM "33k liq / 62k vol" is wash-trading in scam jettons.

**Conclusion:** GRINCH has NO real TON on/off ramp. You cannot buy GRINCH with TON or sell GRINCH back to
TON on any DEX. A TON-in/TON-out profit bot for GRINCH is not viable. Repointing swaps to the GRINCH/GRAM
pool would convert real GRINCH into a worthless untradeable airdrop jetton = total loss. **Do not do it.**

**Why:** verified the pool's real jetton balances and the second asset's (non-existent) markets directly.

**How to apply:** treat GRINCH as illiquid vs TON. Do not pin/trade the GRINCH/GRAM pool with real funds.
If a real TON/GRINCH market ever appears, its address must equal the current factory's get_pool_address
output and be `active` on-chain before trading.
