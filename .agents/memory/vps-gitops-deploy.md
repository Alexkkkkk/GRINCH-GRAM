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

**Cron deploy.sh** на VPS (`*/3 * * * *`) делает `git fetch` → если `origin/main` сдвинулся → `git reset --hard origin/main` + `docker compose up -d --build`. Пока push в GitHub не работает — cron ничего не трогает (HEAD == origin/main). Но если кто-то сделает push в GitHub из другого места — cron перезапишет все scp-правки!

**Почему docker cp недостаточен:** изменения в container writable layer живут только до следующего `docker compose up -d --build` (rebuild из /opt/bot). Всегда после docker cp нужно также обновить файл в /opt/bot — тогда rebuild подхватит правильную версию.

**How to apply:** каждая правка = scp в /opt/bot + rebuild. Не надеяться только на docker cp.
