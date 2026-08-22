#!/bin/bash
# Установка systemd таймера для авто-обновления бота с GitHub
# Запускать на VPS: sudo bash /opt/bot/scripts/setup-autopull.sh

set -euo pipefail

BOT_DIR="/opt/bot"
USER_NAME="${SUDO_USER:-$USER}"

mkdir -p "$BOT_DIR/logs"

# Копируем unit-файлы в systemd пользователя
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

cp "$BOT_DIR/scripts/auto-pull.service" "$SYSTEMD_DIR/"
cp "$BOT_DIR/scripts/auto-pull.timer" "$SYSTEMD_DIR/"

# Заменяем %h на реальный home
sed -i "s|%h|$HOME|g" "$SYSTEMD_DIR/auto-pull.service"

# Перезагружаем systemd
systemctl --user daemon-reload

# Включаем и запускаем таймер
systemctl --user enable auto-pull.timer
systemctl --user start auto-pull.timer

# Разрешаем пользовательские systemd сервисы работать без логина
sudo loginctl enable-linger "$USER_NAME" 2>/dev/null || true

echo "✅ Auto-pull таймер установлен!"
echo "   Проверка: systemctl --user status auto-pull.timer"
echo "   Логи:     tail -f $BOT_DIR/logs/auto-pull.log"
