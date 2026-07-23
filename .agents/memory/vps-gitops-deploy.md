---
name: VPS GitOps deployment pipeline
description: How code actually reaches the production VPS bot container — matters before any "hotfix" or manual docker cp.
---

**Пользователь работает ТОЛЬКО с VPS (2.27.25.126, контейнер `bot-bot-1`).** Replit — только редактор кода. Превью Replit не используется.

**Стандартный деплой правок на VPS:**
1. Отредактировать файл(ы) в Replit
2. `scp <file> $VPS_SSH_USER@2.27.25.126:/opt/bot/<path>/`
3. `ssh VPS "cd /opt/bot && docker compose up -d --build"`

GitHub push через HTTPS не работает (нет токена). Deploy-ключ `vps-bot-deploy` на GitHub — read-only. Поэтому `git push origin main` из Replit падает с 403.

**Полная авто-деплой система (настроена и работает):**
- Replit пушит в GitHub через `gitPush({})` (Replit-managed credentials, работает)
- Cron на VPS `*/3 * * * *` → `deploy.sh` → `git fetch` → если сдвинулся origin/main → `git reset --hard` + `docker compose up -d --build --force-recreate`
- GitHub Webhook → `POST /webhook/github` на боте → пишет trigger-файл в `/var/lib/docker/volumes/bot_bot_data/_data/.deploy_trigger`
- `quantumbrain-watcher.service` (systemd, active) мониторит trigger-файл каждые 5с → запускает `deploy.sh` на хосте
- GitHub webhook нужно настроить вручную: `http://2.27.25.126/webhook/github` (Settings → Webhooks в репо)
- После каждого push обязательно сверять `git rev-parse HEAD` на VPS и `/health`: watcher может задержать применение, тогда безопасно запустить штатный `/opt/bot/deploy.sh` вручную.

**Почему docker cp недостаточен:** изменения в container writable layer живут только до следующего `docker compose up -d --build` (rebuild из /opt/bot). Всегда после docker cp нужно также обновить файл в /opt/bot — тогда rebuild подхватит правильную версию.

**How to apply:** каждая правка = scp в /opt/bot + rebuild. Не надеяться только на docker cp.
