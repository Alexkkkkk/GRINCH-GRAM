---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 03.08.2026 (Grid↔DCA координация)

### Выполнено: исправлена координация Grid ↔ DCA в grid_trader.py

#### Проблема:
Grid и DCA-трейдер используют одни и те же GRINCH — без защиты сетка могла
продавать монеты, которые DCA держит до своей TP (ONLY_PROFIT_EXIT).

#### Три изменения в grid_trader.py (коммит cb90686):

1. **`_get_dca_reserved_grinch()`** — новый метод, читает `open_trades` из DB
   и возвращает суммарное кол-во GRINCH в открытых DCA-позициях.

2. **`build_grid()` — свободный GRINCH**:
   - `free_grinch = wallet_balance - dca_reserved`
   - SELL-уровни строятся только из `free_grinch` (не из полного баланса)

3. **`_execute_sell()` — runtime-guard**:
   - Перед свопом: `free_g = wallet_grinch - dca_reserved`
   - Если `free_g < level.amount_grinch` → статус `skipped_dca`, продажа отменена
   - Логирует предупреждение с цифрами

4. **Восстановление `skipped_dca`** (в tick-loop):
   - Восстанавливается в `waiting` когда DCA закрывает позицию (GRINCH освобождается)
   - Или при откате цены ниже триггера

#### Статус:
- Коммит: cb90686 (local main)
- Push в GitHub: НЕ выполнен (нет SSH deploy key)
- Нужен деплой на VPS: `scp grid_trader.py` + `docker cp`

### Текущее состояние бота (03.08.2026 08:04):
- Позиция DCA: 1 101 171 GRINCH, стейк 521.35 TON, вход @ $0.000883
- Текущая цена: $0.000838 (−5.08%)
- ONLY_PROFIT_EXIT активен, цель: +7.07% → $0.000946
- Grid: active=False
- Свободный TON: 307.575 (газ)
- GRINCH на кошельке: 400 580.87

### Незакрытые вопросы:
1. **GitHub deploy key** — не настроен в этом Replit → git push невозможен; деплой через docker cp
2. **Telegram chat_id** — не настроен
3. **ADMIN_PASSWORD** — не задан (дашборд открыт без пароля)
4. **Grid активация** — можно активировать ПОСЛЕ закрытия DCA-позиции или выделить её на свободный TON
