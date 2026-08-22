---
name: Liquidity guard (continuous pool monitoring)
description: Auto-pauses BUY orders on sharp liquidity drops; never touches sells; how it plugs into trader/advisor.
---

`liquidity_guard.py` polls `coin_info.market("GRINCH")["liquidity"]` every 15s, tracks a rolling peak, and flips `buys_paused=True` when liquidity drops ≥30% from peak (hysteresis resume at ≤15% drop) or falls below an absolute floor ($5000). Started at module import time (`start()` called at bottom of file), not `__main__`.

**Why:** Per the profit-only-guarantee and custodial-sync-invariant patterns already in this codebase, any new safety gate must only block opening new risk (BUY) and never block exits (SELL) — trapped funds are worse than a paused entry. A silent pool liquidity drain (rug/whale exit) can strand new buyers at a bad price if buys keep firing blindly.

**How to apply:** `trader.py._open_trade()` checks `liquidity_guard.is_buy_paused()` and returns False for `side == "buy"` only — SELL path is untouched. Status exposed via `/api/liquidity_guard` and folded into `ai_advisor._build_snapshot()` under `snap["liquidity"]` so the AI advisor is aware of pause state (read-only, does not control the guard). Dashboard card polls every 15s matching the guard's own interval.
