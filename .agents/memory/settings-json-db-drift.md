---
name: Settings sections written DB-only, missing from JSON fallback
description: Any code path that calls db_store.settings_update_section() directly (bypassing settings_store.update_section()) creates a section that exists only in PostgreSQL, never in the local settings.json backup.
---

`settings_store.update_section()` is the only function that writes a settings
section to both PostgreSQL and the local `settings.json` file (DB primary +
JSON fallback). Any code that imports `db_store` directly and calls
`db_store.settings_update_section(...)` — or reads via
`db_store.settings_get(...)` — bypasses the JSON side entirely.

**Why:** Found `trader_state` (manual trading on/off switch, pending Smart-BUY
order, DCA cooldown timestamp) was being written/read straight through
`db_store` in `trader.py`, so it only ever existed in the DB. If Postgres were
ever unreachable at startup, the trading toggle and pending-order recovery
would silently reset instead of falling back to the last known JSON state.
`advisor` and `alerts` sections were also found DB-only in the live `data/`
folder (root cause not fully pinned down — no current code path writes them
DB-only, so likely a one-time direct DB write predating some settings_store
usage) and had to be backfilled into JSON manually.

**How to apply:** Any new persistent setting must go through
`settings_store.get_section()` / `update_section()`, never
`db_store.settings_get()` / `settings_update_section()` directly. To check for
drift, compare `settings_store.load_settings()`-visible sections (JSON) against
`db_store.settings_get_all()` (DB) — a symmetric key-diff per section reveals
gaps; backfill a gap by calling `update_section(name, db_store.settings_get_section(name))`.
