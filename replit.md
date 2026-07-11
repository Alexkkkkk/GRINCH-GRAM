# QuantumBrain — TON/GRINCH Trading Bot

## Обзор проекта

Автоматический торговый бот для пары GRINCH/TON на блокчейне TON через DEX DeDust.  
Включает веб-дашборд (Flask + SocketIO), AI-движок (6 моделей sklearn/XGBoost), мультипользовательскую платформу (TonConnect) и систему мониторинга кошельков.

## Стек

- **Backend:** Python 3, Flask, Flask-SocketIO, Gunicorn, Eventlet
- **AI:** scikit-learn (RF, ET, GB, HGB), XGBoost, MLP — QuantumBrain v4
- **Блокчейн:** pytoniq, dedust SDK, TonCenter API
- **БД:** PostgreSQL (основная) + JSON-файлы (резервный fallback)
- **Данные:** DexScreener, GeckoTerminal, CoinGecko

## Как запустить

```bash
python3 main.py
```

Или через workflow **Start application** (порт 5000).

## Ключевые переменные окружения

| Переменная | Описание |
|---|---|
| `SESSION_SECRET` | Секрет Flask-сессий |
| `TON_MNEMONIC` | Мнемоника кошелька TON (для реальной торговли) |
| `DATABASE_URL` | PostgreSQL строка подключения (Replit, fallback) |
| `EXTERNAL_DATABASE_URL` | Строка подключения к внешней PostgreSQL (приоритетна над `DATABASE_URL`) |
| `ADMIN_USERNAME` | Логин для входа в дашборд |
| `ADMIN_PASSWORD` | Пароль для входа в дашборд |
| `GROQ_API_KEY` | Ключ Groq AI-советника (опционально, можно задать через дашборд) |

Без `TON_MNEMONIC` бот работает в **демо-режиме** (без реальных сделок).

## Статус на Replit

⛔ **Workflow на Replit ОСТАНОВЛЕН И УДАЛЁН по просьбе пользователя (11.07.2026).** Причина: у пользователя уже есть боевой бот на отдельном VPS (2.27.25.126, Docker-контейнер `bot-bot-1`, реальный кошелёк через `TON_MNEMONIC`), и он подключён к ТОЙ ЖЕ внешней БД (`node1.pghost.ru`), что и Replit-инстанс. Оба процесса одновременно писали в общие таблицы (`bot_wallets` и др.), что вызывало `statement timeout` и рассинхронизацию отображаемых данных (DCA-цикл, баланс кошелька) между Replit и VPS.

**Не пересоздавайте и не запускайте workflow на этом проекте**, если он снова параллельно подключится к `EXTERNAL_DATABASE_URL` — это создаст тот же конфликт с боевым VPS-ботом. Если нужно снова запускать бота именно на Replit, нужно либо остановить бота на VPS, либо развести их на разные БД.

⚠️ В переписке и загруженных файлах несколько раз засветился пароль root от VPS в открытом виде — агент его не хранит, но пользователю стоит сменить пароль root и пароль от панели vm.senko.digital.

## Структура

- `main.py` — точка входа
- `app.py` — Flask-приложение, роуты, SocketIO-события
- `trader.py` — основной торговый движок
- `ai_engine.py` — QuantumBrain AI (обучение и предсказания)
- `dedust_client.py` — клиент DeDust DEX (свапы TON↔GRINCH)
- `config.py` — все настраиваемые параметры
- `db_store.py` — работа с PostgreSQL (7 таблиц)
- `experience_manager.py` — AI-адаптация параметров по опыту
- `wallet_tracker.py` — мониторинг кошельков умных денег
- `deposit_monitor.py` — мониторинг депозитов пользователей
- `templates/` — HTML-шаблоны дашборда
- `static/` — JS/CSS ресурсы

## Пользовательские настройки

- Язык интерфейса: **русский**
- Язык общения с агентом: **русский** (всегда отвечать на русском)
