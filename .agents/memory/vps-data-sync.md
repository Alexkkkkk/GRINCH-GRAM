---
name: VPS data folder ↔ DB sync
description: How /app/data on the production VPS relates to the external Postgres DB, and how to check/trigger a sync.
---

The live bot runs as Docker container `bot-bot-1` on the VPS, with `/app/data` mounted from
Docker volume `bot_bot_data`. `DATA_DIR=/app/data` inside the container.

`db_backup.py` already runs as a background thread inside the app and periodically dumps all
7 core tables (bot_settings, bot_trades, bot_equity, bot_open_trades, bot_ai_state, bot_wallets,
bot_wallet_meta) from the DB into `DATA_DIR/backups/<timestamp>/*.json` — this IS the "DB → data
folder" sync mechanism; no separate script is needed to satisfy that request.

Separately, `experience.json`, `settings.json`, `wallets.json` directly under `DATA_DIR` are live
JSON fallback files kept in sync on every write by the app itself (dual-write DB+JSON per
settings-persistence / ai-experience-persistence memory) — not a one-off batch sync target.

**Why:** when asked to "sync the data folder with the database", first check freshness of the
latest `backups/<timestamp>/_meta.json` on the VPS rather than inventing a new export path —
the feature already exists and running it manually just means triggering/confirming a fresh
backup dump.

**How to apply:** SSH to the VPS (`ssh root@2.27.25.126`, credentials via VPS_SSH_USER/
VPS_SSH_PASSWORD secrets), then `docker exec bot-bot-1 sh -c 'ls -la /app/data/backups | tail -5'`
to confirm recency. Also see the VPS-password-in-chat incident note below.
