#!/bin/bash
# QuantumBrain VPS Update Script
# Запустить на VPS-хосте: bash update_vps.sh
set -e

REPO_DIR=$(find /root /home -maxdepth 4 -name "docker-compose.yml" 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
if [ -z "$REPO_DIR" ]; then
  REPO_DIR=$(pwd)
fi

echo "=== QuantumBrain VPS Update ==="
echo "Директория: $REPO_DIR"
cd "$REPO_DIR"

echo "[1/4] git pull..."
git pull origin main

echo "[2/4] docker-compose build..."
docker-compose build --no-cache

echo "[3/4] docker-compose up -d..."
docker-compose up -d

echo "[4/4] Проверка health..."
sleep 8
curl -sf http://localhost/health && echo " ✅ Бот запущен" || echo " ⚠️ Health-check не прошёл"

echo "=== Готово ==="
