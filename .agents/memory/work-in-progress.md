---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 04.08.2026 (вечер, раунд 4)

### Выполнено

1. ✅ **static/js/app.js** — прогресс-бар до закупки в SELL уровнях сетки:
   - Тонкая полоска (3px) под каждым waiting SELL уровнем
   - Показывает: current_price / target_price × 100%
   - Цвет: голубой → жёлтый (>70%) → зелёный (>90%)
   - Под полоской — процент в числовом виде
   - Задеплоено: scp + docker cp, MD5 совпадает

### Деплой на VPS (04.08.2026, вечер)
- app.js задеплоен, MD5: 916d071ce1070c66e6c0f2b936848b49
- Контейнер: healthy, trader=running

### Текущее состояние сетки (анализ)
- center_price: 0.000436 TON
- step_pct: 4.0% | compound: ×1.28
- profit: 28.55 TON | sell_cycles: 14 | buy_cycles: 10
- Все BUY уровни filled (~533 TON в GRINCH)
- Idle-deploy BUY L-2007/2008/2009 заблокированы (цикл убыточен -0.21 TON)
- 73 TON свободных — ждут условий
- Ближайший SELL: L-124 @ 0.000443 (+1.6%)

### Незакрытые задачи
- **Task #2** — Исправить 5 багов (alerts, dedust, settings, strategy, user_trader)
- **Task #3** — idle-deploy статистика на дашборде
- **Task #4** — _maybe_deploy_idle_grinch (SELL-аналог)

### Git статус
- Локальные коммиты НЕ запушены (GitHub auth не настроен)
- VPS_SSH_KEY работает: sshpass -p "$VPS_SSH_KEY" ssh -o StrictHostKeyChecking=no root@2.27.25.126
- Контейнер: bot-bot-1, путь: /usr/src/app/
