---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 04.08.2026 (день, раунд 3)

### Выполнено

1. ✅ **grid_trader.py** — 6 комплексных улучшений сетки:
   - **Compound acceleration**: ставка 2%→5% по win_streak (+0.5%/победа). 3-й реинвест BUY при compound ≥1.5x
   - **Regime confirmation**: политика меняется только после 2+ тиков с одним режимом
   - **Нелинейный sizing**: квадратичная кривая + Kelly-буст ×2.2 при win_streak ≥ 8
   - **Adaptive tick**: 10с рядом с уровнем, 30с далеко
   - **Spike protection**: drop > 1.5×ATR за тик → двойной momentum-block на 10 мин
   - **Idle deploy**: динамический порог (max(20, 10% баланса)), price-reset cooldown, до 5 уровней

### Деплой на VPS (04.08.2026, день)
- grid_trader.py задеплоен через scp+docker cp, MD5 совпадает
- Контейнер перезапущен: **healthy**, trader=running, seconds_since_tick=5.2
- compound=1.28x, profit=28.55 TON, active=True

### Текущее состояние бота
- Health: ok | trader: running | RSS: 290 MB
- Позиция: BUY L-2009 пропущен (цикл убыточен — ждём подходящей цены)

### Незакрытые задачи (из task list)
- **Task #2** — Исправить 5 багов (alerts, dedust, settings, strategy, user_trader)
- **Task #3** — idle-deploy статистика на дашборде
- **Task #4** — _maybe_deploy_idle_grinch (SELL-аналог)

### Git статус
- Локальные коммиты НЕ запушены (GitHub auth не настроен)
- VPS_SSH_KEY работает: sshpass -p "$VPS_SSH_KEY" ssh root@2.27.25.126
- Путь в контейнере: /usr/src/app/ (не /opt/bot!)
