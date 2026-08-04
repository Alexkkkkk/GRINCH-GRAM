---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 04.08.2026 (утро)

### Выполнено

1. ✅ **grid_trader.py** — добавлен `_maybe_deploy_idle_balance()`:
   - GridConfig: IDLE_TON_THRESHOLD=20, IDLE_LEVEL_TON=20, IDLE_DEPLOY_MAX_LEVELS=3, IDLE_COOLDOWN_SEC=300
   - Каждый тик считает free_ton = wallet_ton - frozen_buy_orders - GAS_RESERVE
   - Если free_ton ≥ 20 TON → добавляет до 3 новых BUY-уровней ниже anchor
   - AI BUY-фильтр, profit-guard, дедупликация ±0.5%, cooldown 5 мин
   - Вызов в `_tick()` после DCA-блока

2. ✅ **ai_engine.py** — критический баг исправлен:
   - Добавлен `from config import Config` (строка ~52 импортов)
   - Баг: `Config.EV_THRESHOLD` вызывал NameError при ≥12 подтверждённых сделках
   - Ломал `analyze()` каждый тик, дашборд не обновлялся

### Деплой на VPS (04.08.2026 ~05:43 UTC)
- `grid_trader.py` + `ai_engine.py` задеплоены через `scp` + `docker cp`
- MD5 совпадает в обоих случаях
- Контейнер перезапущен, статус: **healthy** ✅
- NameError исчез из логов ✅
- Последнее действие бота: BUY L-106: 64.92 TON → 116305 GRINCH ✅

### Текущее состояние бота (04.08.2026 ~05:43 UTC)
- Health: ok | trader: running | RSS: 299 MB
- Grid: active=True, sell=18, buy=7, dca=1
- Торговля: выключена ручным переключателем (нет свободного TON для DCA, -11% позиция ждёт TP)
- Позиция: ~677k GRINCH @ -11.28% | Цель: +20.69% до продажи ($0.00090443)

### Незакрытые задачи (task queue)
- **Task #2** — Исправить оставшиеся 5 багов из аудита:
  - alerts.py:27 — _STALL_THRESHOLD_SEC=90 → нужно 180
  - dedust_client.py:169 — кэш баланса застревает при реальном TON=0
  - settings_store.py:74 — JSON пишется до DB
  - strategy.py:40-112 — rolling() без min_periods=1
  - user_trader.py:275 — _virtual_buy не проверяет баланс
- **Task #3** — Показывать idle-deploy статистику на дашборде
- **Task #4** — _maybe_deploy_idle_grinch (SELL-аналог idle-deploy)

### Git статус
- Локальные коммиты НЕ запушены (GitHub auth не настроен — нужен токен или SSH-ключ для HTTPS)
- Деплой на VPS вручную через scp+docker cp
- VPS_SSH_KEY обновлён и работает
