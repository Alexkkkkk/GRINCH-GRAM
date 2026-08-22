---
name: Telegram alerts for trading-loop health
description: How and why alerts.py notifies via Telegram when the trading loop stalls or errors, and the anti-spam design behind it.
---

`alerts.py` reuses the exact same health logic as `/health` in `app.py`
(`trader.last_tick_ts` staleness > 90s = unhealthy, `trader.last_tick_ok is
False` = degraded) so the two never drift apart — there is one source of
truth for "is the trading loop okay", checked by both the HTTP health probe
and the background alert monitor.

**Why direct Bot API instead of the `python-telegram-bot` dependency already
in requirements.txt:** sending one message is a single `POST
https://api.telegram.org/bot<token>/sendMessage` call. Pulling in the full
library's async update-handling machinery for that is unnecessary weight;
`http_client.SESSION` (already used everywhere else for keep-alive pooling)
covers it in a few lines.

**Anti-spam design:** the monitor thread polls every 20s but only sends a
message on a *state transition* (healthy→unhealthy/degraded, or back to
healthy) — not on every poll. If a bad state persists, it will still resend
at most once every 5 minutes (`_MIN_RESEND_GAP`), so an ongoing outage isn't
silent but also doesn't flood the chat.

**How to apply:** credentials (`telegram_bot_token`, `telegram_chat_id`,
`enabled`) live in the `alerts` section of `settings_store` (same DB+JSON
pattern as the Groq advisor key), configurable from the dashboard's
"Настройки" tab — never hardcode them. If a new background loop is added
that should also be monitored, extend `_compute_state()` rather than
starting a second monitor thread.
