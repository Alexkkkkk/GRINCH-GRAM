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
| `DATABASE_URL` | PostgreSQL строка подключения |
| `ADMIN_USERNAME` | Логин для входа в дашборд |
| `ADMIN_PASSWORD` | Пароль для входа в дашборд |
| `GROQ_API_KEY` | Ключ Groq AI-советника (опционально, можно задать через дашборд) |

Без `TON_MNEMONIC` бот работает в **демо-режиме** (без реальных сделок).

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
