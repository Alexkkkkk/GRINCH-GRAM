#!/bin/bash
cd /opt/bot
git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date) | New commit detected: $REMOTE" >> /var/log/auto-pull.log
    git pull origin main
    docker-compose down
    docker-compose up -d --build
    echo "$(date) | Deployed $REMOTE" >> /var/log/auto-pull.log
fi
