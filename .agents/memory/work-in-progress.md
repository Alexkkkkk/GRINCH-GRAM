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
  - Закоммичено в origin/main (`3fd23bb`), задеплоено на VPS через git pull + docker cp + SIGKILL restart
  - Проверено: после рестарта completed_fills = 3 уровня без ручного вмешательства

### Текущее состояние VPS
- DCA позиция: 885k GRINCH, вложено 450 TON, сейчас ~377 TON (-16.2%)
- ONLY_PROFIT_EXIT активен, ждём восстановления (+25% до цели ~$0.000773)
- Grid: 3 SELL в completed_fills (L1 +0.934, L2 +2.494, L111 +1.600), total_profit=10.296 TON, 6 циклов
- Контейнеры: bot-bot-1 (healthy), bot-nginx-1 (healthy)
- tonapi.io периодически отдаёт 429 (пул кэшируется, последний рабочий курс используется)

### Открытые задачи
- Нет активных задач
