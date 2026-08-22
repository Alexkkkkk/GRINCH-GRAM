#!/bin/bash
set -euo pipefail

# Авто-обновление бота с GitHub
# Запускается systemd таймером каждые 2 минуты

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BRANCH="${VPS_BRANCH:-main}"
LOG_FILE="${SCRIPT_DIR}/logs/auto-pull.log"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.prod.yml"
mkdir -p "$(dirname "$LOG_FILE")"

git fetch origin "$BRANCH" 2>/dev/null || {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | Git fetch failed" >> "$LOG_FILE"
    exit 0
}

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | New commit: $REMOTE (was $LOCAL)" >> "$LOG_FILE"
    git reset --hard "origin/$BRANCH"
    docker-compose -f "$COMPOSE_FILE" down
    docker-compose -f "$COMPOSE_FILE" up -d --build
    echo "$(date '+%Y-%m-%d %H:%M:%S') | Deployed $REMOTE" >> "$LOG_FILE"
fi
