---
name: Work In Progress
description: Текущая незавершённая работа — что делалось, какие файлы затронуты, что ещё нужно сделать. Обновлять в конце каждой сессии.
---

# Work In Progress

## Как использовать

В начале каждого ответа — перечитай этот файл и обнови его прямо в ответе пользователю.  
В конце сессии — перемести сделанное в "Завершено", добавь новые задачи в "В работе".

---

## ✅ Завершено (сессия 30.07.2026)

| Что сделано | Результат |
|------------|-----------|
| **DCA state в bot_ai_state** исправлен | entries=1, stake_ton=444.9014, last_buy_price=0.00049622 |
| **open_trades** дополнена полями | dca_entries_count=1, dca_total_stake=444.9014, entry_usd=0.00149858 |
| **control.dca_target_pct** исправлен | 15.0 → 16.0 (совпадает с Config) |
| **trading_enabled** в bot_settings | 3 дублирующихся строки → 1 (True) |
| **6 устаревших lowercase-ключей** удалены из bot_settings | large_sell_dca_enabled, take_profit_pct, dca_drop_trigger_pct, min_profit_ton_abs, dca_stake_ton, trailing_stop_pct |
| **Мусорные файлы** удалены из /opt/bot | app.py.bak, sync_check.py, grid_trader.py.bak2, app.js (flat), index.html (flat), static/js/style.css |
| **.gitignore** обновлён | `*.bak`, `*.bak2` добавлены |
| Git commit `c648c40` | chore: cleanup stale bak files, flat copies, update gitignore |

---

## ✅ Завершено (сессия 29.07.2026)

| Что сделано | Результат |
|------------|-----------|
| Grid bug fix: `atr_pct, regime` → `regime, atr_pct` | TypeError исчез |
| Grid перестроена: шаг 5%→4%, 9→10 уровней | Больше сделок |
| DCA параметры в `.env`: DROP 8%, TARGET 16%, PULLBACK 10% | Агрессивнее входы |
| Grid-панель на дашборде (index.html + app.js) | Статус, уровни, GridAI-лог |
| История сделок загружается из DB при старте (trader.py) | История не пуста после рестарта |
| DB синхронизирована с .env (DROP/TARGET/PULLBACK) | Настройки не сбрасываются |

---

## ❌ Открытые задачи

| # | Приоритет | Задача |
|---|-----------|--------|
| 1 | 🔴 | **Groq API Key невалидный** — 429 rate limit, нужен новый ключ с console.groq.com → API Keys → Create |
| 2 | 🔴 | **OpenAI API Key исчерпан** — `insufficient_quota` (платный план). Нужно пополнить или убрать из провайдеров |
| 3 | 🟡 | **TELEGRAM_CHAT_ID пустой** — алерты не доходят |
| 4 | 🟠 | **Grid BUY уровни пустые** — 2.295 TON (только газ). Активируются автоматически по мере выполнения SELL уровней |
| 5 | 🟡 | **entry_usd в open_trades = None** — бот при каждом тике перезаписывает поле; нужно добавить его в trader.py при открытии сделки |

## ✅ Завершено (сессия 30.07.2026 — grid карточка)

| Что сделано | Результат |
|------------|-----------|
| **Баг renderGridPanel** исправлен | `if (!d \|\| !d.active !== false && !d.active)` → `if (!d)` — карточка рендерится в любом состоянии |
| **`/api/grid/status` в публичные пути** | Добавлен в `_PUBLIC_EXACT` — без авторизации, как /api/performance |
| **Задеплоен** `app.py` + `static/js/app.js` на VPS | docker cp + docker restart → карточка появилась |
| **VPS_SSH_KEY** обновлён в Secrets | SSH-доступ работает: `sshpass -p "$VPS_SSH_KEY" ssh root@2.27.25.126` |
| **Grid-сетка активна** | +10.296 TON прибыли, 6 циклов, 3/13 SELL выполнено, сетка готова к торговле |

---

## ✅ Завершено (сессия 30.07.2026 — синхронизация)

| Что сделано | Результат |
|------------|-----------|
| **SSH подключение** настроено | VPS_SSH_KEY в секретах Replit |
| **Сверка дашборда с БД** проведена | Все данные совпадают (22 сделки, P&L +147.9979, позиция -16.01%) |
| **Grid state** проверен | Файл /app/data/grid_state.json ВНУТРИ контейнера верный (10/13 SELL, циклов 6, +10.296 TON) |
| **Расхождение grid** оказалось ложным | Ошибка в тест-скрипте: неверные ключи + запрос вне контейнера |

## ⚠️ Важные наблюдения (30.07.2026)

- **bot_settings хранит каждый параметр отдельной строкой** с колонкой `section` (NOT NULL). Секции: `trading`, `trader_state`, `config`, `ai_advisor` и др.
- **bot_ai_state.dca перезаписывается на каждом тике** — фиксы state актуальны только до следующего тика (но это нормально, бот берёт позицию из bot_open_trades при рестарте).
- **entry_usd = None** — бот не устанавливает entry_price_usd при открытии сделки (нужно добавить в trader.py)
- **price_feed API**: в контейнере `price_feed.get()` не работает (нет атрибута); нужно `price_feed.PriceFeed` или запрашивать через /api/status (но требует auth).

---

## 📊 DCA статус (29.07.2026 ~19:18 UTC)

- **Позиция:** 873,281 GRINCH @ avg $0.000723 | -15.1%
- **Вложено:** 444.90 TON | **Сейчас:** ~378 TON  
- **Breakeven:** $0.000731 (~L4 сетки)
- **TP (новый):** 16% = $0.000839 (~L8-L9 сетки)
- **TON свободно:** 2.295 TON (только газ)

---

## 📋 Контекст проекта

- **Боевой бот:** VPS 2.27.25.126, контейнер `bot-bot-1`, порт 3000 (nginx на 80)
- **Код на VPS:** `/opt/bot/` + `.env` (права 600)
- **SSH:** `sshpass -p "$VPS_SSH_KEY" ssh -o StrictHostKeyChecking=no root@2.27.25.126`
- **Деплой:** scp → docker cp → или `docker compose up -d bot`
- **Replit используется как редактор** — превью не нужно, всё деплоится на VPS
