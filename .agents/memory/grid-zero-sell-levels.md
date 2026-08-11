---
name: Grid zero SELL levels
description: GridAI can remain active with healthy ticks but rebuild zero SELL levels when all GRINCH is reserved by DCA or free inventory is below the per-level minimum
---

The grid status must be interpreted by its levels, not only by `active`: `active=true` with `sell_levels=0` and waiting BUY levels means the poller is alive but has no sell inventory. `build_grid()` skips SELL levels when free GRINCH after the DCA reserve cannot make a minimum-value order; the AI manager may log a successful rebuild with `0` SELL levels.

**Why:** A live grid can show no actions even though the loop is healthy: BUY triggers are below the current price, while SELL inventory was excluded by DCA reservation or failed the minimum-order gate.

**How to apply:** Check `sell.total`, `buy.waiting`, current price versus the nearest BUY, and the DCA-reserved/free GRINCH split before restarting or rebuilding. Do not force a SELL rebuild until the inventory ownership is verified.