---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 05.08.2026

### Выполнено

1. ✅ **fix: SELL guard** — `_execute_sell` и restore-логика `skipped_dca` вычитают
   `grid_sell_alloc` из DCA-резерва, иначе guard блокировал все SELL (wallet=DCA=857k).
2. ✅ **fix: `_add_cycle_sell` guard** — grinch < 1.0 → пропуск уровня (нет 0-GRINCH SELLов).
3. ✅ **Торговля включена** — settings_store + DB обновлены, контейнер рестартован.
4. ✅ **4 аудит-бага** из прошлой сессии (lock/budget/race/id) — задеплоены и проверены.

### Текущее состояние (05.08.2026, ~03:20 UTC)
- Health: OK | trader: running | RAM: 362 MB
- Grid: active=True | sell_waiting=20 | profit_ton=28.55 | next_sell=+2.1%
- DCA: 857573 GRINCH @ $0.00084470, сейчас -29% | мёртвый час 03:xx UTC
- TON баланс: ~81.6 TON

### md5 деплоя
- grid_trader.py: 4185f436523f8d66c8bc6bd838282d7b
- trader.py: 663b3bb07d295192acce37a5d6469bd1

### Почему торговля снова выключилась
- В 03:13:27 `disable_trading()` был вызван через веб-дашборд (кто-то нажал Stop)
- Это НЕ автоматика — только ручное нажатие или `POST /api/trading/disable`
- GridTrader.py НЕ проверяет trading_enabled — сетка работает независимо

### Известные проблемы (не критичные)
- DCA-reduce entries учитываются в DCA_MAX_ENTRIES (может быть не желательно)
- /health не мониторит grid.last_tick_ts (задача #4 отменена)
- OpenAI / DeepSeek ключи на VPS не работают (429 / 401) — LLM-советник падает

### Git
- Все коммиты на origin/main (GitHub). VPS GitOps cron: */3 * * * * deploy.sh
- deploy.sh: git reset --hard origin/main + docker compose up --force-recreate
