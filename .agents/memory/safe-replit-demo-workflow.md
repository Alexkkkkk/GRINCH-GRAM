---
name: Safe Replit demo workflow
description: How to run the dashboard in Replit without competing with the live VPS bot
---

The Replit preview workflow must unset `EXTERNAL_DATABASE_URL` and `DATABASE_URL` and set `DEMO_MODE=true`. This keeps the preview read-only/demo and prevents it from becoming a second process writing to the VPS bot's shared PostgreSQL database.

**Why:** The production VPS bot and the Replit project can point at the same external database; running both as live workers causes conflicting writes, timeouts, and inconsistent bot state.

**How to apply:** Preserve the safe environment overrides whenever recreating or editing the Replit web workflow. Do not use the production database in a preview workflow unless the VPS worker is stopped or the databases are explicitly separated.