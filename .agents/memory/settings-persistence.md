---
name: Dashboard settings persistence
description: How dashboard-changeable settings survive restarts via settings.json
---

Dashboard-editable settings persist across restarts through `settings.json` (written by `settings_store.py`), NOT by rewriting Python source.

**Why:** user wanted "change from dashboard AND save in code" — in-memory Config/liquidator state reset to defaults on every restart. A JSON file is the persistence boundary; defaults in `config.py`/liquidator remain the fallback.

**How to apply:**
- `settings_store.py` exposes `get_section(name)` / `update_section(name, dict)`; writes are lock-guarded + atomic (tmp + os.replace). Single-process threaded app, so process-local lock is sufficient.
- Sections: `"config"` (UPPERCASE Config attr names) and `"liquidator"` (`sell_rise_pct`).
- `config.py` applies the `config` section at import time with **type coercion to the default's type** (guards a corrupted file from injecting bad types). Recompute derived values (e.g. `FEE_ROUND_TRIP`) after overrides.
- Any NEW dashboard-tunable setting must: (1) be persisted in its POST handler via `update_section`, and (2) be loaded/applied at startup — otherwise it silently resets on restart.
- Keep secrets OUT of settings.json (it is committed alongside code).
