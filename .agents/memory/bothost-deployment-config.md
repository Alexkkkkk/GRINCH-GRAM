---
name: Bothost deployment config
description: Critical config for running this bot on Bothost (255MB RAM, Docker, persistent /app/data volume)
---

# Bothost deployment config

## Rules

1. `DATA_DIR=/app/data` must be set in Dockerfile ENV — this is Bothost's persistent volume (survives redeploys). Without it, settings.json and .session_secret reset on every container restart.

2. `EXTERNAL_DATABASE_URL` takes priority over `DATABASE_URL` in `db_store.py` (line 47). Set this to the pghost.ru connection string in Bothost Variables panel — NOT in code.

3. `session_secret` file is stored in DATA_DIR (app.py `_resolve_secret_key()`), not in CWD. Fixed to prevent random session key on every restart.

4. ai_advisor `start_background()` was accidentally called twice — once inside `start_background()` and once at module level in app.py. Removed the duplicate.

5. DB pool: `maxconn=8` in LOW_MEMORY_MODE (Bothost), 16 otherwise. Each psycopg2 connection = ~4-8MB RSS.

6. Groq 413 (tokens per minute limit): advisor window reduced 100→50 ticks; `recent_ticks` trimmed to 6 entries; `mini_candles` removed from prompt.

**Why:** Bothost has 255MB RAM limit. Every MB counts. The persistent volume at /app/data is the only storage that survives container restarts.

**How to apply:** Any new file that must persist across Bothost redeploys should write to `os.environ.get("DATA_DIR", ...)` path.
