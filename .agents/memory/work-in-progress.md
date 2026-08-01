---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 01.08.2026 (вечер)

### Выполнено: исправление всех найденных багов после аудита

#### DB-исправления (применены на VPS):
1. ✅ **failed_providers** очищены: groq=0, openai=0 → AI-советник разблокирован
2. ✅ **selected_provider** переключён на `deepseek` (имеет ключ, не падал)
3. ✅ **ai_state.dca** синхронизирован: entries=2, stake_ton=521.3526, last_buy_price=0.00046286

#### Код (задеплоен на VPS + закоммичен в git):
4. ✅ **grid_trader.py**: исправлена формула compound_bonus
   - Было: `bonus = net_ton * (new_mult - 1.0)` (чуть завышала)
   - Стало: `bonus = (net_ton - GAS_RESERVE_TON) * (new_mult - 1.0)` (точная)

### Текущее состояние бота (01.08.2026 18:37 UTC)
- Позиция: -18.0% (-94 TON unrealized), stake=521.35 TON
- DCA триггер СРАБОТАЛ (падение 17.6% > 15%) — НО 2.295 TON свободно, не хватает для докупки
- Grid: активна, tick идут, 9 SELL waiting (L1 @ 0.000429), 5 BUY no_funds (2.3 TON ≠ 15 TON min)
- AI-советник: deepseek выбран, следующий запрос через ~2 мин после рестарта
- Telegram: chat_id НЕ настроен (пользователь не ответил на форму)

### Незакрытые вопросы:
1. **Telegram chat_id** — нужен числовой ID чата. Задать через дашборд: Settings → Alerts → chat_id
2. **BUY levels no_funds** — нужен свободный TON. Невозможно без пополнения кошелька TON
3. **DCA докупка хочет войти** (откат 29.1% ≥ 15%) — нужно ~5-10 TON на кошельке
4. **Task #4 (TP)** — TP=0.000715 рассчитан от avg_entry_usd=0.000668; текущая цена 0.000551 USD, до TP +29.8%. TP пересчитывается автоматически при следующей merge.

### DB-схемы таблиц (критически важно — нестандартные!):
- bot_open_trades: **trade_id** (char), data (jsonb), updated_at
- bot_trades: **id** (char), data (jsonb), **closed_at**
- bot_equity: id, **ts**, **ton**, **grinch**, grinch_usd, equity_ton (прямые колонки, НЕ jsonb)
- bot_settings: **section** (char), **key** (char), **value** (text)
- bot_ai_state: **key** (char), **value** (text)

### experience.json структура (изменилась!):
Ключи: version, created, **trades** (list!), open_trades, equity, **stats** (dict), **ai** (dict), **control** (dict)
НЕ плоская структура — вложенная.

### Ложные срабатывания из аудита (не баги):
- total_trades=0 в grid: мой скрипт проверял несуществующий ключ; правильный — total_sell_cycles=8
- compound_bonus > total_profit: ожидаемое поведение при compound reinvesting; формула была чуть неточна (исправлено)
- BUY no_funds: нет свободного TON, не код
- Recenter не срабатывал: правильно (цена ушла на 1.1 шага, порог 2.5)
