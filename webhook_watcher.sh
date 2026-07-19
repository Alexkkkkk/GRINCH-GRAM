#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# QuantumBrain — Хост-демон: наблюдает за trigger-файлом от webhook
# Когда Flask (внутри Docker) записывает /app/data/.deploy_trigger,
# этот скрипт на ХОСТЕ запускает deploy.sh и убирает trigger.
#
# Установка (запускается из setup_autodeploy.sh):
#   systemctl enable --now quantumbrain-watcher
# ─────────────────────────────────────────────────────────────────────────────
BOT_DIR="/opt/bot"
LOG="$BOT_DIR/deploy.log"

# Находим путь к volume на хосте
VOLUME_PATH=$(docker volume inspect bot_bot_data --format '{{.Mountpoint}}' 2>/dev/null || echo "")
if [ -z "$VOLUME_PATH" ]; then
    echo "[$(date '+%F %T')] [Watcher] ❌ volume bot_bot_data не найден — выход" >> "$LOG"
    exit 1
fi

TRIGGER="$VOLUME_PATH/.deploy_trigger"
echo "[$(date '+%F %T')] [Watcher] 🟢 Запущен. Слежу за: $TRIGGER" >> "$LOG"

while true; do
    if [ -f "$TRIGGER" ]; then
        MSG=$(cat "$TRIGGER" 2>/dev/null | head -1 || echo "?")
        echo "[$(date '+%F %T')] [Watcher] 🔔 Trigger получен: $MSG" >> "$LOG"
        rm -f "$TRIGGER"
        /bin/bash "$BOT_DIR/deploy.sh" >> "$LOG" 2>&1
    fi
    sleep 5
done
