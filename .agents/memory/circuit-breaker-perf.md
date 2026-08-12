---
name: Circuit Breaker & Performance System
description: Daily auto-pause + extended stats + BrainFusion dynamic weights added in July 2026
---

## Circuit Breaker (Daily Stop-Loss)
- Config: `CIRCUIT_BREAKER_ENABLED=1`, `CIRCUIT_BREAKER_DAILY_LOSS_PCT=15.0`
- Tracks `daily_pnl` + `daily_start_equity` in `trader.stats`
- Resets at midnight UTC via `_reset_daily_stats_if_needed()` called every tick
- Check in `_tick()` AFTER manual-guard, BEFORE DCA/AI logic
- Sends Telegram alert via `alerts.send_alert()` on trigger

**Why:** Prevents compounding losses on a bad day (bad market regime, API anomaly, strategy error).

## Extended Stats (trader.stats)
New fields added alongside existing total_pnl/total_trades/winning_trades:
- `win_streak` / `max_win_streak` — current and best consecutive wins
- `best_trade_ton` / `worst_trade_ton` — single trade extremes
- `daily_pnl` / `daily_start_ts` / `daily_start_equity` — day-scoped tracking
- `circuit_breaker_active` — bool flag (reset at midnight)

Central update method: `_record_trade_pnl(pnl_ton)` — call INSIDE `_close_lock`.
Called from `_close_trade_locked` (main close path).

## Stale Position Reaper (opt-in)
- Config: `STALE_POSITION_ENABLED=0` (default OFF), `STALE_POSITION_MAX_HOURS=72`, `STALE_POSITION_MIN_PROFIT_PCT=1.0`
- Runs in `_check_stop_loss_take_profit` per-trade before TP/SL logic
- Closes position if age > MAX_HOURS AND profit_pct < MIN_PROFIT_PCT

## BrainFusion Dynamic Weights
- Per-source tracking: `_ai_wins/_ai_total`, `_ta_wins/_ta_total`, `_adv_wins/_adv_total`
- Updated in `on_trade_closed()` based on last signal at close time
- `_dyn_base_w(base, wins, total)`: adjusts ±25% from base, clamps to [0.3×base, 1.5×base]
- Active after ≥5 evaluations per source; below that threshold — static base weights
- Persisted in bot_ai_state DB key "brain_fusion"

**Why:** Static weights assume AI is always 70% reliable regardless of market conditions. Dynamic weights auto-boost the better-performing source.

## Performance API
- New endpoint: `GET /api/performance` → Sharpe Ratio (annualized), max drawdown, win_streak, max_win_streak, best/worst_trade, daily_pnl, circuit_breaker, source_accuracy
- Sharpe = (mean_pnl / std_pnl) × √252, requires ≥5 closed trades
- api_memory cached 10s (gc.get_objects() was expensive per-call)
- data_hub.get_snapshot() cached 5s (called from AI engine every tick)

## Dashboard UI
- Ticker bar: "Сегодня: +X.XXX TON" (daily P&L), "🔥 N подряд" (streak ≥2), "⛔ CB АКТИВЕН" (circuit breaker)
- Trades tab stats bar: streak, max-streak, Sharpe, daily P&L added
- JS polls `/api/performance` every 15s
