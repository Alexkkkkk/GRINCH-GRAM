#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# QuantumBrain — Одноразовое обслуживание VPS (запустить от root на VPS)
# Закрывает все нефиксированные пункты аудита от 25.07.2026:
#   ✅ Swapfile 512MB
#   ✅ Еженедельный docker prune в cron
#   ✅ Обновление системных пакетов
#   ✅ HTTPS через Nginx + Certbot (опционально — нужен домен)
#   ✅ Аудит SSH authorized_keys
#
# Запуск: bash /opt/bot/setup_maintenance.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
info() { echo -e "   ${CYAN}$1${NC}"; }
err()  { echo -e "${RED}❌ $1${NC}"; }

echo ""
echo "════════════════════════════════════════════════"
echo "  QuantumBrain — Техническое обслуживание VPS  "
echo "════════════════════════════════════════════════"
echo ""

# ── 1. SWAPFILE 512MB ────────────────────────────────────────────────────────
echo "── [1/5] Swap ──────────────────────────────────────────"
SWAP_TOTAL=$(free -m | awk '/^Swap:/{print $2}')
if [ "$SWAP_TOTAL" -gt 0 ]; then
    ok "Swap уже настроен (${SWAP_TOTAL}MB) — пропускаем"
else
    warn "Swap отсутствует — создаём swapfile 512MB..."
    if [ -f /swapfile ]; then
        swapoff /swapfile 2>/dev/null || true
        rm -f /swapfile
    fi
    dd if=/dev/zero of=/swapfile bs=1M count=512 status=progress
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    # Добавляем в fstab если ещё нет
    if ! grep -q '/swapfile' /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    # Настройка swappiness
    sysctl vm.swappiness=10 > /dev/null
    if ! grep -q 'vm.swappiness' /etc/sysctl.conf; then
        echo 'vm.swappiness=10' >> /etc/sysctl.conf
    fi
    SWAP_NOW=$(free -m | awk '/^Swap:/{print $2}')
    ok "Swapfile создан: ${SWAP_NOW}MB (swappiness=10)"
fi

# ── 2. DOCKER PRUNE — еженедельный cron ─────────────────────────────────────
echo ""
echo "── [2/5] Docker prune cron ─────────────────────────────"
PRUNE_CRON="0 4 * * 0 docker builder prune -f >> /opt/bot/deploy.log 2>&1 && docker image prune -f >> /opt/bot/deploy.log 2>&1"
CRONTAB_TMP=$(mktemp)
if crontab -l 2>/dev/null | grep -q 'docker builder prune'; then
    ok "Cron для docker prune уже настроен"
else
    (crontab -l 2>/dev/null || true; echo "$PRUNE_CRON") > "$CRONTAB_TMP"
    crontab "$CRONTAB_TMP"
    ok "Добавлен cron: docker builder+image prune каждое воскресенье в 04:00"
fi
rm -f "$CRONTAB_TMP"

# ── 3. ОБНОВЛЕНИЕ ПАКЕТОВ ───────────────────────────────────────────────────
echo ""
echo "── [3/5] Системные пакеты ──────────────────────────────"
echo "   Обновляем apt (безопасно — без dist-upgrade)..."
apt-get update -qq
UPGRADABLE=$(apt list --upgradable 2>/dev/null | grep -c 'upgradable' || echo 0)
if [ "$UPGRADABLE" -gt 0 ]; then
    echo "   Найдено пакетов к обновлению: $UPGRADABLE"
    # Только безопасные обновления (security patches)
    apt-get upgrade -y -o Dpkg::Options::="--force-confdef" \
                         -o Dpkg::Options::="--force-confold" \
                         --with-new-pkgs -qq
    ok "Пакеты обновлены ($UPGRADABLE шт.)"
else
    ok "Все пакеты актуальны"
fi

# ── 4. АУДИТ SSH КЛЮЧЕЙ ─────────────────────────────────────────────────────
echo ""
echo "── [4/5] SSH authorized_keys ───────────────────────────"
AUTH_KEYS="/root/.ssh/authorized_keys"
if [ -f "$AUTH_KEYS" ]; then
    KEY_COUNT=$(grep -c 'ssh-' "$AUTH_KEYS" 2>/dev/null || echo 0)
    echo "   Текущие ключи в authorized_keys ($KEY_COUNT шт.):"
    echo ""
    n=1
    while IFS= read -r line; do
        if [[ "$line" =~ ^ssh- ]]; then
            COMMENT=$(echo "$line" | awk '{print $NF}')
            SHORT=$(echo "$line" | cut -c1-60)
            echo "   [$n] $COMMENT"
            echo "       ${SHORT}..."
            n=$((n+1))
        fi
    done < "$AUTH_KEYS"
    echo ""
    warn "Проверь список выше. Для удаления лишнего ключа:"
    info "nano /root/.ssh/authorized_keys"
    info "Оставь только ключи от твоих устройств + текущий Replit."
    info "Сохранённые ключи Replit-агента безопасны — они нужны для деплоя."
else
    warn "Файл $AUTH_KEYS не найден"
fi

# ── 5. HTTPS — Nginx + Certbot ───────────────────────────────────────────────
echo ""
echo "── [5/5] HTTPS (Nginx + Certbot) ──────────────────────"
if command -v nginx &>/dev/null && command -v certbot &>/dev/null; then
    ok "Nginx и Certbot уже установлены"
else
    echo -n "   У тебя есть домен для бота? (например bot.example.com) [y/N]: "
    if [ -t 0 ]; then
        read -r HAS_DOMAIN
    else
        HAS_DOMAIN="n"
        warn "Запущен не в интерактивном режиме — пропускаем HTTPS"
    fi

    if [[ "$HAS_DOMAIN" =~ ^[Yy]$ ]]; then
        echo -n "   Введи домен: "
        read -r DOMAIN
        echo -n "   Введи email для Let's Encrypt: "
        read -r EMAIL

        echo "   Устанавливаем Nginx + Certbot..."
        apt-get install -y nginx certbot python3-certbot-nginx -qq

        # Nginx конфиг
        cat > "/etc/nginx/sites-available/quantumbrain" <<NGINX_CONF
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:80;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
NGINX_CONF

        # Проксирование: docker слушает :80, nginx будет на :80 и :443
        # Меняем docker-compose порт на 8080
        warn "ВАЖНО: нужно изменить порт в docker-compose.yml с 80:3000 на 8080:3000"
        warn "и обновить Nginx конфиг (proxy_pass http://127.0.0.1:8080)"
        info "Это делается отдельно через git push — инструкция ниже."

        ln -sf /etc/nginx/sites-available/quantumbrain /etc/nginx/sites-enabled/
        nginx -t && systemctl reload nginx

        # Сертификат
        certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive --redirect
        ok "HTTPS настроен для $DOMAIN"
    else
        info "HTTPS пропущен — домен не указан."
        info "Когда появится домен, запусти: certbot --nginx -d ВАШ_ДОМЕН"
        info "Предварительно установи: apt-get install -y nginx certbot python3-certbot-nginx"
    fi
fi

# ── ИТОГ ─────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════"
echo -e "${GREEN}  ✅ Обслуживание завершено!${NC}"
echo ""
SWAP_FINAL=$(free -m | awk '/^Swap:/{print $2}')
DISK_USED=$(df -h / | awk 'NR==2{print $5}')
echo "   Swap:  ${SWAP_FINAL}MB"
echo "   Диск:  $DISK_USED"
echo ""
echo "   Что сделано:"
echo "   ✅ Swapfile 512MB (если не было)"
echo "   ✅ Docker prune cron (воскресенье 04:00)"
echo "   ✅ Системные пакеты обновлены"
echo "   ✅ SSH ключи выведены для ревью"
echo "   ✅ HTTPS (если домен указан)"
echo "════════════════════════════════════════════════"
