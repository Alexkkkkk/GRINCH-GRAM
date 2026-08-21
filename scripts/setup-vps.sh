#!/bin/bash
set -euo pipefail

echo "🚀 GRINCH-GRAM VPS Setup"
echo "========================"

# ── System Update ──────────────────────────────────────────────
echo "📦 Updating system..."
apt-get update && apt-get upgrade -y

# ── Install Docker ─────────────────────────────────────────────
if ! command -v docker &> /dev/null; then
    echo "📦 Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    echo "✅ Docker already installed: $(docker --version)"
fi

if ! docker compose version &> /dev/null; then
    apt-get install -y docker-compose-plugin
fi

# ── Create deployer user ────────────────────────────────────────
if ! id "deployer" &>/dev/null; then
    echo "👤 Creating deployer user..."
    useradd -m -s /bin/bash deployer
    usermod -aG docker deployer
    mkdir -p /home/deployer/.ssh
    touch /home/deployer/.ssh/authorized_keys
    chown -R deployer:deployer /home/deployer/.ssh
    chmod 700 /home/deployer/.ssh
    chmod 600 /home/deployer/.ssh/authorized_keys
    echo "✅ deployer user created"
else
    echo "✅ deployer user already exists"
fi

# ── Enable password auth for GitHub Actions ─────────────────────
echo "🔧 Configuring SSH..."
if ! grep -q "^PasswordAuthentication yes" /etc/ssh/sshd_config 2>/dev/null; then
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
    systemctl restart sshd
    echo "✅ Password authentication enabled"
fi

# ── Directory structure ─────────────────────────────────────────
echo "📁 Creating directories..."
mkdir -p /opt/bot/{backups,logs,scripts,nginx/ssl}
chown -R deployer:deployer /opt/bot

# ── Log rotation ────────────────────────────────────────────────
cat > /etc/logrotate.d/grinch-gram << 'ROTATE'
/opt/bot/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 root root
    sharedscripts
    postrotate
        docker kill --signal="USR1" grinch-nginx 2>/dev/null || true
    endscript
}
ROTATE

# ── Fail2ban ────────────────────────────────────────────────────
if ! command -v fail2ban-server &> /dev/null; then
    echo "📦 Installing fail2ban..."
    apt-get install -y fail2ban
fi
cat > /etc/fail2ban/jail.local << 'F2B'
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 5
bantime = 3600
F2B
systemctl restart fail2ban

# ── Firewall ────────────────────────────────────────────────────
echo "🔥 Configuring firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# ── Backup script ───────────────────────────────────────────────
cat > /opt/bot/scripts/backup.sh << 'BACKUP'
#!/bin/bash
set -euo pipefail
BACKUP_DIR="/opt/bot/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

# Database backup
docker exec grinch-db pg_dump -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-grinch}" > "$BACKUP_DIR/db_$TIMESTAMP.sql" 2>/dev/null || true

# App data backup
tar czf "$BACKUP_DIR/data_$TIMESTAMP.tar.gz" -C /opt/bot data 2>/dev/null || true

# Cleanup old backups (keep 7 days)
find "$BACKUP_DIR" -name "*.sql" -mtime +7 -delete
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +7 -delete

# Cleanup old failed images (keep 3 days)
docker images --format "{{.Repository}}:{{.Tag}}" | grep ":failed-" | while read img; do
    docker rmi "$img" 2>/dev/null || true
done
BACKUP
chmod +x /opt/bot/scripts/backup.sh

# Cron for backups
(crontab -l 2>/dev/null; echo "0 3 * * * /opt/bot/scripts/backup.sh") | crontab -

# ── Auto-cleanup Docker ─────────────────────────────────────────
cat > /etc/cron.daily/docker-cleanup << 'CLEANUP'
#!/bin/bash
docker system prune -f
docker image prune -af --filter "until=168h" 2>/dev/null || true
CLEANUP
chmod +x /etc/cron.daily/docker-cleanup

# ── Sysctl tuning ───────────────────────────────────────────────
cat >> /etc/sysctl.conf << 'SYSCTL'
# Network performance
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.ip_local_port_range = 1024 65535
# Security
net.ipv4.tcp_syncookies = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
SYSCTL
sysctl -p 2>/dev/null || true

# ── Docker config for GHCR (if GHCR_PAT provided later) ─────────
cat > /opt/bot/.docker-config-template << 'DOCKERCONFIG'
{
  "auths": {
    "ghcr.io": {
      "auth": "BASE64_USERNAME_PASSWORD"
    }
  }
}
DOCKERCONFIG

echo ""
echo "================================"
echo "✅ VPS Setup Complete!"
echo "================================"
echo ""
echo "🔐 SSH ACCESS:"
echo "  Password: VPS_PASSWORD in GitHub Secrets"
echo "  SSH Key:  bash /opt/bot/scripts/generate-ssh-key.sh deployer"
echo ""
echo "📋 NEXT STEPS:"
echo "  1. Set deployer password: passwd deployer"
echo "  2. Create .env:         nano /opt/bot/.env"
echo "  3. Login to GHCR:       echo TOKEN | docker login ghcr.io -u USER --password-stdin"
echo "  4. Start services:      cd /opt/bot && docker compose -f docker-compose.prod.yml up -d"
echo ""
echo "🚀 Push to GitHub main → auto-deploy starts!"
echo "================================"
