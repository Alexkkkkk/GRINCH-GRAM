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

echo "🏥 GRINCH-GRAM Self-Heal"
echo "========================"

cd /opt/bot

# ── Check Docker ───────────────────────────────────────────────
echo "🔍 Checking Docker..."
if ! docker version > /dev/null 2>&1; then
    echo "❌ Docker not running, attempting restart..."
    tg_alert "⚠️ *GRINCH-GRAM Alert*\n❌ Docker down on VPS — attempting restart"
    systemctl restart docker || service docker restart || true
    sleep 3
    if ! docker version > /dev/null 2>&1; then
        echo "❌ Docker restart failed"
        tg_alert "🚨 *GRINCH-GRAM CRITICAL*\n❌ Docker restart FAILED on VPS\n👨‍💻 Manual intervention required!"
        exit 1
    fi
    echo "✅ Docker restarted"
    tg_alert "✅ *GRINCH-GRAM Recovered*\n🐳 Docker restarted successfully"
fi

# ── Check containers ───────────────────────────────────────────
echo "🔍 Checking containers..."
if ! docker compose -f docker-compose.prod.yml ps | grep -q "grinch-bot"; then
    echo "⚠️ Bot container not found, starting all..."
    tg_alert "⚠️ *GRINCH-GRAM Alert*\n📦 Bot container missing — starting all services"
    docker compose -f docker-compose.prod.yml up -d
    sleep 10
fi

# ── Check health ──────────────────────────────────────────────
echo "🔍 Health check..."
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Health OK — no action needed"
    exit 0
fi

echo "⚠️ Health check failed — attempting fixes..."
tg_alert "⚠️ *GRINCH-GRAM Alert*\n🏥 Health check failed — starting self-heal sequence"

# ── Fix 1: Restart bot ────────────────────────────────────────
echo "🔄 Fix 1: Restarting bot..."
docker compose -f docker-compose.prod.yml restart bot
sleep 10
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Fixed by restart"
    tg_alert "✅ *GRINCH-GRAM Recovered*\n🔄 Bot restart fixed the issue"
    exit 0
fi

# ── Fix 2: Recreate bot ────────────────────────────────────────
echo "🔄 Fix 2: Recreating bot..."
docker compose -f docker-compose.prod.yml up -d --force-recreate --no-deps bot
sleep 10
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Fixed by recreate"
    tg_alert "✅ *GRINCH-GRAM Recovered*\n🔧 Bot recreate fixed the issue"
    exit 0
fi

# ── Fix 3: Full restart all ───────────────────────────────────
echo "🔄 Fix 3: Full restart all services..."
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d
sleep 15
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Fixed by full restart"
    tg_alert "✅ *GRINCH-GRAM Recovered*\n🔄 Full service restart fixed the issue"
    exit 0
fi

# ── Fix 4: Check disk and clean ───────────────────────────────
echo "🔄 Fix 4: Checking disk space..."
FREE_GB=$(df -BG / | tail -1 | awk '{print $4}' | tr -d 'G')
if [ "$FREE_GB" -lt 2 ]; then
    echo "🧹 Low disk (${FREE_GB}GB), cleaning..."
    docker system prune -af --volumes
    docker image prune -af
    tg_alert "⚠️ *GRINCH-GRAM Alert*\n🧹 Low disk space (${FREE_GB}GB) — cleaned up Docker"
fi

# ── Fix 5: Check logs for errors ──────────────────────────────
echo "🔄 Fix 5: Checking logs..."
LOGS=$(docker logs --tail 30 grinch-bot 2>/dev/null || true)
echo "$LOGS"

# ── Final check ───────────────────────────────────────────────
sleep 5
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Fixed"
    tg_alert "✅ *GRINCH-GRAM Recovered*\n🔧 Self-heal succeeded after multiple attempts"
    exit 0
else
    echo "❌ All fixes failed — manual intervention required"
    tg_alert "🚨 *GRINCH-GRAM CRITICAL*\n❌ ALL self-heal attempts FAILED\n📝 Last logs:\n```\n${LOGS:0:800}\n```\n👨‍💻 Manual intervention required ASAP!"
    exit 1
fi
