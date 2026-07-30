---
name: Work In Progress
description: ЧТО ДЕЛАЛОСЬ В ПРОШЛОЙ СЕССИИ — файлы, незавершённые задачи, следующие шаги. Читать в начале каждой сессии.
---

# Work In Progress

## Последняя сессия: 2026-07-30 (вечер)

### Что сделано
- Обновлён секрет VPS_SSH_KEY (новый пароль)
- **ИСПРАВЛЕН БАГ «только в плюс» (grid_trader.py)**:
  - До: AI BUY ≥ 55% был жёстким гейтом ПЕРЕД проверкой прибыльности → BUY никогда не срабатывал (GridAI не обучен → signal=0%)
  - После: сначала `_is_profitable_buy_cycle()` (математика), потом AI SELL-гейт, AI BUY — только масштабирует размер (0.7x–1.8x), не блокирует
  - Тот же паттерн применён к DCA-уровням
- **ИСПРАВЛЕН БАГ GridAI (_train)**:
  - `_safe_atr()` коерция atr_pct к float (защита от строк/None)
  - Когда все примеры одного класса (только прибыльные) — DCA-модель пропускается, но step-модель обучается и `_trained=True` выставляется
  - GridAI теперь обучается с первого запуска: `step_model=OK dca_model=OK`
- Коммит `073d0c5` — задеплоен через docker cp на VPS

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
- Task #2 (PROPOSED): Fix GridAI training crash → ЗАКРЫТ этой сессией (исправлен)
- Task #3 (PROPOSED): Fix missing trade profit/price data in database (profit_ton, profit_pct etc. NULL в bot_trades)
- Task #4 (PROPOSED): Telegram-алерт когда grid BUY levels застряли на no_funds

### Известные проблемы
- GridAI ai_manager.last_regime=UNKNOWN (пока не было ни одного успешного тика с известным режимом)
- Replit workflow сломан (не критично — Replit используется только как редактор)
- git push из Replit не работает (таймаут); VPS git push — нет GitHub credentials
  → деплой только через docker cp
