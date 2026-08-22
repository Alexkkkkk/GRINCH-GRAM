#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# QuantumBrain — Одноразовая настройка авто-деплоя на VPS
# Запустить один раз: bash /opt/bot/setup_autodeploy.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

BOT_DIR="/opt/bot"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
info() { echo -e "   $1"; }

echo ""
echo "════════════════════════════════════════"
echo "  QuantumBrain — Настройка авто-деплоя  "
echo "════════════════════════════════════════"
echo ""

cd "$BOT_DIR"

# ── 1. Проверяем git remote ───────────────────────────────────────────────────
echo "[1/5] Проверяем git remote..."
REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")
if [ -z "$REMOTE_URL" ]; then
    warn "Git remote origin не настроен!"
    echo -n "   Введи URL репозитория (https://github.com/...): "
    read -r REPO_URL
    git remote add origin "$REPO_URL"
fi
ok "Remote: $(git remote get-url origin)"

# ── 2. Проверяем доступ к GitHub ─────────────────────────────────────────────
echo "[2/5] Проверяем доступ к GitHub..."
if git fetch origin main --quiet 2>/dev/null; then
    ok "Git fetch работает"
else
    warn "Не могу подключиться к GitHub. Проверь:"
    info "- Интернет на VPS: curl -I https://github.com"
    info "- Если приватный репо — нужен deploy key:"
    info "  ssh-keygen -t ed25519 -C 'vps-deploy' -f ~/.ssh/deploy_key -N ''"
    info "  cat ~/.ssh/deploy_key.pub  # добавь в GitHub Settings → Deploy Keys"
    info "  git remote set-url origin git@github.com:OWNER/REPO.git"
    exit 1
fi

# ── 3. Делаем deploy.sh исполняемым ──────────────────────────────────────────
echo "[3/5] Права на скрипты..."
chmod +x "$BOT_DIR/deploy.sh"
ok "deploy.sh исполняем"

# ── 4. Настраиваем cron (каждые 3 минуты) ────────────────────────────────────
echo "[4/5] Настраиваем cron..."
CRON_JOB="*/3 * * * * /bin/bash $BOT_DIR/deploy.sh >> $BOT_DIR/deploy.log 2>&1"
CRONTAB_TMP=$(mktemp)

# Убираем старые записи этого же скрипта, добавляем новую
(crontab -l 2>/dev/null | grep -v "deploy.sh" || true; echo "$CRON_JOB") > "$CRONTAB_TMP"
crontab "$CRONTAB_TMP"
rm -f "$CRONTAB_TMP"
ok "Cron установлен: каждые 3 минуты"

# ── 5. Устанавливаем webhook-сервер (для мгновенного деплоя) ─────────────────
echo "[5/5] Настройка GitHub Webhook..."

# Проверяем, запущен ли бот
BOT_RUNNING=0
if curl -sf http://localhost/health > /dev/null 2>&1; then
    BOT_RUNNING=1
fi

if [ "$BOT_RUNNING" = "1" ]; then
    ok "Бот запущен — webhook доступен на: http://$(curl -sf ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')/webhook/github"
    echo ""
    echo "   Настрой в GitHub репозитории:"
    echo "   Settings → Webhooks → Add webhook:"
    info "Payload URL: http://$(curl -sf ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')/webhook/github"
    info "Content type: application/json"
    info "Secret: (возьми из .env → WEBHOOK_SECRET или оставь пустым)"
    info "Events: Just the push event ✓"
else
    warn "Бот не запущен — сначала запусти: cd /opt/bot && docker compose up -d --build"
    info "Потом настрой webhook в GitHub: Settings → Webhooks → Add webhook"
    info "URL: http://ВАШ_IP/webhook/github"
fi

echo ""
echo "════════════════════════════════════════"
ok "Авто-деплой настроен!"
echo ""
echo "   Как это работает:"
echo "   1. Ты редактируешь код в Replit"
echo "   2. Replit делает git push → GitHub"
echo "   3. GitHub шлёт webhook → бот на VPS"
echo "      (или cron проверяет каждые 3 мин)"
echo "   4. VPS делает git pull + docker rebuild"
echo "   5. Бот перезапускается с новым кодом ✅"
echo ""
echo "   Логи деплоя: tail -f $BOT_DIR/deploy.log"
echo "════════════════════════════════════════"
