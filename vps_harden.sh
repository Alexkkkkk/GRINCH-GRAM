#!/bin/bash
# vps_harden.sh — Укрепление VPS на уровне ОС
# Запускать от root ОДИН РАЗ: bash /opt/bot/vps_harden.sh
# После выполнения SSH остаётся доступен ТОЛЬКО по ключу.

set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
ok()  { echo -e "${GRN}✅ $*${NC}"; }
err() { echo -e "${RED}❌ $*${NC}"; }
inf() { echo -e "${YLW}ℹ️  $*${NC}"; }

echo ""
echo "════════════════════════════════════════"
echo "  🛡️  VPS Security Hardening Script"
echo "════════════════════════════════════════"
echo ""

# ── 1. UFW Firewall ────────────────────────────────────────────────────────
inf "Настраиваем UFW (firewall)..."
apt-get install -y ufw -q
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   comment "SSH"
ufw allow 80/tcp   comment "HTTP (nginx)"
ufw allow 443/tcp  comment "HTTPS"
# Блокируем прямой доступ к порту бота (только через nginx)
ufw deny 3000/tcp  comment "Bot port — direct access blocked"
ufw --force enable
ok "UFW: SSH+HTTP+HTTPS открыты, порт 3000 закрыт снаружи"

# ── 2. fail2ban ────────────────────────────────────────────────────────────
inf "Настраиваем fail2ban..."
apt-get install -y fail2ban -q

cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
# Бан на 1 час по умолчанию
bantime   = 3600
findtime  = 600
maxretry  = 5
backend   = auto

[sshd]
enabled   = true
port      = 22
# SSH: 3 неудачи за 10 мин → бан на 24 часа
maxretry  = 3
bantime   = 86400
findtime  = 600

[nginx-limit-req]
enabled   = true
filter    = nginx-limit-req
port      = http,https
logpath   = /var/log/nginx/error.log
maxretry  = 10
bantime   = 3600

[nginx-botsearch]
enabled   = true
filter    = nginx-botsearch
port      = http,https
logpath   = /var/log/nginx/access.log
maxretry  = 5
bantime   = 86400
EOF

# Фильтр для nginx DDoS (rate-limit 429)
cat > /etc/fail2ban/filter.d/nginx-limit-req.conf << 'EOF'
[Definition]
failregex = limiting requests, excess:.* by zone.*client: <HOST>
ignoreregex =
EOF

systemctl enable fail2ban
systemctl restart fail2ban
ok "fail2ban: SSH + nginx-ratelimit + botsearch джейлы активны"

# ── 3. Kernel: защита от SYN-flood, IP-spoofing ───────────────────────────
inf "Применяем kernel-параметры защиты..."
cat > /etc/sysctl.d/99-security.conf << 'EOF'
# SYN flood protection
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 2048
net.ipv4.tcp_synack_retries = 2
net.ipv4.tcp_syn_retries = 5

# Отклонять широковещательный ICMP
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1

# IP spoofing protection (reverse path filter)
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

# Отключить IP source routing
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0

# Не принимать ICMP redirects
net.ipv4.conf.all.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0

# Log martian packets
net.ipv4.conf.all.log_martians = 1

# Увеличить очередь соединений
net.core.somaxconn = 1024
net.ipv4.tcp_max_tw_buckets = 1440000
EOF

sysctl -p /etc/sysctl.d/99-security.conf > /dev/null
ok "Kernel sysctl: SYN-flood + IP-spoofing защита применена"

# ── 4. SSH hardening ──────────────────────────────────────────────────────
inf "Укрепляем SSH конфигурацию..."
SSHD_CFG="/etc/ssh/sshd_config"
cp "$SSHD_CFG" "${SSHD_CFG}.bak.$(date +%Y%m%d_%H%M%S)"

_set_ssh() {
    local key=$1 val=$2
    if grep -qE "^#?${key}" "$SSHD_CFG"; then
        sed -i "s|^#*${key}.*|${key} ${val}|" "$SSHD_CFG"
    else
        echo "${key} ${val}" >> "$SSHD_CFG"
    fi
}

_set_ssh PasswordAuthentication   no
_set_ssh PermitEmptyPasswords     no
_set_ssh PubkeyAuthentication     yes
_set_ssh MaxAuthTries             3
_set_ssh LoginGraceTime           30
_set_ssh X11Forwarding            no
_set_ssh AllowAgentForwarding     no
_set_ssh AllowTcpForwarding       no
_set_ssh PrintMotd                no
_set_ssh ClientAliveInterval      300
_set_ssh ClientAliveCountMax      2

# Проверяем конфиг перед применением
sshd -t && systemctl reload sshd
ok "SSH: парольный вход выключен, MaxAuthTries=3, тайм-ауты настроены"

# ── 5. Добавить публичный ключ Replit ─────────────────────────────────────
REPLIT_KEY_FILE="/opt/bot/replit_key.pub"
if [ -f "$REPLIT_KEY_FILE" ]; then
    mkdir -p ~/.ssh
    chmod 700 ~/.ssh
    touch ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    REPLIT_KEY=$(cat "$REPLIT_KEY_FILE")
    if ! grep -qF "$REPLIT_KEY" ~/.ssh/authorized_keys; then
        echo "$REPLIT_KEY" >> ~/.ssh/authorized_keys
        ok "SSH: публичный ключ Replit добавлен в authorized_keys"
    else
        ok "SSH: публичный ключ Replit уже присутствует"
    fi
else
    err "Файл $REPLIT_KEY_FILE не найден — ключ не добавлен"
fi

# ── 6. Автообновления безопасности ───────────────────────────────────────
inf "Включаем автообновления безопасности..."
apt-get install -y unattended-upgrades -q
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
ok "Автообновления безопасности включены"

# ── 7. Итог ───────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo -e "${GRN}  ✅ Укрепление завершено!${NC}"
echo "════════════════════════════════════════"
echo ""
echo "Что сделано:"
echo "  🔥 UFW: открыты только 22, 80, 443"
echo "  🔒 fail2ban: SSH (бан 24ч после 3 попыток) + nginx rate-limit"
echo "  🧠 Kernel: SYN-flood + IP-spoofing защита"
echo "  🔑 SSH: только ключи, MaxAuthTries=3"
echo "  🔄 Автообновления безопасности: включены"
echo ""
echo "Следующий шаг — пересобрать Docker-стек (добавлен nginx):"
echo "  cd /opt/bot && docker compose up -d --build"
echo ""
inf "Проверить состояние: ufw status && fail2ban-client status"
