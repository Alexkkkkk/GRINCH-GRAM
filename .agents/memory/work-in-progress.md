---
name: Work In Progress
description: ЧТО ДЕЛАЛОСЬ В ПРОШЛОЙ СЕССИИ — файлы, незавершённые задачи, следующие шаги
---

## Последняя сессия: 2026-07-31

### Что сделано

**1. Исправлен режим «только в плюс» для сетки (grid_trader.py)**
- Ранее: `AI_MIN_BUY_CONF=55%` — жёсткий блокер. BrainFusion HOLD → ai_buy_conf=0% → grid никогда не покупал
- Теперь: `_is_profitable_buy_cycle()` = главный гейт. AI влияет только на РАЗМЕР ордера (0.7x–1.8x)
  - AI BUY ≥ 55% → полный размер по шкале
  - AI BUY < 55% (слабый/HOLD) → вход min ×0.7, НО цикл прибылен → BUY происходит
  - AI SELL сильный → стоп (без изменений)

**2. Исправлен GridAI (grid_ai.py)**
- `atr_pct='UNKNOWN'` (строка) → TypeError в `_make_features()` → обучение падало
- Фикс: `float(atr_pct)` с try/except, `isinstance(regime, str)` защита
- Данные на VPS исправлены: 3 записи 'UNKNOWN' → 0.0
- **GridAI теперь обучен: ✅ 10 примеров, step_model=OK, dca_model=OK**

**3. Деплой**
- scp → /opt/bot/ + docker cp → container
- git rebase + push → e5c5efe в origin/main ✅
- VPS /opt/bot HEAD = e5c5efe ✅ (синхронизировано)

### Текущий статус VPS (07:05 UTC)
- GridAI: trained=True, 10 примеров, win_rate=100%, avg_profit=1.72 TON
- Grid: active=True, buy=6, sell=10, compound=1.14x
- profit-check работает: `⚠️ SELL L2 @ 0.000411 — убыточно (est -1.39 TON)` ← корректно блокирует

### Нерешённые проблемы

1. **SELL L2 убыточно** — GRINCH куплен дороже чем SELL L2 (0.000411). Пройдёт когда цена вырастет.
2. **Task #3** — missing profit_ton/profit_pct/avg_price/close_price в bot_trades/bot_open_trades DB
3. **Торговля DCA выключена** (ручной переключатель) — намеренно пока DCA в минусе -26%

### Следующие шаги (если пользователь попросит)
- Task #3: записывать profit_ton, profit_pct, avg_price, close_price в БД при закрытии сделок
- Telegram-уведомление когда BUY уровни go no_funds (Task #2 частично — авто-ребилд есть)
