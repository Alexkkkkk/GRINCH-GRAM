---
name: Replit vs Bothost port targets
description: Two separate deployment targets for this bot use different, intentionally different ports.
---

This project runs in two independent places with different port requirements:

- **Replit workspace/preview**: the workflow's server must bind port 5000 (Replit's webview rule).
  `main.py` defaults `PORT` to 5000, `.replit` `[[ports]]` maps 5000→80, and `waitForPort = 5000`.
- **Bothost Docker deployment**: `Dockerfile` / `docker-compose.yml` are locked to `PORT=${PORT:-3000}`
  per `bothost-locked-settings.md` — a separate external host with its own reverse proxy expecting 3000.

**Why:** early on, `.replit` still had a leftover dual-port setup (3000 unused-in-practice +
5000 unmapped) inherited from a prior import, which caused a blank Replit preview even though the
process was healthy. Fixing it means aligning the *Replit* side to 5000; it does NOT mean touching
the Bothost Dockerfile/compose files, which are a different target with a locked port.

**How to apply:** if the Replit preview is blank/refused but the process logs show it's up, check
which port it actually bound vs `waitForPort`/`[[ports]]` — don't assume the Dockerfile/Bothost
config is broken or needs to match. Never edit Bothost's `PORT=3000` without explicit user permission.
