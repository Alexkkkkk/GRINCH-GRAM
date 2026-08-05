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

## Анализ VPS — 05.08.2026

- VPS и контейнер `bot-bot-1` работают; `/health`: trader running, последний tick ~2 секунды назад.
- Grid active, центр `0.0004713675 TON`, текущий шаг `4.0%`, режим в логах `SQUEEZE`, ATR около `2.4%`.
- BUY: 14 уровней — 8 filled, 5 cancelled_reposition, 1 waiting; свободный BUY-резерв только `40.62 TON` на уровне `0.00043581`.
- SELL: 28 уровней — 21 waiting, 3 filled, 2 skipped_small, 1 skipped_ai, 1 skipped_dca; в ожидании около `1.757M GRINCH`.
- Накопленный результат grid в state: `29.2188 TON`; последние 90 минут — свежие AI-решения, ошибок исполнения не видно.
- Изменений на VPS не вносилось. Для перенастройки сначала требуется отдельное решение по перекосу: почти весь BUY-резерв уже исполнен, SELL-уровни начинаются ниже центра и широко уходят вверх.
- Скриншоты 05.08 подтверждены live-state: портфель 458.3843 TON, 859534 GRINCH, grid +29.219 TON, SELL 21/28, BUY idle 40.6 TON.
- Потенциальный риск: waiting SELL-аллокация ~1.757M GRINCH превышает кошелёк 859534 GRINCH (~2.04x). Runtime guard ограничивает каждый отдельный SELL балансом, но не суммарную аллокацию всех waiting-уровней; при росте возможны пропуски/ошибки уровней.
