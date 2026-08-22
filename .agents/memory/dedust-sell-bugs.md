---
name: DeDust sell silent kill bugs
description: Two bugs that caused GRINCH jetton transfer to silently fail (exit_code=27, no bounce): wrong address format for TonCenter + float overflow in nano amount.
---

# DeDust SELL: two silent kill bugs (confirmed 2026-07-01)

## Bug 1: `str(pytoniq_core.Address)` returns `"Address<EQ...>"` not `"EQ..."`

When passing `str(wallet.address)` to TonCenter v3 `/jetton/wallets`, TonCenter returns:
```
HTTP 422: "failed to decode: schema: error converting value for index 0 of owner_address"
```
because pytoniq_core's Address.__str__ wraps the result: `'Address<EQDEiPeiJf1jzU...>'`.

**Fix:** `_clean_addr_str(addr)` helper in DedustClient:
```python
s = str(addr)
if s.startswith("Address<") and s.endswith(">"):
    return s[8:-1]
return s
```
Used everywhere we pass an address to an external API (TonCenter v3, TonAPI).

**Why:** TonCenter v3 is now the PRIMARY source for GRINCH jetton wallet address and balance (replacing TonAPI which rate-limits due to running app's constant polling). Without _clean_addr_str, TonCenter always fails, falls through to SDK, SDK computes wrong address → sell goes to wrong address or balance reads as 0.

**How to apply:** Any time you use `str(pytoniq_core.Address)` as an API parameter, wrap in `_clean_addr_str()`.

---

## Bug 2: `int(float_grinch * 1e9)` can exceed on-chain nano balance → exit_code=27

Symptom: Jetton wallet receives transfer op (`0x0f8a7ea5`), runs 3002 VM steps, aborts with `exit_code=27`, `outs=0`, `aborted=True`. Since the message was sent with `bounce=False` (pytoniq wallet default), there is NO bounce — GRINCH does not come back, all gas is consumed.

Root cause: `grinch_amount=314.5304` (float from `get_balance()`), then:
```
int(314.5304 * 1e9) = 314530400000
actual on-chain balance = 314530377494
314530400000 > 314530377494 → jetton wallet: "balance exceeded" → exit_code=27
```

**Fix:** get the exact on-chain nano balance BEFORE building the transfer body, then cap:
```python
baseline_nano = await self._grinch_balance_nano(provider, wallet.address)
amount_nano = min(int(grinch_amount * (10 ** 9)), baseline_nano)
if amount_nano <= 0:
    return {"ok": False, "error": "GRINCH баланс равен 0"}
```
The sell body is then built with `amount_nano` (exact), never `int(float * 1e9)` directly.

**Why:** Floating-point `float * 1e9` rounds UP for some values (e.g., 314.5304 → 314530400000 > 314530377494). The jetton wallet's balance check is exact integer comparison. With bounce=False there is no refund signal.

**How to apply:** Any time you build a jetton transfer for "sell all GRINCH", use `baseline_nano` from the chain, not `int(requested_float * 1e9)`. The `min()` cap handles partial sells safely too.

---

## TonCenter v3 is the reliable source for GRINCH jetton wallet data

`https://toncenter.com/api/v3/jetton/wallets?owner_address={EQ...}&jetton_address={EQ...}&limit=1`
- Returns correct `address` (jetton wallet) and `balance` (exact nano)
- No rate limiting observed even when TonAPI is 429 from running app polling
- TonAPI `/v2/accounts/{owner}/jettons` is the secondary fallback
- SDK `JettonRoot.get_wallet()` is LAST RESORT ONLY — computes wrong address for GRINCH's non-standard contract when liteserver fails

**Confirmed sell gas values (2026-07-01):**
- `gas_nano = 0.25 TON` (attached to the jetton transfer message)
- `fwd_nano = 0.18 TON` (forwarded from jetton wallet → pool)
- Preflight: `needed = gas_nano + 0.01 TON` (no extra buffer; sell returns TON from pool reserves)
- Result: 314.53 GRINCH → ~0.09 TON, wallet balance increased from 0.834 → 0.924 TON
