---
name: Groq AI Advisor
description: How the Groq-based autonomous trading advisor is wired, its known startup race condition, and safety boundaries.
---

`ai_advisor.py` provides an autonomous advisor: Groq LLM (via OpenAI-compatible API, model `llama-3.3-70b-versatile`) reviews bot state/trades and can auto-tune trading parameters (a bounded `TUNABLE` dict), never rewrites source code. Key is entered via the dashboard (Settings tab) and persisted in `settings_store` (DB-first, JSON fallback), not just env vars.

**Startup race condition:** the module read the Groq key once at import time from `settings_store`. If the DB connection wasn't ready yet at that exact moment, `GROQ_API_KEY` stayed empty for the process lifetime even though the key was correctly stored, making the advisor silently report "key not set" until next restart.

**Why:** `settings_store._db()` depends on `db_store` lazily creating a connection pool; on cold start this can race with other module imports.

**How to apply:** always read the key lazily at call time (`_effective_key()` helper: use in-memory value if present, else re-read from `settings_store`) rather than caching a single import-time snapshot. Apply the same lazy-read pattern to any other credential loaded from settings_store at module import.

**CORRECTED (2026-07-06):** the previous note claiming a hardcoded external Postgres fallback (`node1.pghost.ru`) was WRONG — verified `db_store.py` only ever reads `os.environ.get("DATABASE_URL")`, no external host anywhere in the file. All settings/state persist in the Replit-provisioned Postgres DB. The stray "pghost.ru" text was just a stale docstring comment in `db_backup.py` (now fixed) — do not repeat the external-DB claim.

**Safety boundary:** "full AI autonomy" here means autonomous parameter tuning within hardcoded bounds (see `full-ai-adaptation.md`, `profit-only-guarantee.md`), not literal AI-driven source code modification — that was intentionally not implemented due to financial/safety risk for a bot handling real custodial crypto funds. User repeatedly asked for the AI to "change the code itself"; the response each time is to expand the safe TUNABLE parameter surface (trailing-stop stages, short-trail, smart-TP confidence, etc. added) rather than cross into source mutation.

**Session interval persistence (2026-07-06):** `AUTO_INTERVAL_MIN`/`AUTO_TRADES_TRIGGER` had the same import-time-only bug as the API key — `set_config()` only mutated the in-memory global, so any dashboard change reverted to the hardcoded default on restart. Fixed by mirroring the key's lazy-load pattern: read persisted `interval_min`/`trades_trigger` from `settings_store` section "advisor" at import, and have `set_config()` write back via `update_section` on every change. Apply this same read-at-import + write-on-change pattern to any other advisor tunable that should survive a restart.
