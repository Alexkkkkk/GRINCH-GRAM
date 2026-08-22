#!/bin/bash
set -euo pipefail

BOT_DIR="/opt/bot"
SERVICE_FILE="/etc/systemd/system/auto-pull.service"
TIMER_FILE="/etc/systemd/system/auto-pull.timer"

mkdir -p "$BOT_DIR/logs"
chmod 755 "$BOT_DIR/auto-pull.sh"

cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=GRINCH-GRAM auto git-pull and redeploy
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/auto-pull.sh
StandardOutput=append:$BOT_DIR/logs/auto-pull.log
StandardError=append:$BOT_DIR/logs/auto-pull.log
UNIT

cat > "$TIMER_FILE" <<UNIT
[Unit]
Description=Run GRINCH-GRAM auto-pull every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
AccuracySec=10s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now auto-pull.timer
systemctl start auto-pull.service

echo "✅ Auto-pull timer installed"
echo "   Check: systemctl status auto-pull.timer"
echo "   Logs:  tail -f $BOT_DIR/logs/auto-pull.log"
