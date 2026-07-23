#!/bin/bash
# QuantumBrain — Быстрое обновление кода на VPS без ребилда Docker
# Запустить на VPS хосте: bash patch_vps.sh
# Обновляет .py файлы внутри запущенного контейнера из GitHub + graceful reload

set -e
GITHUB_RAW="https://raw.githubusercontent.com/Alexkkkkk/GRINCH-GRAM/main"
WORKDIR="/usr/src/app"

echo "=== QuantumBrain VPS Patch ==="

# Найти запущенный контейнер бота
CONTAINER=$(docker ps --format "{{.ID}} {{.Image}} {{.Names}}" | grep -v "CONTAINER" | head -1 | awk '{print $1}')
if [ -z "$CONTAINER" ]; then
  echo "ERROR: не найден запущенный Docker-контейнер"
  exit 1
fi
echo "Контейнер: $CONTAINER ($(docker ps --format '{{.Names}}' --filter id=$CONTAINER))"

# Список файлов для обновления
FILES=(
  "ai_engine.py"
  "strategy.py"
  "experience_manager.py"
  "trader.py"
  "app.py"
  "config.py"
  "alerts.py"
  "analytics_buffer.py"
  "brain_fusion.py"
  "liquidity_guard.py"
  "price_feed.py"
  "settings_store.py"
  "wallet_manager.py"
  "wallet_tracker.py"
  "db_store.py"
  "http_client.py"
)

echo "[1/3] Обновление файлов..."
for f in "${FILES[@]}"; do
  if docker exec "$CONTAINER" bash -c "curl -sSfL '$GITHUB_RAW/$f' -o '$WORKDIR/$f.tmp' && mv '$WORKDIR/$f.tmp' '$WORKDIR/$f'" 2>/dev/null; then
    echo "  ✅ $f"
  else
    echo "  ⚠️ $f (пропущен — нет в репозитории)"
  fi
done

echo "[2/3] Graceful reload gunicorn..."
docker exec "$CONTAINER" bash -c "
  PID=\$(pgrep -f 'gunicorn main:app' | head -1)
  if [ -n \"\$PID\" ]; then
    kill -HUP \$PID
    echo 'SIGHUP отправлен PID='\$PID
  else
    echo 'gunicorn PID не найден — перезапускаю контейнер'
    exit 1
  fi
" || docker restart "$CONTAINER"

echo "[3/3] Проверка health..."
sleep 10
curl -sf http://localhost/health && echo " ✅ Бот работает" || echo " ⚠️ Проверьте: docker logs $CONTAINER"

echo ""
echo "=== Patch завершён ==="
echo "Проверка и исправление открытых позиций выполняются через защищённую админ-сессию."
