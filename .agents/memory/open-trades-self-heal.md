---
name: Open-trades startup self-heal
description: How trader.py reconciles stale DB open_trades against real on-chain balance at every startup
---

# Open-trades startup self-heal

## Rule
Three cases are handled in `trader.__init__` after `restore_trader()` and DCA-restore:

1. **GRINCH=0 on wallet, open trade in DB** → position was sold externally (e.g. by liquidator between restarts) — auto-clear `self.open_trades = []` and persist empty list.
2. **GRINCH mismatch > 1%** → scale both `amount` AND `stake_ton` by `real/book` ratio so P&L % on dashboard reflects real cost basis.
3. **winning_trades > total_trades** → clamp `winning_trades = total_trades` and persist — prevents >100% winrate display.

**Why:** External Postgres host (node1.pghost.ru) has read-replica lag; one-off fix scripts sometimes read stale data even immediately after a write. In-memory state is always authoritative; self-healing at init is the only reliable way to guarantee display correctness.

**How to apply:** All three checks live in `trader.py` near line ~192–245 (search `Сверка баланса` / `Санитайз статистики`). Any new path that creates/modifies open positions must also update `stake_ton` consistently.
