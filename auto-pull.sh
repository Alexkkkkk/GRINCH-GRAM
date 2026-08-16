#!/bin/bash
set -euo pipefail

cd /opt/bot
BRANCH="${VPS_BRANCH:-main}"

git fetch origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date) | New commit detected: $REMOTE" >> /var/log/auto-pull.log
    git pull --ff-only origin "$BRANCH"
    docker-compose down
    docker-compose up -d --build
    echo "$(date) | Deployed $REMOTE" >> /var/log/auto-pull.log
fi
