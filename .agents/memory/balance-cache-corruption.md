---
name: Corrupted balance snapshots (equity chart spikes)
description: TON=0 reads from TonCenter/TonAPI while GRINCH is nonzero are almost always API glitches, not real wallet state — must be rejected, not cached or recorded.
---

A wallet holding an open GRINCH position always keeps a small TON gas reserve;
it is never realistically exactly 0. When `get_shared_balance()`
(`dedust_client.py`) or `experience_manager.record_balance()` see TON=0 paired
with a nonzero GRINCH balance, that is a transient TonCenter/TonAPI read glitch,
not a real balance — accepting it corrupts the cached balance (feeding wrong
data into trading decisions) and corrupts the persisted equity curve (causes
the capital/balance chart to spike down to ~0 and immediately back up).

**Why:** Diagnosed from a real equity history showing repeating exact-duplicate
states alternating every ~3 ticks (e.g. 303.6 TON/205K GRINCH twice, then
0 TON/629K GRINCH once, repeating) — a pattern only explainable by a bad API
read being cached/recorded, not real trading activity (real swaps move
balances gradually and monotonically, not by flipping back and forth between
two exact fixed states).

**How to apply:** Both call sites now guard against this: `get_shared_balance()`
discards a fresh TON=0 reading if the previous cached TON was materially
positive (keeps old cached value instead); `record_balance()` independently
refuses to persist an equity point where `ton == 0 and grinch > 0`. Apply the
same defensive pattern to any other code path that ingests raw on-chain
balance reads for this bot.
