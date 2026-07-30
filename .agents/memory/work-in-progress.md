---
name: Work In Progress
description: ЧТО ДЕЛАЛОСЬ В ПРОШЛОЙ СЕССИИ — файлы, незавершённые задачи, следующие шаги. Читать в начале каждой сессии.
---

# Work In Progress

## Последняя сессия: 2026-07-30 (вечер)

### Что сделано
- Обновлён секрет VPS_SSH_KEY (новый пароль)
- **ИСПРАВЛЕН БАГ**: `get_shared_balance()` возвращает `{"TON":...,"GRINCH":...}` (заглавные), но код везде читал строчные `ton/grinch` → 0 → `no_funds` на BUY-уровнях сетки
  - `grid_trader.py` `_get_balances()`: строчные → заглавные ключи
  - `app.py` `api_grid_build`: строчные → заглавные ключи
- **ДОБАВЛЕНА АВТО-АКТИВАЦИЯ BUY**: `_need_rebuild()` теперь проверяет «все BUY-уровни no_funds + TON достаточно» и запускает перестройку без кулдауна
- Docker образ пересобран и задеплоен на VPS
- Все 5 BUY-уровней стали `waiting` по 17.659 TON каждый

### Текущее состояние VPS (16:40 UTC 30.07.2026)
- bot-bot-1: healthy | bot-nginx-1: healthy
- Grid: **АКТИВНА**, 5 BUY (waiting, 17.659 TON/ур) + 10 SELL waiting + 3 filled
- Grid profit: 10.30 TON реализовано + 16.70 TON compound (1.12x)
- DCA: 880,702 GRINCH | вложено 450.29 TON | сейчас ~333 TON | -26%
- ONLY_PROFIT_EXIT: ждём +44% до $0.000773 (сейчас $0.000536)
- Liquidator: активен, та же цель
- TON на кошельке: 93.29 TON

### Коммиты (origin/main)
- `262d19c` — fix(grid): rebuild BUY levels when all no_funds but TON available; fix uppercase key in _get_balances
- `48217ae` — fix: grid build/get_balances use uppercase TON/GRINCH keys from get_shared_balance

### Открытые задачи
- Task #2 (PROPOSED): Telegram-алерт когда grid BUY levels застряли на no_funds — частично закрыт (авто-ребилд добавлен), остался только алерт
- Task #3 (PROPOSED): Fix missing trade profit/price data in database (profit_ton, profit_pct etc. NULL в bot_trades)

### Известные проблемы
- ⚠️ GridAI не обучен (9 примеров, ошибка `'str' ** int` в grid_ai.py при обучении)
- Replit workflow сломан (не критично — Replit используется только как редактор)
