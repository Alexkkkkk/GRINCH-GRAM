---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 04.08.2026 (вечер, раунд 5)

### Выполнено

1. ✅ **Прогресс-бары** в SELL и BUY уровнях сетки (static/js/app.js + templates/index.html)
   - SELL: полоска + "+X.X% до продажи"
   - BUY: новая секция «🟢 BUY УРОВНИ», полоска + "-X.X% до закупки"

2. ✅ **4 бага в сетке исправлены** (grid_trader.py + static/js/app.js):
   - **Bug 1 (Log spam)**: `_unprofitable_warned` set — "убыточен" логируется 1 раз, не каждые 30с
   - **Bug 2 (Idle-deploy создаёт убыточные уровни)**: добавлен `_min_ton_for_profit` = gas×2/cycle_factor ≈ 31 TON при шаге 4%; base_amount = max(IDLE_LEVEL_TON, min_ton)
   - **Bug 3 (JS isBlocked)**: исправлен — проверяет `l.note.includes('idle-deploy')` вместо несуществующего статуса
   - **Bug 4 (JS "✓ достигнут" для BUY)**: при цене ниже BUY-уровня показывает "⚡ ниже уровня"

### Деплой на VPS (04.08.2026, вечер)
- app.js: 29f15553e4dbaae063b37cbecca5de96
- grid_trader.py: c33d2ab4cb9da75050034fed1d5b067c
- Контейнер перезапущен: healthy, trader=running, 287.7 MB

### Результат после рестарта
- Log spam ОСТАНОВЛЕН: 3 строки при старте → тишина (suppress-set работает)
- Новые idle-deploy уровни будут создаваться с min 31 TON (прибыльный цикл)
- Существующие 3 уровня (L-2007/2008/2009 @ 20 TON) остаются, но будут замещены правильными при следующем idle-deploy

### Незакрытые задачи
- **Task #3** — idle-deploy статистика на дашборде
- **Task #4** — _maybe_deploy_idle_grinch (SELL-аналог)

### Git статус
- Локальные коммиты НЕ запушены (GitHub auth не настроен)
- VPS_SSH_KEY работает: sshpass -p "$VPS_SSH_KEY" ssh -o StrictHostKeyChecking=no root@2.27.25.126
- Контейнер: bot-bot-1, путь: /usr/src/app/
