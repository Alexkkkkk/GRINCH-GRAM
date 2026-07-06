---
name: Manual trading on/off switch
description: How the manual trading-enabled toggle differs from other pause/stop mechanisms in this bot
---

There are THREE distinct "stop trading" mechanisms in this bot — don't conflate them:

1. `trader.running` (Пуск/Стоп buttons, `/api/start` `/api/stop`) — starts/stops the ENTIRE background thread: AI pretraining, tick loop, equity recording. Auto-starts on every boot via `start_background()`.
2. `experience_manager` `control.paused` (`exp.is_paused()`) — AI's own auto-pause on drawdown, persists across restarts, only blocks new BUY entries.
3. `trader.trading_enabled` (manual toggle button, `/api/trading/enable|disable`) — the user-facing kill switch. Always initializes to `False` in `Trader.__init__` (never read from persisted storage), so every code restart defaults to trading OFF. Gated at the very top of both `_tick()` and `_tick_dca()`, blocking ALL trade execution (entries AND exits) while leaving price monitoring/dashboard/AI analysis running normally.

**Why:** user explicitly required trading to default OFF after every restart and require an explicit manual action to resume — this must not be confused with the AI's own risk-pause (#2), which is a different, semi-persistent mechanism.

**How to apply:** any new trade-executing code path (new entry strategy, new exit path) must also check `self._trading_disabled_guard()` (or the `trading_enabled` flag directly) at its start, the same way `_tick`/`_tick_dca` do.
