# Деплой на VPS

## 1. Подключиться к серверу

```bash
ssh root@2.27.25.126
```
Используй SSH-ключ или пароль, переданный через защищённый менеджер секретов.

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
# База данных — значение задаётся только через защищённый менеджер секретов.
# EXTERNAL_DATABASE_URL=<значение из секретного хранилища>

# Секрет сессии Flask — задаётся только через защищённое хранилище.

# Дашборд
ADMIN_USERNAME=admin
ADMIN_PASSWORD=сюда_пароль

# TON кошелёк — мнемоника задаётся только через защищённое хранилище.

# Telegram уведомления (опционально) — значения задаются только через
# защищённое хранилище окружения, а не записываются в этот файл.
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
