---
name: Work In Progress
description: ЧТО ДЕЛАЛОСЬ В ПРОШЛОЙ СЕССИИ — файлы, незавершённые задачи, следующие шаги. Читать в начале каждой сессии.
---

# Work In Progress

## Последняя сессия: 2026-07-30 (вечер)

### Что сделано
- Обновлён секрет VPS_SSH_KEY (новый пароль)
- **ИСПРАВЛЕН grid_trader.py** — «только в плюс»: убран AI-гейт перед BUY, AI масштабирует только размер
- **ИСПРАВЛЕН grid_ai.py** — обучение: safe_atr float-коерция, обработка одного класса
- **БЭКФИЛЛ bot_trades** — заполнены open_price и profit_pct для всех 22 сделок
  - Скрипт: `/tmp/backfill_profit.py` (уже выполнен, удалять не нужно)
  - Формула: open_price = close_price / (1 + profit_ton/stake_ton)
  - profit_pct = profit_ton / stake_ton * 100

### Текущее состояние VPS (17:10 UTC 30.07.2026)
- bot-bot-1: healthy | bot-nginx-1: healthy
- Grid: АКТИВНА, тики идут (last_tick age ~7s)
  - 5 BUY waiting (17.659 TON/ур, от -3.8% до -17.8% от центра)
  - 10 SELL waiting
  - profit: 10.30 TON реализовано + 16.70 TON compound (1.12x)
- GridAI: trained=True, 9 примеров, step_model=OK dca_model=OK
- Торговля (DCA): ⏸️ выключена ручным переключателем
- DCA: 880,702 GRINCH | вложено 450.29 TON | -26% | ждёт +44% до $0.000773

### Открытые задачи
- Task #4 (PROPOSED): Telegram-алерт когда grid BUY levels застряли на no_funds

### Известные проблемы
- GridAI ai_manager.last_regime=UNKNOWN (пока не было ни одного успешного тика с известным режимом)
- Replit workflow сломан (не критично — Replit используется только как редактор)
- git push из Replit не работает (таймаут); VPS git push — нет GitHub credentials
  → деплой только через docker cp
