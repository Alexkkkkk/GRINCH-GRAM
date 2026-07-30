---
name: Work In Progress
description: ЧТО ДЕЛАЛОСЬ В ПРОШЛОЙ СЕССИИ — файлы, незавершённые задачи, следующие шаги. Читать в начале каждой сессии.
---

# Work In Progress

## Последняя сессия: 2026-07-30

### Что сделано
- Восстановлено SSH-подключение к VPS (обновлён секрет VPS_SSH_KEY)
- Исправлен баг: `completed_fills` обнулялся при graceful shutdown контейнера
  - `grid_trader.py` `from_dict()`: авто-миграция из filled sell_levels когда completed_fills пуст
  - Закоммичено в origin/main (`3fd23bb`), задеплоено на VPS
- Проведён полный аудит VPS и БД

### Текущее состояние VPS (08:41 UTC 30.07.2026)
- DCA позиция: 885,414 GRINCH, вложено 450.19 TON, сейчас ~335 TON (-26.3%)
- ONLY_PROFIT_EXIT активен, ждём +43% до цели $0.000773
- Liquidator: цель $0.000773, до продажи +43.1%
- DCA_MAX_ENTRIES=3 лимит достигнут, докупок нет
- TON баланс на кошельке: **2.29 TON** (критически мало — нет денег для докупок)
- Grid: 3 SELL в completed_fills, total_profit=10.296 TON
- Peak equity: 600.78 TON | Drawdown: 9.67% от пика | Sharpe: 1.72
- Контейнеры: bot-bot-1 (healthy), bot-nginx-1 (healthy)
- Health: degraded (tick_age~92s, порог 180s)
- Groq advisor: провалился (~3 часа назад, rate-limit)

### НАЙДЕННЫЕ БАГИ (аудит)
1. **КРИТИЧНО**: `profit_ton`, `profit_pct`, `avg_price`, `close_price`, `dca_entries_count` не пишутся в `bot_trades` и `bot_open_trades` (MISSING в jsonb). Реальный учёт в `experience.json` + `ai_state` работает корректно (147.997 TON, 20/22 wins).
2. **bot_ai_examples**: всего 4 строки — AI обучается через experience.json, не через DB-таблицу.
3. **Конфликт trading_enabled**: trader_state="False", trading="True".
4. **Untracked файлы** в /opt/bot: app.js, index.html.

### Открытые задачи
- Task #3: Fix missing trade profit and price data in the database
- Task #4: Raise DCA entry limit (нужно пополнить TON на кошельке сначала)
