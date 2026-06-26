---
name: Profit-only (never-sell-at-a-loss) guarantee
description: ONLY_PROFIT_EXIT must hold across ALL sell paths and cannot be disabled
---

# Profit-only guarantee — "никогда не продавать в минус"

Owner requirement: the bot must NEVER sell a position at a loss — if it's in the red,
hold until the price recovers. This is `Config.ONLY_PROFIT_EXIT`.

**Rules:**
- `ONLY_PROFIT_EXIT` is hardcoded `True` and reasserted at config.py module top-level
  AFTER the settings.json loader, so a persisted/legacy key cannot disable it. It is NOT
  exposed in the dashboard settings UI (the "Стоп-лосс (%)" field was removed) and not env-toggleable.
- EVERY sell/close path must enforce the floor `Config.required_gross_pct()` before selling:
  - auto AI-signal exit (`_close_all_trades`)
  - SL/TP/trailing exit (`_check_stop_loss_take_profit`) — in this mode stop_loss starts at 0
    and only arms upward once profit ≥ floor, so it can only exit in profit
  - **manual/API single close (`close_trade` → /api/trade/close)** — must also reject
    `pnl_pct < required_gross_pct()` and return `{ok:False, error:...}` without selling.

**Why:** when only the auto paths were gated, the manual close path (/api/trade/close) still
force-sold at a loss — a real bypass of the guarantee. Any NEW close/sell path added later
must reuse the same floor check, or it silently reopens the loss-exit hole.

**How to apply:** when adding any code that calls `_close_trade`/`place_order("sell", ...)`,
gate it on `Config.ONLY_PROFIT_EXIT` + `required_gross_pct()` unless it is an explicit,
privileged emergency override. `STOP_LOSS_PCT` still exists in Config (default 5.0) but is
inert while ONLY_PROFIT_EXIT is True (sl forced to 0); don't resurrect it in the UI.
