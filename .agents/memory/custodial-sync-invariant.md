---
name: Custodial virtual-state sync invariant
description: In the custodial GRINCH platform, every virtual user-state change must follow a CONFIRMED real on-chain execution, or balances/withdrawals desync.
---

# Virtual user state must only change after confirmed real execution

**Rule:** in the custodial model, the platform makes ONE real on-chain trade that represents aggregate user
exposure; per-user accounting is virtual. Any virtual mutation (deduct TON, set/clear `grinch_held`, credit P&L)
must fire **only after** the corresponding real action actually succeeded.

**Why:** firing user `signal_callbacks` on a *failed* real order desyncs virtual balances from real funds:
- failed real BUY + virtual BUY → user charged virtual TON with no real position;
- failed real SELL + virtual SELL → user's `grinch_held` cleared and TON withdrawal unlocked for funds that were
  never liquidated on-chain.

**How to apply (the four flows that must stay in sync):**
- **Deposit credit:** TonCenter `getTransactions` returns newest-first; process the batch **oldest→newest** (sort by
  `lt` ascending) so `last_checked_lt` advances monotonically and multiple deposits in one poll window all credit.
- **BUY:** the open-trade function returns success/failure; emit the BUY callback only on a real fill.
- **Signal SELL:** close-all returns true only if positions existed and **all** really closed; a failed real sell
  leaves the position open to retry next tick (keeps `grinch_held>0`, withdrawals locked) — never fake-close.
- **SL/TP SELL:** stop-loss/take-profit closes must emit the same SELL callback once all positions are fully
  closed, otherwise users stay virtually locked forever.
- **Same-tick ordering:** never emit SELL and BUY in the same tick — after an SL/TP full close + SELL, end the tick
  early so a BUY can't open over user state the async SELL handler hasn't settled yet.

**Permanent-failure tradeoff:** a sell that fails forever keeps withdrawals locked (retried every tick). This is the
safe default (better than a fake close); add operator alerting/backoff only if it becomes an operational problem.
