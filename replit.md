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

Проект переимпортирован повторно (11.07.2026) — зависимости снова были потеряны, `uv sync` их восстановил. Workflow **Start application** (`python3 main.py`, порт 5000) запущен и работает, дашборд открывается, БД (внешний PostgreSQL pghost.ru) подключена и проверена (12 таблиц), демо-режим — `TON_MNEMONIC` не задан. Для реальной торговли нужно добавить секрет `TON_MNEMONIC` через Replit Secrets.

⚠️ Пользователь однажды прислал в чат пароль root от внешнего VPS (2.27.25.126) в открытом виде — он НЕ был использован и не сохранён агентом. Если понадобится SSH-доступ к тому серверу, пароль должен быть сначала сменён и передан только через Replit Secrets, не через чат.

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
