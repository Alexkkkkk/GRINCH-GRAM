---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 03.08.2026

### Выполнено: 4 исправления

1. ✅ **brain_fusion.py:41** — добавлен `from config import Config` глобально
   - Root cause `[Push] Ошибка: name 'Config' is not defined`
   - Файл использовал Config на строке 303 в update_wallet(), но никогда не импортировал его глобально (только `_Cfg` внутри функции на 409)

2. ✅ **app.py:push_updates** — улучшено логирование: traceback добавлен в `[Push] Ошибка`

3. ✅ **grid_trader.py** — добавлен метод `reset_error_levels(level_ids=None)`
   - Сбрасывает error → waiting; уровни без баланса → skipped_small
   - API endpoint: `POST /api/grid/reset_errors` с body `{"level_ids": [1,4,5]}`

4. ✅ **templates/index.html:2689** — BUY-трейды в "Последние сделки" теперь не показывают "+0.0000 TON"
   - `profit = (isBuy && t.status !== 'closed') ? null : ...`
   - Причина бага: pnl=0.0 не является null, `??` его не заменяет

### Состояние git:
- Коммит: `fix: brain_fusion Config NameError + grid reset_errors API + BUY pnl display`
- **Push на GitHub НЕ выполнен** — UNAUTHENTICATED (нужно подключить GitHub-аккаунт в Replit или пушнуть вручную через Replit Git panel)
- VPS НЕ подхватит изменения пока не будет push в origin/main

### Незакрытые вопросы:
1. **GitHub push** — залогиниться в Replit Git panel и запушить
2. **L1 error-уровень** — после деплоя вызвать `POST /api/grid/reset_errors {"level_ids":[1,4,5]}`
3. **Telegram chat_id** — не настроен
4. **BUY no_funds** — нужен свободный TON (min 5 TON)
