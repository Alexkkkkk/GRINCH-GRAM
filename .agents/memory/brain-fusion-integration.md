---
name: BrainFusion integration
description: Central consensus brain unifying AI+TA+LLM; key pitfalls when integrating into trader.py and ai_advisor.py
---

## Rule
`brain_fusion.py` is the global consensus brain singleton. Integrations in trader.py and ai_advisor.py must follow these patterns.

## Key pitfalls

### 1. Use RLock, not Lock
`get_state()` acquires the lock then calls `get_wallet_analysis()`, which also acquires the same lock.
This deadlocks with a plain `threading.Lock()`.
**Fix:** `self._lock = threading.RLock()` in BrainFusion.__init__.

### 2. Wrap the import in a try/except stub
If brain_fusion fails to import (syntax error, missing dep), the entire trader.py and ai_advisor.py fail to start.
**Fix:** Wrap `import brain_fusion as _bf` in try/except, provide a no-op stub class `_BFStub` with all methods returning safe defaults.

### 3. Coerce ai_conf input in should_skip_confirmation
Caller passes `conf` from ai_result which can be None, string, or non-numeric.
**Fix:** `ai_conf = float(ai_conf or 0.0)` at the top of should_skip_confirmation().

### 4. Always restore Config params in BOTH BUY paths
When scalp mode temporarily modifies Config.TAKE_PROFIT_PCT/TRAILING_STOP_PCT/TARGET_NET_PCT,
and pump mode modifies Config.AI_SIZE_MULT — restore them in BOTH:
- The `if use_smart:` (pending-buy) path — restore immediately since _open_trade is deferred
- The `else:` (immediate-open) path — restore after `_open_trade()`

Save all originals with `_save_tp`, `_save_trail`, `_save_net`, `_save_mult` flags at the start of the BUY block.

## How to apply
Any future addition that temporarily modifies Config params for a single trade must follow the save/restore pattern with separate restoration in pending-buy and immediate-open paths.

## Module API summary
- `_bf.update_ai(ai_dict)` — call after ai_engine.analyze() each tick
- `_bf.update_ta(result_dict)` — call after strategy.analyze() each tick  
- `_bf.update_advisor(verdict, confidence, regime, advice)` — call after LLM response
- `_bf.update_wallet(ton, grinch, price_ton, pnl_pct)` — call each tick
- `_bf.get_fusion_signal()` → FusionSignal dataclass (is_scalp_window, is_pump_window, position_boost, scalp_tp_pct, scalp_trail_pct)
- `_bf.should_skip_confirmation(conf)` → bool
- `_bf.is_bullish_consensus(min_conf)` → bool
- `_bf.on_trade_closed(pnl_ton, was_scalp)` — feedback after trade close
- `_bf.get_state()` → dict for dashboard/LLM snapshot

**Why:** BrainFusion integrates three async data sources (AI tick, TA tick, LLM 10min interval) — the lock nesting and import resilience patterns are critical for stability.
