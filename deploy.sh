#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# QuantumBrain — Auto-deploy script (запускается cron'ом каждые 3 минуты)
# Путь на VPS: /opt/bot/deploy.sh
# Cron: */3 * * * * /bin/bash /opt/bot/deploy.sh >> /opt/bot/deploy.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BOT_DIR="/opt/bot"
LOG="$BOT_DIR/deploy.log"
LOCK="$BOT_DIR/.deploy.lock"
MAX_LOG_LINES=1000

cd "$BOT_DIR"

# ── Ротация лога (не даём расти бесконечно) ──────────────────────────────────
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt "$MAX_LOG_LINES" ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

TS() { date '+%F %T'; }

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
