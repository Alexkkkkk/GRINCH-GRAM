#!/bin/bash
set -euo pipefail

# ═══ Telegram Alert Function ═══
TG_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TG_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

tg_alert() {
  local msg="$1"
  if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage"       -d "chat_id=${TG_CHAT_ID}"       -d "parse_mode=Markdown"       -d "text=${msg}" > /dev/null 2>&1 || true
  fi
}

echo "🏥 Local Health Check"
echo "====================="

# Health endpoint
HEALTH_OK=false
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Bot health: OK"
    HEALTH_OK=true
else
    echo "❌ Bot health: FAILED"
    tg_alert "⚠️ *GRINCH-GRAM Health Check*\n❌ Bot health endpoint FAILED on VPS\n🔍 Run: bash /opt/bot/scripts/self-heal.sh"
fi

# Container status
echo "--- Containers ---"
docker compose -f /opt/bot/docker-compose.prod.yml ps

# Resource usage
echo "--- Resources ---"
DISK_USED=$(df -h / | tail -1 | awk '{print $5}')
MEM_USED=$(free | grep Mem | awk '{printf "%.1f%%", $3/$2 * 100.0}')
echo "Disk: ${DISK_USED} used"
echo "Memory: ${MEM_USED} used"

# Alert on high resource usage
DISK_NUM=$(echo "$DISK_USED" | tr -d '%')
if [ "$DISK_NUM" -gt 85 ] 2>/dev/null; then
    tg_alert "⚠️ *GRINCH-GRAM Alert*\n💾 Disk usage critical: ${DISK_USED}\n🧹 Consider running cleanup"
fi

# Image versions
echo "--- Images ---"
docker images ghcr.io/alexkkkkk/grinch-gram --format "{{.Tag}} | {{.Size}}" | head -5

# Summary alert if unhealthy
if [ "$HEALTH_OK" = false ]; then
    exit 1
fi
