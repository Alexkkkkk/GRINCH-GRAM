---
name: DeDust swap gas & settlement verification
description: Why TON→GRINCH buys bounced and how swap success must be confirmed on-chain, not on broadcast.
---

# DeDust swaps: gas floor + honest settlement

## Buy gas must cover jetton-wallet deploy
A native (TON→jetton) buy on a DeDust volatile pool needs **~0.4 TON gas**, not the
"documented" ~0.25. The pool must run the swap AND pay out the jetton through the
jetton-vault, which **deploys the buyer's jetton wallet** when it doesn't exist yet
(custodial wallet starts with 0 GRINCH → wallet undeployed). At 0.25 TON the pool
reverts with `exit_code 65535` and bounces; TON returns minus network gas, no jetton
arrives. Sell path already used 0.6 TON and worked — only buy was starved.

**Why:** confirmed via tonapi on-chain traces — pool received ~0.249 TON, reverted
65535+bounce. Decimals (9) were correct, swap amount was conveyed correctly, and pool
price matched the external feed, so it was NEITHER a decimals NOR a slippage-feed bug.
**How to apply:** keep buy gas ≥ 0.4 TON; excess gas refunds, so over-provisioning is
safe. If a buy bounces, suspect gas/wallet-deploy before touching slippage or decimals.

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
