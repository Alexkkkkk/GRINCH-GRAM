---
name: GridAI-Mgr step oscillation
description: GridAI-Mgr._manage() must NOT set step_pct — adjust_step_by_atr() (ML) already does it in _tick() before the manager runs.
---

# GridAI-Mgr step oscillation

## The rule
Never add step-setting logic inside `GridAI-Mgr._manage()` (the `GridAIManager` class in `grid_trader.py`).

**Why:** `_tick()` calls `adjust_step_by_atr()` first, which uses `grid_ai.get_optimal_step()` (full ML ensemble with regime-specific bounds). Then `_tick()` calls `ai_manager.tick()`. Any step override in `_manage()` runs *after* the ML result and tramples it, causing an infinite oscillation:
- ML (VOLATILE bounds [5,10%]) → 5.5%
- Manager formula `atr * policy_mult` = 2.2% × 1.1 = 2.44% → clamped to MIN_STEP=4.0%
- Next tick: ML sees 4.0%, raises to 5.5%; manager fires again → 4.0% … forever

**How to apply:** If you ever want the manager to influence the step, do it by adjusting the `grid_ai` experience/calibration (e.g. `calibrated_min_step`), not by writing `t._state.step_pct` directly.

## Fix applied 2026-08-06
Removed the "── 2. Динамический шаг по ATR×policy" block (~12 lines) from `GridAI-Mgr._manage()` in `grid_trader.py`. Committed to VPS `/opt/bot` git repo (commit 7be5a50).
