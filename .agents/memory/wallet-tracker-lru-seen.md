---
name: wallet_tracker _seen LRU dict
description: wallet_tracker uses dict (not set) for _seen deduplication to preserve insertion order
---

# wallet_tracker._seen is a dict, not a set

## Rule
`WalletTracker._seen` must be `dict` (insertion-order LRU), NOT `set`. When trimming to MAX_SEEN, delete the **oldest** keys (front of dict), not a random half.

**Why:** `set(list(a_set)[3000:])` discards a random half because sets have no insertion order. Recently seen trade IDs could be lost → duplicate processing on next API call (C2 bug).

**How to apply:**
- `self._seen[tx] = 1` to add
- `tx in self._seen` to check (unchanged)
- Trim: `oldest = list(self._seen.keys())[:MAX_SEEN//2]; for k in oldest: del self._seen[k]`
- `db_store.wallets_load()` returns `{k: 1 for k in ...}` (dict, not set)
- On load from DB/JSON: `{k: 1 for k in seen_iterable}` to normalize

## Files changed
- `wallet_tracker.py` `_seen` init, `_record()`, `_load()`, fallback reset
- `db_store.py` `wallets_load()` return type
