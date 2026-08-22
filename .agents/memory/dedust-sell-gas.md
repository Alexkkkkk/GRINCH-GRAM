---
name: DeDust jetton sell gas
description: Why GRINCH→TON sells silently bounce and the wallet-TON preflight that prevents it
---

# DeDust GRINCH→TON sell gas

Selling jettons on DeDust sends a jetton-transfer message carrying `gas_nano` TON
that forwards `fwd_nano` to the JettonVault, which then executes the swap. Working
values (lower ones bounce): **gas_nano = 0.6 TON total, fwd_nano = 0.35 TON, deadline 600s.**

**Failure mode:** if the wallet's TON balance is below `gas_nano`, the transfer is
still broadcast ("Sent ... Failed" in TonKeeper) but the swap inside the vault has
no TON to run — GRINCH refunds and the gas is wasted. The on-chain symptom is a
sell that "Sent" but Failed, sending the jetton to the DeDust vault and bouncing back.

**Fix / rule:** preflight in `_sell_async` — read `provider.get_account_state(wallet.address).balance`
(nanotons) and abort with a clear error when `balance < gas_nano + 0.05 TON` buffer,
*before* broadcasting. Never attempt a jetton sell without confirming wallet TON ≥ gas.

**Why:** the proceeds of a sell arrive as a separate later message, so they can't
fund the swap's own gas — the wallet must already hold enough TON. A dust GRINCH
amount is doubly pointless: ~0.6 TON gas to recover fractions of a cent.
