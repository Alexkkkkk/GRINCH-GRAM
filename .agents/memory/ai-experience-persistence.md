---
name: AI experience persistence & self-management
description: How the bot persists AI learning across restarts and self-tunes trading params; why "AI edits code" means parameter adaptation, not source rewriting.
---

# AI experience persistence & self-management

`experience_manager.py` (singleton `experience_manager`) owns `experience.json`:
trade journal, equity/balance curve, trader stats, AI confirmed-experience export,
and adaptive `control` params. Atomic temp-file write under an RLock.

- The "AI" is a **local sklearn ensemble** (`ai_engine.py`), NOT an LLM. Its learning
  lived only in-memory (`_confirmed_X/y/w`, slot accuracy) and was **wiped on every
  restart** — that was the core gap. Fixed via `ai_engine.export_experience()` /
  `import_experience()` (numpy↔JSON, guarded by feature-dim match; refits on load).
  `import_experience` must run AFTER `pretrain` (needs `_feature_names`).

**"Программа правит код для управления" = adaptive parameter tuning, NOT literal
source rewriting.** `analyze_and_adapt()` reads loss-streak / recent net PnL /
drawdown-from-peak and mutates `Config.MIN_AI_CONFIDENCE` (stricter after losses),
`Config.TRADE_AMOUNT` (smaller on drawdown), and a drawdown **pause** flag with
hysteresis (pause ≥30% DD, resume ≤15%). Trader's BUY gate checks `exp.is_paused()`.
**Why:** letting an AI rewrite its own trading source is dangerous; param adaptation
gives the requested "self-management" safely.

`analyze_and_adapt()` also has a **profit-growth lever** (not just defense): on a
winning streak with `recent_net>0` AND low drawdown (`< DD_SHRINK_1`), `trade_amount`
grows above base (1.25× at WIN_GROW_1=3, up to GROW_CAP=1.5× at WIN_GROW_2=6),
hard-capped. **Defense always wins:** drawdown shrink (≥10%→0.60×, ≥20%→0.35×) runs
AFTER growth and overrides it; final clamp `max(base*0.25, min(amt, base*GROW_CAP))`.
Growth is always relative to `base_trade_amount`, so the set_baseline invariant holds.
**Why:** user asked "AI edits code so profit always grows" — declined literal source
rewriting (real-money risk); bounded stake-up on proven success is the safe version.

**Two invariants to keep:**
- Manual config changes (`/api/config`) must call `experience_manager.set_baseline()`
  for any changed `min_ai_confidence`/`trade_amount`, or adaptation drags the value
  back toward a stale baseline.
- `record_balance()` must **skip** the sample when GRINCH is held but the TON USD
  quote (or GRINCH price) is unavailable — otherwise equity collapses to TON-only,
  fabricating a huge drawdown and a false trading pause.

Read-only state exposed at `GET /api/experience`.

## Open positions persistence (don't sell cheaper after restart)
`open_trades` are also persisted to `experience.json` and auto-saved on EVERY
open/close (`exp.save_open_trades(self.open_trades)` in `_open_trade` and
`_close_trade`). On boot `restore_trader` reloads them into `trader.open_trades`
+ `trader.trades` so the take-profit floor (`entry × (1+net)`) keeps the real
entry price across restarts.
**Why:** open trades were in-memory only; after restart the bot forgot its buy
price and the liquidator re-anchored its ref to the *current* price, so it could
sell below cost. `experience_manager.get_cost_basis()` returns the weighted-avg
entry of open trades; the liquidator uses it as its reference instead of the live
price → target = buy×(1+sell_rise_pct), never sells cheaper than bought.
