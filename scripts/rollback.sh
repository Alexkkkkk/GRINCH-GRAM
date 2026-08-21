#!/bin/bash
set -e
cd /opt/bot
echo "🔄 Rolling back..."
if ! docker image inspect ghcr.io/alexkkkkk/grinch-gram:previous > /dev/null 2>&1; then
    echo "❌ No previous image!"; exit 1
fi
docker tag ghcr.io/alexkkkkk/grinch-gram:latest "ghcr.io/alexkkkkk/grinch-gram:failed-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
docker tag ghcr.io/alexkkkkk/grinch-gram:previous ghcr.io/alexkkkkk/grinch-gram:latest
docker compose -f docker-compose.prod.yml up -d --no-deps bot
sleep 5
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Rollback OK!"
else
    echo "❌ Rollback failed!"; exit 1
fi
