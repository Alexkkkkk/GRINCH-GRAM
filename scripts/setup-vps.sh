#!/bin/bash
set -e

echo "🚀 GRINCH-GRAM VPS Setup"
echo "========================"

apt-get update && apt-get upgrade -y

if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
fi

if ! docker compose version &> /dev/null; then
    apt-get install -y docker-compose-plugin
fi

if ! id "deployer" &>/dev/null; then
    useradd -m -s /bin/bash deployer
    usermod -aG docker deployer
    mkdir -p /home/deployer/.ssh
    touch /home/deployer/.ssh/authorized_keys
    chown -R deployer:deployer /home/deployer/.ssh
    chmod 700 /home/deployer/.ssh
    chmod 600 /home/deployer/.ssh/authorized_keys
fi

if ! grep -q "^PasswordAuthentication yes" /etc/ssh/sshd_config 2>/dev/null; then
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
    systemctl restart sshd
fi

mkdir -p /opt/bot/{backups,logs,scripts}
chown -R deployer:deployer /opt/bot

cat > /etc/logrotate.d/grinch-gram << 'ROTATE'
/opt/bot/logs/*.log {
    daily rotate 14 compress delaycompress missingok notifempty
    create 0644 root root
    sharedscripts
    postrotate
        docker kill --signal="USR1" grinch-nginx 2>/dev/null || true
    endscript
}
ROTATE

if ! command -v fail2ban-server &> /dev/null; then
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

ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

cat > /opt/bot/scripts/backup.sh << 'BACKUP'
#!/bin/bash
BACKUP_DIR="/opt/bot/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR
docker exec grinch-db pg_dump -U "${POSTGRES_USER}" "${POSTGRES_DB}" > "$BACKUP_DIR/db_$TIMESTAMP.sql" 2>/dev/null || true
tar czf "$BACKUP_DIR/data_$TIMESTAMP.tar.gz" /opt/bot/data 2>/dev/null || true
find "$BACKUP_DIR" -name "*.sql" -mtime +7 -delete
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +7 -delete
BACKUP
chmod +x /opt/bot/scripts/backup.sh

(crontab -l 2>/dev/null; echo "0 3 * * * /opt/bot/scripts/backup.sh") | crontab -

cat > /etc/cron.daily/docker-cleanup << 'CLEANUP'
#!/bin/bash
docker system prune -f
docker image prune -af --filter "until=168h" 2>/dev/null || true
CLEANUP
chmod +x /etc/cron.daily/docker-cleanup

cat >> /etc/sysctl.conf << 'SYSCTL'
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.ip_local_port_range = 1024 65535
SYSCTL
sysctl -p 2>/dev/null || true

echo ""
echo "✅ VPS Setup Complete!"
echo "Next: passwd deployer && create /opt/bot/.env && docker login ghcr.io"
