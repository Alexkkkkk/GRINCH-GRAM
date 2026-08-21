#!/bin/bash
set -euo pipefail

echo "🏥 Local Health Check"
echo "====================="

# Health endpoint
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
    echo "✅ Bot health: OK"
else
    echo "❌ Bot health: FAILED"
fi

# Container status
echo "--- Containers ---"
docker compose -f /opt/bot/docker-compose.prod.yml ps

# Resource usage
echo "--- Resources ---"
echo "Disk: $(df -h / | tail -1 | awk '{print $5}') used"
echo "Memory: $(free | grep Mem | awk '{printf "%.1f%%", $3/$2 * 100.0}') used"

# Image versions
echo "--- Images ---"
docker images ghcr.io/alexkkkkk/grinch-gram --format "{{.Tag}} | {{.Size}}" | head -5
