---
name: VPS git drift and broken auto-deploy
description: The live VPS bot's checkout has uncommitted local edits and its cron auto-deploy (git pull) is broken — don't assume origin/main reflects what's actually running.
---

## Finding (2026-07-15)
- `/opt/bot` on the VPS (2.27.25.126, container `bot-bot-1`) has real uncommitted `git diff` against its own HEAD in `trader.py`, `ai_advisor.py`, `dedust_client.py`, `static/js/app.js`, `templates/index.html`, `static/css/style.css`, `.replit` — meaningful production hotfixes that were made directly on the VPS and never committed/pushed.
- The VPS's own `deploy.sh` (`git fetch origin main` → `git reset --hard origin/main` → `docker compose up -d --build`) has been silently failing: `git fetch` gets `Permission denied (publickey)` against `git@github.com:Alexkkkkk/GRINCH-GRAM.git`. Because of `set -e`, the script exits before ever reaching `git reset`/rebuild — so cron-triggered auto-deploy has not applied any new GitHub commits for some time, but it also never overwrote the local uncommitted edits (lucky accident, not by design).
- `docker-compose.yml` uses `build: .` (Dockerfile `COPY . .` bakes the working tree into the image) — there is no code bind-mount. This means `docker compose up -d --build` on the VPS always rebuilds from whatever is currently sitting in `/opt/bot`'s working tree, uncommitted changes included.

**Why this matters:** the Replit repo and GitHub `origin/main` are NOT a reliable source of truth for "what the live bot is actually running." Assuming they are and force-pushing or resetting VPS's `/opt/bot` to `origin/main` would silently discard real production hotfixes.

**How to apply:** to change VPS behavior, always SSH in and check `git status --short` / `git diff --stat` in `/opt/bot` first. Apply edits directly to the VPS's working tree (mirroring whatever change was made in Replit), then `docker compose up -d --build` there — do NOT `git pull`/`git reset --hard` on the VPS until the SSH deploy key permission issue is fixed AND the local diffs are reconciled (committed) with the user's explicit go-ahead.
