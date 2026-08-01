---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 01.08.2026 (вечер, аудит)

### Выполнено: глубокий аудит синхронизации кода

Запущены 4 параллельных субагента (API-роуты, модули, DB-схема, Config-параметры).

#### Найдено и исправлено (3 бага):

1. ✅ **grid_trader.py:1483-1485** — `from db_store import db_store as _ds` + `_ds.trades_load_open()`
   - Исправлено: `import db_store as _ds` + `_ds.open_trades_get()` + итерация как list (не `.values()`)

2. ✅ **app.py:3180-3181** — тот же баг (grid build endpoint)
   - Исправлено аналогично

3. ✅ **db_store.py** — `tracked_amount`, `tracked_entries`, `tracked_stake` не сохранялись в DB
   - Добавлены колонки в DDL + DO $$ migrate для существующих БД
   - Обновлены wallet_snapshot_insert(), wallet_snapshots_get_recent(), wallet_snapshot_get_latest()

#### Деплой:
- Файлы закоммичены в git и задеплоены через `docker compose up -d --build`
- Контейнер бот статус: **healthy** ✅
- DB-миграция запустится автоматически при следующем подключении к БД

### Текущее состояние бота (01.08.2026 ~19:35 UTC)
- Позиция: -18.0% (-94 TON unrealized), stake=521.35 TON, amount=1101171.25 GRINCH
- DCA откат 29.1% ≥ порог 15% → готов к докупке, НО 2.295 TON ≠ min 5 TON (нет средств)
- ONLY_PROFIT_EXIT: заблокирован (убыток), держим до возврата в плюс
- Grid: active=False, sell=9, buy=5, compound=1.16x
- AI: 4 подтверждённых примера, переобучений=488

### Незакрытые вопросы:
1. **Telegram chat_id** — не настроен (задача #2)
2. **BUY no_funds** — нужен свободный TON (min 5 TON после газа и резерва)
3. **Позиция -18%** — ждём восстановления до TP=$0.000715 (+29.9%)

### Аудит — что OK (не требует исправлений):
- Все Flask-роуты имеют frontend-вызовы ✅
- SocketIO события совпадают ✅
- Все Config.* атрибуты определены ✅
- Все db_store функции вызываются корректно (кроме исправленных) ✅
- Циклические импорты — только managed (app←trader ленивый, не блокирует) ✅

### DB-схемы таблиц (критически важно — нестандартные!):
- bot_open_trades: **trade_id** (char), data (jsonb), updated_at
- bot_trades: **id** (char), data (jsonb), **closed_at**
- bot_equity: id, **ts**, **ton**, **grinch**, grinch_usd, equity_ton (прямые колонки, НЕ jsonb)
- bot_settings: **section** (char), **key** (char), **value** (text)
- bot_ai_state: **key** (char), **value** (text)
- bot_wallet_snapshots: 15 колонок + tracked_amount, tracked_entries, tracked_stake (добавлены 01.08)

### experience.json структура (изменилась!):
Ключи: version, created, **trades** (list!), open_trades, equity, **stats** (dict), **ai** (dict), **control** (dict)
НЕ плоская структура — вложенная.
