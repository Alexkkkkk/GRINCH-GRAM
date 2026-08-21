#!/bin/bash
set -euo pipefail

echo "🏥 GRINCH-GRAM Self-Heal"
echo "========================"

cd /opt/bot

# ── Check Docker ───────────────────────────────────────────────
echo "🔍 Checking Docker..."
if ! docker version > /dev/null 2>&1; then
    echo "❌ Docker not running, attempting restart..."
    systemctl restart docker || service docker restart || true
    sleep 3
    if ! docker version > /dev/null 2>&1; then
        echo "❌ Docker restart failed"
        exit 1
    fi
    echo "✅ Docker restarted"
fi

# ── Check containers ───────────────────────────────────────────
echo "🔍 Checking containers..."
if ! docker compose -f docker-compose.prod.yml ps | grep -q "grinch-bot"; then
    echo "⚠️ Bot container not found, starting all..."
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

# ── Fix 1: Restart bot ────────────────────────────────────────
echo "🔄 Fix 1: Restarting bot..."
docker compose -f docker-compose.prod.yml restart bot
sleep 10
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Fixed by restart"
    exit 0
fi

# ── Fix 2: Recreate bot ────────────────────────────────────────
echo "🔄 Fix 2: Recreating bot..."
docker compose -f docker-compose.prod.yml up -d --force-recreate --no-deps bot
sleep 10
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Fixed by recreate"
    exit 0
fi

# ── Fix 3: Full restart all ───────────────────────────────────
echo "🔄 Fix 3: Full restart all services..."
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d
sleep 15
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Fixed by full restart"
    exit 0
fi

# ── Fix 4: Check disk and clean ───────────────────────────────
echo "🔄 Fix 4: Checking disk space..."
FREE_GB=$(df -BG / | tail -1 | awk '{print $4}' | tr -d 'G')
if [ "$FREE_GB" -lt 2 ]; then
    echo "🧹 Low disk (${FREE_GB}GB), cleaning..."
    docker system prune -af --volumes
    docker image prune -af
fi

# ── Fix 5: Check logs for errors ──────────────────────────────
echo "🔄 Fix 5: Checking logs..."
docker logs --tail 30 grinch-bot 2>/dev/null || true

# ── Final check ───────────────────────────────────────────────
sleep 5
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Fixed"
    exit 0
else
    echo "❌ All fixes failed — manual intervention required"
    exit 1
fi
