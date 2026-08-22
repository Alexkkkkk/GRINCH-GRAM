---
name: Wallet & smart-money monitoring
description: How pool-wide wallet tracking + the smart-money AI signal work, and the two correctness traps.
---

The bot monitors every wallet trading GRINCH in the pool (Config.GRINCH_POOL_ADDRESS)
via GeckoTerminal pool `/trades`, aggregates per-wallet stats, and derives a
"smart money" signal that nudges (only) BUY decisions.

**Dedup key = the trade `id`, NOT `tx_hash`.**
**Why:** one transaction can emit multiple trade legs (multi-hop). Deduping on
`tx_hash` silently drops legitimate trades/addresses. GeckoTerminal trade objects
carry a unique top-level `id` like `ton_<block>_<txhash>_<logindex>_<ts>`.
**How to apply:** any new pool-trade ingestion must key on `t["id"]` (fallback to a
composite of tx_hash+block+amounts), never bare tx_hash.

**Realized PnL must use matched volume = min(grinch_bought, grinch_sold).**
**Why:** the free API gives only recent trades, so history is "forward-only" and a
wallet's observed sells often exceed its observed buys. `grinch_sold*(avg_sell-avg_buy)`
then inflates profit and misclassifies wallets as "smart", distorting the AI filter.
**How to apply:** PnL = matched*(avg_sell-avg_buy); a wallet is "smart" only with both
buys and sells observed and matched PnL > 0.

**Smart-money signal is BUY-only and bounded.** score in [-1,1] = net TON flow of
profitable wallets in last 1h (fallback: overall flow). In trader._tick: block entry
when score ≤ SMART_MONEY_BLOCK (unless hard_override); soften MIN_AI_CONFIDENCE
(floored at SMART_MONEY_MIN_FLOOR) when score ≥ SMART_MONEY_BOOST_AT. **Never touches
SELL or the never-sell-at-loss / only-profit-exit logic.**

**Honest limitation:** free GeckoTerminal returns only ~recent pool trades, not full
history. Full per-wallet picture accrues forward over time; persisted in wallets.json
(atomic .tmp + os.replace, survives restart). Don't revert wallets.json — runtime state.
Heavy manual polling hits HTTP 429; the in-app 30s poller is fine.
