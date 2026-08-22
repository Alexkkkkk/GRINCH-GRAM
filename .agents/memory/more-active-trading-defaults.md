---
name: More-active-trading defaults
description: What was changed to make the bot trade more actively, and why it had to be done outside the LLM advisor.
---

The AI advisor (`ai_advisor.py`) can only self-tune trading parameters when a live `GROQ_API_KEY` is configured (via dashboard or env). At the time of this change no key was set (`get_section("advisor").get("groq_api_key")` empty), so `run_advisor()` short-circuits with `"GROQ_API_KEY не задан"` and never applies recommendations.

**Why:** the user asked to make the advisor trade more actively, but with no key the advisor is inert — the only way to actually deliver "more active trading" right then was to edit the underlying source defaults (`config.py`, `ai_engine.py`, `ai_advisor.py`) directly and also persist the same values into `settings_store` "config" section (which `config.py` loads on import and overrides source defaults), so it takes effect immediately without a restart-losing the change.

**How to apply:** when asked to tune trading aggressiveness and no Groq key is present, adjust in tandem: `Config.MIN_AI_CONFIDENCE` (min AI confidence to enter), `Config.AI_SIZE_MULT` (position size multiplier), `Config.SMART_BUY_PULLBACK_PCT` (how much pullback to wait for before buying), `ai_engine.BUY_THRESHOLD` (probability needed for AI BUY signal — this one has no settings_store persistence, only source-default + live in-memory), and `ai_advisor.AUTO_INTERVAL_MIN` (how often the advisor re-evaluates, once a key exists). Keep values within the same TUNABLE ranges the advisor itself is allowed to use, and never touch `ONLY_PROFIT_EXIT` (always True, hardcoded).
