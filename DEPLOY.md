# Деплой на VPS

## 1. Подключиться к серверу

```bash
ssh root@2.27.25.126
```
Введи пароль из письма (панель управления).

---

## 2. Установить Git и склонировать код

```bash
apt-get update && apt-get install -y git
git clone https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git /opt/bot
cd /opt/bot
```

> Либо загрузи файлы через scp (см. ниже).

---

## 3. Создать файл .env с секретами

```bash
nano /opt/bot/.env
```

Вставить и заполнить:

```env
# База данных (та же что на Replit, или новая)
EXTERNAL_DATABASE_URL=postgresql://USER:PASS@HOST:PORT/DBNAME

# Секрет сессии Flask (любая длинная строка)
SESSION_SECRET=сюда_длинную_случайную_строку_50_символов

# Дашборд
ADMIN_USERNAME=admin
ADMIN_PASSWORD=сюда_пароль

# TON кошелёк (24 слова через пробел)
TON_MNEMONIC=слово1 слово2 ... слово24

# Telegram уведомления (опционально)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Сохранить: Ctrl+O → Enter → Ctrl+X

---

## 4. Запустить бота

```bash
cd /opt/bot
docker compose up -d --build
```

Первый запуск: ~5–10 минут (скачивает Python-пакеты).

---

## 5. Проверить что работает

```bash
# Логи в реальном времени
docker compose logs -f

# Статус контейнера
docker compose ps

# Health check
curl http://localhost/health
```

---

## 6. Открыть в браузере

```
http://2.27.25.126
```

---

## Полезные команды

```bash
# Перезапуск
docker compose restart

# Остановить
docker compose down

# Обновить код и перезапустить
git pull && docker compose up -d --build

# Посмотреть RAM
docker stats
```

---

## Загрузить файлы без Git (альтернатива шагу 2)

С компьютера (или из Replit Shell):
```bash
scp -r /path/to/bot root@2.27.25.126:/opt/bot
```
