#!/bin/bash
set -e
cd /opt/bot
LOG=/opt/bot/deploy.log
git fetch origin main >> "$LOG" 2>&1
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" != "$REMOTE" ]; then
  echo "[$(date '+%F %T')] Новый коммит: $REMOTE (было $LOCAL) — деплою" >> "$LOG"
  git reset --hard origin/main >> "$LOG" 2>&1
  docker compose up -d --build >> "$LOG" 2>&1
  echo "[$(date '+%F %T')] Деплой завершён" >> "$LOG"
fi
