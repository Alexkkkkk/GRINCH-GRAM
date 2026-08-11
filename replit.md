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

**Workflow на Replit настроен только для безопасной demo-проверки.** Он запускается без `EXTERNAL_DATABASE_URL` и `DATABASE_URL`, с `DEMO_MODE=true` на порту 5000. Боевой бот пользователя работает отдельно на VPS (2.27.25.126, Docker-контейнер `bot-bot-1`, реальный кошелёк через `TON_MNEMONIC`) и подключён к внешней БД (`node1.pghost.ru`).

**Не запускайте Replit workflow с `EXTERNAL_DATABASE_URL` одновременно с VPS-ботом** — это создаст конфликт двух процессов, пишущих в общие таблицы (`bot_wallets` и др.). Для реальной торговли на Replit сначала остановите VPS-бота или используйте отдельную БД.

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

## Новые инженерные движки (11.07.2026)

По решению пользователя Replit-инстанс теперь использует ОТДЕЛЬНУЮ БД
(встроенный `DATABASE_URL`, не `EXTERNAL_DATABASE_URL` — секрет с боевой
БД VPS удалён из окружения Replit). Это безопасно развязывает разработку
здесь от продакшена на VPS.

Добавлены 7 независимых read-only/консультативных движков (не встроены
в боевой `trader.py`, не исполняют реальные сделки, не мутируют
production-таблицы/боевой AI):

| Файл | Что делает | Запуск |
|---|---|---|
| `backtest.py` | Walk-forward бэктест technical-стратегии на истории GeckoTerminal | `python3 backtest.py --days 30 --min-quality B` |
| `paper_trading.py` | Виртуальная торговля на живых свечах (без денег), состояние в `paper_trading_state.json` | `python3 paper_trading.py --tick` / `--status` / `--loop` |
| `llm_agent.py` | LLM-агент (Groq, function-calling) поверх цены/бэктеста/пейпер-трейдинга/умных денег/RAG | `python3 llm_agent.py --ask "..."` |
| `rag_context.py` | Векторный поиск похожих исторических рыночных ситуаций (numpy, без внешней vector DB) | `python3 rag_context.py --days 20` |
| `explainability.py` | Переводит вывод `strategy.analyze()` в понятное объяснение решения | `python3 explainability.py --days 3` |
| `alert_rules.py` | Доп. правила алертов (крупная победа/убыток, просадка, итог бэктеста) поверх `alerts.send_alert` | вызывается из кода: `notify_trade_closed(...)` и т.д. |
| `multi_agent.py` | Экспериментальный консенсус нескольких агентов (technical/smart-money/backtest-context) — НЕ замена боевого `brain_fusion.py` | `python3 multi_agent.py --days 5` |

Важно: `ai_engine.py` (QuantumBrain) — синглтон с мутируемым состоянием
моделей; ни один из новых движков не прогоняет через него исторические
данные (это испортило бы боевые модели). Бэктест/RAG/мультиагент
используют только `strategy.py` (чистые функции без побочных эффектов).

## Пользовательские настройки

- Язык интерфейса: **русский**
- Язык общения с агентом: **русский** (всегда отвечать на русском)
