---
name: VPS GitOps deployment pipeline
description: How code actually reaches the production VPS bot container — matters before any "hotfix" or manual docker cp.
---

The production VPS (`/opt/bot`, container `bot-bot-1`) is a git checkout of the
`origin/main` GitHub repo (same repo this Replit workspace pushes to). A cron
job runs `/opt/bot/deploy.sh` every ~1 min: `git fetch` → if `origin/main`
moved, `git reset --hard origin/main` → `docker compose up -d --build`.

**Why this matters:** any change applied only via `docker cp` into the running
container lives in the container's ephemeral layer. The NEXT time deploy.sh
(or any `docker compose up -d --force-recreate`/`--build`) runs, the container
is rebuilt from the git-tracked source in `/opt/bot`, silently discarding the
`docker cp` patch. This bit a session that had "deployed" several fixes via
`docker cp` — they happened to survive because they'd *also* already been
pushed to `origin/main` earlier in the session, but a later fix (adding
CatBoost to `ai_engine.py` + `requirements.txt`) had only been committed
locally in Replit, not yet reflected in the container, and was lost on the
next recreate until the commit was confirmed pushed to `origin/main` and
deploy.sh re-run.

**How to apply:** to ship any change to this bot, commit + push to `origin/main`
from the Replit repo (the environment appears to auto-commit/push already —
verify with `git status`/`git log` before assuming a change reached prod).
Treat `docker cp` as a temporary way to *test* a change live, never as the
deploy mechanism — always follow up by confirming the same content is committed
and pulled via `deploy.sh` (`git -C /opt/bot rev-parse HEAD` should match
`origin/main`, and `docker compose up -d --build` must have actually run, not
just `--force-recreate` which reuses the cached image).
