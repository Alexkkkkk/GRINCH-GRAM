---
name: notify_trade_closed thread safety
description: Rules for calling ai_advisor.notify_trade_closed() safely from trader.py
---

## Rules

1. **All mutations of `_last_trade_data` and `_session_stats` must happen inside `_lock`.**
   Snapshot builds (`_build_snapshot`) read these globals from a different thread (timer/advisor thread). Without the lock, reads can see a half-written dict (torn state).

2. **Snapshot counters BEFORE resetting them.**
   `self.dca_entries_count` is reset to 0 right after `_dca_sell_all` closes. Capture it as `_dca_entries_snap = self.dca_entries_count` before the reset, then pass `_dca_entries_snap` to `notify_trade_closed`. Otherwise the advisor always sees `dca_entries=0`.

3. **Emit EXACTLY ONE `notify_trade_closed` per trade close event.**
   The original `_dca_sell_all` had two calls (one for target exit, one for profit-protect path). That caused double session-stat increments and two advisor runs per close. The second call was removed — use a single `close_reason` that reflects actual trigger context.

**Why:**
Found by code review after adding rich trade-dict to notify_trade_closed. Thread safety was a regression introduced when global mutable state was added outside the existing _lock pattern.
