---
name: Audit report architecture mapping
description: External audit checklists may assume SQLite or a different schema; validate each item against the live PostgreSQL/DeDust architecture before applying it.
---

External audit instructions must be mapped to the current database schema, transaction model, and DEX flow before implementation. SQLite snippets, nonexistent tables, and generic SDK fallbacks are not safe to copy literally.

**Why:** The imported audit mixed SQLite examples with the bot's PostgreSQL schema and included a DeDust SDK fallback that could send tokens to an unsafe address.

**How to apply:** Confirm the actual function, schema, and on-chain path first; implement only the compatible invariant and verify with compilation plus a clean workflow restart.