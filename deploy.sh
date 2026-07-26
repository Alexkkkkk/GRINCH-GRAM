#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# QuantumBrain — Auto-deploy script (запускается cron'ом каждые 3 минуты)
# Путь на VPS: /opt/bot/deploy.sh
# Cron: */3 * * * * /bin/bash /opt/bot/deploy.sh >> /opt/bot/deploy.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Вспомогательная функция (объявляем ПЕРВОЙ — используется везде ниже) ──────
TS() { date '+%F %T'; }

BOT_DIR="/opt/bot"
LOG="$BOT_DIR/deploy.log"
LOCK="$BOT_DIR/.deploy.lock"
MAX_LOG_LINES=1000

cd "$BOT_DIR"

# ── Алерт переполнения диска (>85% → немедленная очистка кэша) ───────────────
DISK_PCT=$(df / --output=pcent | tail -1 | tr -d ' %')
if [ "$DISK_PCT" -ge 85 ]; then
    echo "[$(TS)] 🚨 ДИСК ${DISK_PCT}% — экстренная очистка build cache!" >> "$LOG"
    docker builder prune -f >> "$LOG" 2>&1
    docker image prune -f   >> "$LOG" 2>&1
    DISK_PCT_AFTER=$(df / --output=pcent | tail -1 | tr -d ' %')
    echo "[$(TS)] 💾 Диск после очистки: ${DISK_PCT_AFTER}%" >> "$LOG"
    # Telegram-уведомление об опасном диске
    TG_T=$(grep TELEGRAM_BOT_TOKEN "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d ' ')
    TG_C=$(grep TELEGRAM_CHAT_ID  "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d ' ')
    if [ -n "$TG_T" ] && [ -n "$TG_C" ]; then
        curl -sf "https://api.telegram.org/bot$TG_T/sendMessage" \
             -d "chat_id=$TG_C&text=🚨+VPS+диск+был+${DISK_PCT}%25.+Очищен+кэш+Docker,+стало+${DISK_PCT_AFTER}%25" \
             > /dev/null 2>&1 || true
    fi
fi

# ── Ротация лога (не даём расти бесконечно) ──────────────────────────────────
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt "$MAX_LOG_LINES" ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# ── Lock — защита от параллельных запусков ────────────────────────────────────
if [ -f "$LOCK" ]; then
    LOCK_PID=$(cat "$LOCK" 2>/dev/null || echo "0")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[$(TS)] SKIP: другой деплой уже запущен (PID=$LOCK_PID)" >> "$LOG"
        exit 0
    fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# ── Автоустановка SSH-ключа Replit (запускается при КАЖДОМ цикле — до early-exit) ─
REPLIT_KEY_FILE="$BOT_DIR/replit_key.pub"
if [ -f "$REPLIT_KEY_FILE" ]; then
    mkdir -p ~/.ssh
    chmod 700 ~/.ssh
    touch ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    REPLIT_KEY=$(cat "$REPLIT_KEY_FILE")
    if ! grep -qF "$REPLIT_KEY" ~/.ssh/authorized_keys 2>/dev/null; then
        echo "$REPLIT_KEY" >> ~/.ssh/authorized_keys
        echo "[$(TS)] ✅ Replit SSH ключ добавлен в authorized_keys" >> "$LOG"
    fi
fi

# ── Проверяем наличие новых коммитов ─────────────────────────────────────────
git fetch origin main --quiet 2>> "$LOG"

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0   # нет изменений — тихо выходим
fi

SHORT_LOCAL="${LOCAL:0:7}"
SHORT_REMOTE="${REMOTE:0:7}"
echo "" >> "$LOG"
echo "═══════════════════════════════════════════════" >> "$LOG"
echo "[$(TS)] 🚀 ДЕПЛОЙ: $SHORT_LOCAL → $SHORT_REMOTE" >> "$LOG"

# ── Применяем новый код ───────────────────────────────────────────────────────
git reset --hard origin/main >> "$LOG" 2>&1
echo "[$(TS)] ✅ git reset OK" >> "$LOG"

# ── Собираем и запускаем ─────────────────────────────────────────────────────
docker compose up -d --build --remove-orphans --force-recreate >> "$LOG" 2>&1
echo "[$(TS)] ✅ docker compose OK" >> "$LOG"

# ── Health-check (ждём до 60 сек пока бот поднимется) ─────────────────────────
echo "[$(TS)] ⏳ Ждём старта (health-check)..." >> "$LOG"
for i in $(seq 1 12); do
    sleep 5
    if curl -sf http://localhost/health > /dev/null 2>&1; then
        echo "[$(TS)] ✅ Health OK — бот запущен (попытка $i)" >> "$LOG"
        HEALTH_OK=1
        break
    fi
done

if [ "${HEALTH_OK:-0}" = "0" ]; then
    echo "[$(TS)] ⚠️  Health-check не прошёл за 60 сек — проверь логи: docker compose logs" >> "$LOG"
fi

# ── Telegram-уведомление о деплое ─────────────────────────────────────────────
TG_TOKEN=$(grep TELEGRAM_BOT_TOKEN "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d ' ')
TG_CHAT=$(grep TELEGRAM_CHAT_ID  "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d ' ')

if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
    STATUS="${HEALTH_OK:-0}"
    EMOJI=$( [ "$STATUS" = "1" ] && echo "✅" || echo "⚠️" )
    MSG="$EMOJI QuantumBrain обновлён%0A$SHORT_LOCAL → $SHORT_REMOTE%0A$(TS)"
    curl -sf "https://api.telegram.org/bot$TG_TOKEN/sendMessage" \
         -d "chat_id=$TG_CHAT&text=$MSG" > /dev/null 2>&1 || true
fi

echo "[$(TS)] 🏁 Деплой завершён" >> "$LOG"
echo "═══════════════════════════════════════════════" >> "$LOG"

# ── Еженедельная очистка Docker (воскресенье, 03:00–04:59) ───────────────────
DOW=$(date '+%u')   # 1=Пн … 7=Вс
HOUR=$(date '+%H')
if [ "$DOW" = "7" ] && [ "$HOUR" -ge 3 ] && [ "$HOUR" -lt 5 ]; then
    PRUNE_FLAG="$BOT_DIR/.last_docker_prune"
    TODAY=$(date '+%Y-%m-%d')
    if [ "$(cat "$PRUNE_FLAG" 2>/dev/null)" != "$TODAY" ]; then
        echo "[$(TS)] 🧹 Еженедельная очистка Docker build cache..." >> "$LOG"
        docker builder prune -f  >> "$LOG" 2>&1
        docker image prune -f    >> "$LOG" 2>&1
        echo "$TODAY" > "$PRUNE_FLAG"
        echo "[$(TS)] ✅ Очистка завершена" >> "$LOG"
    fi
fi
