---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 04.08.2026 (раунд 8)

### Выполнено

1. ✅ **Аудит сетки VPS** — полный анализ состояния (раунд 7)
2. ✅ **Fix: idle-deploy аварийный порог при 0 BUY уровнях** (раунд 7)
3. ✅ **feat: DCA-reduce — сетка снижает DCA-минус** (раунд 8)
   - В `GridConfig` добавлены 3 параметра: `DCA_REDUCE_ENABLED=True`, `DCA_REDUCE_RATE=0.25`, `DCA_REDUCE_MIN_PROFIT=1.0`
   - Новый метод `_reduce_dca_loss()` в `GridTrader`: после каждого прибыльного SELL берёт 25% прибыли, покупает GRINCH по текущей цене и добавляет в open_trades DCA-трейдера → снижает средний вход
   - Вызов добавлен в `_execute_sell()` после compound-реинвеста
   - Коммит: 9186da9

### Деплой на VPS (раунд 8)
- ⚠️ VPS_SSH_KEY не доступен в текущей среде Replit — деплой НЕ выполнен
- Деплой вручную: `scp grid_trader.py root@2.27.25.126:/opt/bot/grid_trader.py && ssh root@2.27.25.126 "docker restart bot-bot-1"`
- md5: 49812bb387966e2deb057c8c7a790e9a

### Текущее состояние сетки VPS (на момент раунда 7)
- Профит: 28.55 TON | 14 SELL-цикла | 10 BUY-цикла | Compound 1.28x
- Центр: 0.000400 TON | Шаг: 4.0% | Режим: DOWNTREND
- SELL waiting: 21 (ближайший +4%)
- BUY waiting: 2 (L-2007 @ -3.1%, L-2008 @ -6.9%)
- DCA-трейдер: **выключен вручную**
- DCA-позиция: 857k GRINCH @ stake 523 TON, entry $0.0008447, текущий убыток -172 TON (-33%)
- TON на балансе: ~81 TON (76 TON в BUY, ~5 TON газовый резерв)

### Следующие шаги
- Добавить VPS_SSH_KEY в Replit Secrets для автодеплоя
- После деплоя проверить лог: `[Grid] 📉 DCA-reduce:` и `[Grid] ✅ DCA-reduce OK:`
- Опционально: настроить DCA_REDUCE_RATE через dashboard Settings

### Git статус
- Коммиты: 8f57283 + 4df9b4e + 9186da9 в локальном git
- GitHub push не настроен
