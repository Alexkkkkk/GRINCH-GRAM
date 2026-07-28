---
name: VPS SSH Access
description: How to connect to the production VPS from Replit shell commands
---

# VPS SSH Access

**Host:** 2.27.25.126  
**User:** root  
**Auth:** password stored in secret `VPS_SSH_KEY` (yes, the key name holds the password — that's how the user saved it)

## How to run commands on VPS

```bash
sshpass -p "$VPS_SSH_KEY" ssh -o StrictHostKeyChecking=no root@2.27.25.126 "COMMAND"
```

## Key paths on VPS

- Bot code: `/opt/bot` (git-tracked, deployed via cron `deploy.sh`)
- Docker compose: `docker compose` in `/opt/bot`
- Container name: `bot-bot-1`
- Data volume: `/app/data` inside container

## Useful commands

```bash
# Container status
docker ps

# Bot logs (last 50)
docker logs bot-bot-1 --tail=50

# Rebuild and restart
cd /opt/bot && docker compose up -d --build

# Copy file to container
docker cp /opt/bot/file.py bot-bot-1:/usr/src/app/file.py

# Exec inside container
docker exec bot-bot-1 python3 -c "..."
```

**Why:** SSH credentials were added 2026-07-28 via Replit Secrets (VPS_SSH_KEY, VPS_SSH_PASSWORD).

**How to apply:** Any time user asks to deploy, check logs, or run commands on VPS — use sshpass pattern above.
