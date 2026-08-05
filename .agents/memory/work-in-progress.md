---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 05.08.2026

### Выполнено

1. ✅ **Изменены уровни grid-сетки** — id=-2012 и id=-2013 (previously -2021/-2013 до реценровки):
   - id=-2012: 35.77 → 31.1 TON @ 0.000417 [waiting]
   - id=-2013: 40.62 → 31.1 TON @ 0.000401 [waiting]
   - Метод: docker stop → правка JSON напрямую в volume (`/var/lib/docker/volumes/bot_bot_data/_data/grid_state.json`) → docker start

### Важное открытие

- `_cleanup_stale_idle_levels` при каждом старте удаляет `idle-deploy` BUY уровни с `amount_ton < min_ton`
- min_ton = GAS_PER_TRADE_TON × 2 / cycle_factor = 0.30 × 2 / 0.01930 ≈ **31.1 TON**
- Нельзя ставить `idle-deploy` уровни меньше 31.1 TON — они будут удалены при рестарте

### Текущее состояние (05.08.2026)

- Grid active=True | sell_waiting=25 | buy_waiting=2 @ 31.1 TON
- Уровни -2012 (0.000417) и -2013 (0.000401) — waiting 31.1 TON каждый

### Как редактировать grid уровни на VPS

1. `docker stop bot-bot-1`
2. Редактировать `/var/lib/docker/volumes/bot_bot_data/_data/grid_state.json` напрямую на хосте
3. `docker start bot-bot-1`
4. Через 20с проверить JSON — значения должны сохраниться

### Git

- Все коммиты на origin/main (GitHub). VPS GitOps cron: */3 * * * * deploy.sh
- deploy.sh: git reset --hard origin/main + docker compose up --force-recreate
