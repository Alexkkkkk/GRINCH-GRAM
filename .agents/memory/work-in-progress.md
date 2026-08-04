---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 04.08.2026 (раунд 8)

### Выполнено

1. ✅ **feat: DCA-reduce** — сетка снижает DCA-минус, 25% прибыли каждого SELL → докупка в DCA-позицию
2. ✅ **fix: 4 бага из аудита** (коммит e74522d):
   - 🔴 Grid lock held 60-120s во время buy → фоновый поток + `_dca_reduce_lock`
   - 🔴 Double spending compound+DCA → DCA-бюджет вычисляется ДО compound
   - 🟡 `_merge_long_trades` вне `_ot_lock` → append+merge под одним lock
   - 🟡 Trade ID менялся на `grid_dca_reduce_*` → trader.py сохраняет ID старейшей позиции
   - minor: `_add_cycle_sell` guard: grinch < 1.0 → пропуск

### Деплой (04.08.2026, раунд 8)
- grid_trader.py: 289930ee867c642072faddec3cf3295c
- trader.py: 663b3bb07d295192acce37a5d6469bd1
- Контейнер: running+healthy

### Текущее состояние сетки VPS
- Профит: 28.55 TON | 14 SELL-цикла | Compound 1.28x
- SELL waiting: 21 (ближайший +18.8% от текущей цены)
- BUY waiting: 2
- DCA-позиция: 857k GRINCH @ stake 523 TON, -30.89% (минус уменьшится при первом SELL)
- TON баланс: ~81.6 TON

### Известные проблемы (из аудита, не критичные)
- /health мониторит только trader.last_tick_ts, не grid — зависание сетки невидимо
- Gas accounting inconsistency: merge считает total_buy_gas × N entries, но _dca_portfolio_value и _enriched_open_trades используют 1 buy_gas — P&L немного оптимистичен
- DCA-reduce учитывается в DCA_MAX_ENTRIES счётчике (может быть не желательно)

### Git статус
- Коммиты: 8f57283 + 4df9b4e + 9186da9 + e74522d
- GitHub push не настроен
