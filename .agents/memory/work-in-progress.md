---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 01.08.2026

### Выполнено: полный аудит VPS

Проведён аудит трёх направлений: БД, код/состояние бота, сетка.

#### Ключевые находки

**🔴 КРИТИЧНО:**
1. `telegram_chat_id` пустой → Telegram-алерты не работают
2. openai (сбой 01.08 13:37) и groq (сбой 31.07 09:28) в failed_providers → AI-советник не работает
3. Открытая позиция -18.4% (-95.76 TON), TP=0.000715 (+83% от текущей ~0.000391)

**🟡 ВАЖНО:**
4. Grid: total_trades=0 при total_sell_cycles=8 (баг счётчика)
5. Grid: compound_bonus=29.74 TON > total_profit=12.91 TON (аномалия)
6. Grid: все 5 BUY-уровней no_funds, перецентровка ни разу не срабатывала
7. Stake расхождение: open_trades=521.35 TON vs ai_state.dca=444.9 TON (76 TON)
8. DCA_MAX_ENTRIES=1 достигнут, новый вход невозможен

#### Состояние бота (01.08.2026 ~15:10 UTC)
- Позиция: dedust_1785565235, BUY, 1 101 171 GRINCH, открыта 28.07 @ 0.000668 TON
- avg_price (DCA): 0.000463 TON, текущая: ~0.000391 TON
- stake: 521.35 TON, value_now: 430.25 TON
- ONLY_PROFIT_EXIT: заблокирован, ждёт восстановления
- Grid: активна, тик #858, 9 SELL waiting, 5 BUY no_funds
- AI stats: 33 сделки, 20 побед (60.6%), total PnL=160.91 TON, Sharpe=1.72

#### Предложенные задачи
- Task #2: Восстановить Telegram-алерты и AI-советника
- Task #3: Починить баги сетки
- Task #4: Разобраться с открытой позицией (TP, stake)

### Схемы таблиц (важно — не стандартные!)
- bot_open_trades: **trade_id** (char), data (jsonb), updated_at
- bot_trades: **id** (char), data (jsonb), **closed_at**
- bot_equity: id (bigint), **ts**, **ton**, **grinch**, grinch_usd, equity_ton (прямые колонки, НЕ jsonb!)
- bot_ticks: id (bigint), **ts**, data (jsonb)
- bot_settings: **section** (char), **key** (char), **value** (text) — НЕ section+data!
- bot_ai_state: **key** (char), **value** (text)
- bot_wallets: **address** (char), data (jsonb)
- bot_wallet_snapshots: id, ts, ton_balance, grinch_balance, ... (прямые колонки)
- user_wallets: id, token, name, ton_address, virtual_ton_balance, ... (много колонок)

### experience.json структура (изменилась!)
Ключи: version, created, **trades** (list!), open_trades, equity, stats, ai, control
НЕ плоская структура — вложенная с секциями.
