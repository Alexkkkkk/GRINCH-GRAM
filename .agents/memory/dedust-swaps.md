---
name: DeDust swap params cell salt formula
description: Correct salt formula for DeDust GRINCH/TON CPMM-v2 pool params cell — field1=(recip_hash*2)%2^256, not recip_hash itself.
---

# DeDust swaps: pool-native min-out + honest settlement

## DEFINITIVE ROOT CAUSE of exit 9 bounces: WRONG SALT FORMULA
Verified by decoding 5 real successful swap BOCs on the GRINCH/TON pool and comparing
with our failed swap. Pool (`Config.GRINCH_POOL_ADDRESS`) parses the params cell's
256-bit "salt" (field1) and exits **compute exit 9 (cell underflow)** when
`field1 == field2` (both equal to recip_hash). The correct formula is:

  **`salt (field1) = (recip_hash * 2) % (2**256)` — left-shift by 1 bit of recip_hash**

All 5 successful swaps confirmed this relationship exactly (field1 = field2 × 2 mod 2^256).
Our code used `salt = recip_hash` → field1 == field2 → exit 9 for our new wallet's hash.
The OLD wallet's hash happened to be a "good" value coincidentally; the new wallet's
hash `0xc488f7a2...` triggers exit 9 when used directly as field1.

**Fix (live-tested, confirmed GRINCH received):** in `_build_params_cell`:
  `salt = (recip_hash * 2) % (2 ** 256)` (NOT `salt = recip_hash`)

**Why:** the pool reads field1 and field2 as distinct values; equal values corrupt some
internal TVM cell-read path → exit 9. The left-shift formula matches ALL observed real
swap transactions across different wallets and amounts.
**How to apply:** this fix covers BOTH buy and sell (shared `_build_params_cell`).
Diagnose future pool bounces by decoding the raw_body BOC from tonapi and checking
field1 == (field2 × 2) % 2^256. The exit code from the pool tx (not wallet tx) shows the
real error: ec=9=underflow, ec=30=min-out exceeded, ec=0=success.


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

## CORRECTED ROOT CAUSE (proven on-chain): wrong swap OP, not min-out
The earlier "min-out skew" theory was WRONG and cost a full day. Hard on-chain proof:
- This GRINCH/TON pool (`Config.GRINCH_POOL_ADDRESS` = `EQDpVwTQr…OC9Z` =
  `0:e95704d0af…fd138`) is a **non-standard CPMM** (TonAPI interface `dedust_v2_cpmm`,
  exposes **only `get_pool_data`** — the SDK's `get_reserves`/`get_assets`/`get_pool_type`
  all throw exit 11 because they don't exist on this contract version). `get_pool_data`:
  `asset_x=""` (native TON), `asset_y`=GRINCH, `base_fee_bps=100` (1%).
- **Every successful swap on this pool uses op `0xa5a7cbf8`** sent **directly to the pool**
  (native TON buys go user-wallet → pool, NO vault). Exit codes observed on the pool:
  `0`=success, **`30`=min-out/slippage not met (the REAL slippage reject)**,
  **`65535`=our failure = wrong/unrecognized op**.
- Our `dedust` SDK **1.1.4** routes TON→native vault (`0:dae153a7…` "mergesort.t.me")
  op `0xea06185d`→pool op **`0x61ee542d`** (legacy). This pool does NOT understand
  `0x61ee542d` → throws **65535** and bounces. So the SDK is the wrong protocol version
  for this pool; min-out was never the cause (a lower limit still threw 65535).

**FULLY REVERSED + BYTE-EXACT swap bodies (verified: rebuilt real buy AND sell bodies
from parsed fields → BOC matched original bit-for-bit).** Builders live in
`dedust_client._build_buy_body` / `_build_sell_transfer_body` (+ `_build_limits_cell`,
`_build_params_cell`). Layout (pytoniq_core `begin_cell`):
- **ref0 (limits):** `uint32=0xc442500f`, `min_out:Coins`, `uint8=0`, `deadline:uint32`,
  `uint3=0`. (min_out = GRINCH-nano for buy, TON-nano for sell.)
- **ref1 (params):** `store_address(recipient)`, `store_address(None)` (addr_none),
  `uint16=c1`, `uint256=salt` (random, pool does NOT validate it), `uint16=0x400`,
  `uint256=recipient.hash_part`. **c1 = 0x800 for BUY, 0x801 for SELL** (direction marker).
- **BUY body** (sent native TON DIRECTLY to pool, value = swap_amount + ~0.3 TON gas):
  `uint32=0xa5a7cbf8, query_id:uint64(random), amount:Coins, ^ref0, ^ref1`.
- **SELL body** = standard jetton-transfer `op 0x0f8a7ea5` of GRINCH sent to OUR grinch
  jetton-wallet, with `destination=POOL`, `response=wallet`, `custom_payload=none`,
  `forward_ton=0.18 TON`, forward_payload (in ref) = `{uint32=0xcbc33949, ^ref0, ^ref1}`.
  NOT via the dedust jetton-vault. Attach **0.25 TON gas total** (was 0.35; pool returns
  excess). Confirmed working 2026-07-01: 314.53 GRINCH → 0.09 TON received.

**Why:** the `dedust` 1.1.4 native/jetton-vault flow (legacy pool-op `0x61ee542d`) is the
WRONG protocol for this CPMM-v2 pool → exit 65535 bounce. The hand-built `0xa5a7cbf8`
direct-to-pool flow matches real successful txs byte-for-byte.
**How to apply:** trade THIS pool only via the builders above; never the SDK vault path.
Still requires ONE funded validation trade (wallet was ~0.29 TON, untestable) — confirm
by GRINCH balance delta, not broadcast. Re-verify constants against a fresh successful
pool tx if the pool contract is ever upgraded.

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
