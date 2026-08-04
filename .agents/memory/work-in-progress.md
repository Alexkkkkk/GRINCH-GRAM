---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 04.08.2026 (утро, раунд 2)

### Выполнено

1. ✅ **grid_trader.py** — 3 улучшения GridAI:
   - **Momentum-reversal gate**: BUY блокируется если `_price_momentum_pct < -2%` (цена активно падает). Исключение: deep_dip > 1 шага — тогда входим не откладывая.
   - **Anti-cascade защита**: не более 2 BUY за 10 мин; при превышении — пауза 5 мин. `_buy_timestamps` deque, `_cascade_hold_until` float. BUY и DCA оба записывают timestamp.
   - **Depth-weighted idle-deploy**: каждый шаг глубже anchor = +15% к размеру ордера (max 1.5x). Выгодная цена = покупаем больше.
   - Новые GridConfig параметры: MOMENTUM_BUY_BLOCK_PCT, CASCADE_MAX_BUYS, CASCADE_WINDOW_SEC, CASCADE_COOLDOWN_SEC, IDLE_DEPTH_BOOST, IDLE_DEPTH_MAX_MULT

2. ✅ **ai_engine.py** — `from config import Config` (баг #1, NameError).

### Деплой на VPS (04.08.2026 ~05:53 UTC)
- Оба файла задеплоены через scp+docker cp, MD5 совпадает.
- Контейнер перезапущен: **healthy**, trader=running, seconds_since_tick=1.4
- Grid: active=True, sell=19, buy=10, dca=1, compound=1.28x

### Текущее состояние бота (04.08.2026 ~05:53 UTC)
- Health: ok | trader: running | RSS: 288 MB
- Grid: active=True, sell=19, buy=10, dca=1
- GridAI: 23 примера, обучен (RF+GB+Ridge step, RF+LR DCA)
- Торговля: ручной переключатель выключен (позиция -11% ждёт TP)
- Позиция: ~677k GRINCH @ -11.28% | Цель: +20.69%

### Незакрытые задачи (task queue)
- **Task #2** — Исправить оставшиеся 5 багов:
  - alerts.py:27 — _STALL_THRESHOLD_SEC=90 → нужно 180
  - dedust_client.py:169 — кэш баланса застревает при реальном TON=0
  - settings_store.py:74 — JSON пишется до DB
  - strategy.py:40-112 — rolling() без min_periods=1
  - user_trader.py:275 — _virtual_buy не проверяет баланс
- **Task #3** — idle-deploy статистика на дашборде
- **Task #4** — _maybe_deploy_idle_grinch (SELL-аналог)

### Git статус
- Локальные коммиты НЕ запушены (GitHub auth не настроен)
- VPS_SSH_KEY работает: sshpass -p "$VPS_SSH_KEY" ssh root@2.27.25.126
- Путь в контейнере: /usr/src/app/ (не /opt/bot!)
