---
name: VPS code and data rollback
description: Full VPS rollback must treat repository code and persistent trading data as separate recovery targets.
---

When rolling back the VPS, restore repository code to the requested deployment point but preserve persistent trading data unless there is a timestamp-matched data snapshot. A historical code commit does not imply a matching database, wallet, or Grid state snapshot.

**Why:** Restoring an approximate old data backup can remove valid trades, balances, and later Grid learning; code rollback is reversible when the current data is archived separately.

**How to apply:** Archive `/app/data` and the current repository before switching commits, disable the automatic deploy watcher during the rollback, and verify the running GridAI version, Grid tick freshness, health, and data counts afterward.