---
name: Work In Progress
description: Текущая незавершённая работа — что делалось, какие файлы затронуты, что ещё нужно сделать. Обновлять в конце каждой сессии.
---

# Work In Progress

## Как использовать

В начале каждого ответа — перечитай этот файл и обнови его прямо в ответе пользователю.  
В конце сессии — перемести сделанное в "Завершено", добавь новые задачи в "В работе".

---

## ✅ Завершено (сессия 29.07.2026 — Grid Trading)

| Что сделано | Результат |
|------------|-----------|
| `grid_trader.py` создан | Recovery grid + reинвест + AI-фильтр + динамический шаг |
| Grid endpoints добавлены в `app.py` | `/api/grid/status|build|activate|deactivate|step` |
| Grid поллер запущен в `start_background()` | Логи: `[Grid] Grid-trader поллер запущен` |
| Сетка построена и активирована | 9 SELL уровней, шаг 5%, состояние в `/app/data/grid_state.json` |
| Файлы задеплоены на VPS | `/opt/bot/grid_trader.py`, `/opt/bot/app.py` |

---

## ❌ Открытые задачи

| # | Приоритет | Задача |
|---|-----------|--------|
| 1 | 🔴 | **Groq API Key невалидный** — нужен новый ключ с console.groq.com → API Keys → Create |
| 2 | 🟡 | **TELEGRAM_CHAT_ID пустой** — алерты не доходят |
| 3 | 🟠 | **DCA TP конфликт с Grid** — когда grid SELL L9 (~$0.000826) сработает, проверить что DCA TP ($0.000831) не дублирует продажу |
| 4 | 🟡 | **Grid BUY уровни пустые** — нет свободного TON (2.3 TON). Активируются автоматически по мере выполнения SELL уровней (реинвест) |

---

## 🔄 Grid статус (29.07.2026 ~18:30 UTC)

**Центральная цена:** 0.000380 TON/GRINCH  
**Шаг:** 5% | **Уровни:** 9 SELL, 5 BUY (no_funds)

| Уровень | TON/GRINCH | USD (×1.40) | GRINCH | Статус |
|---------|-----------|-------------|--------|--------|
| SELL L1 | 0.000399 | $0.000559 | 98,036 | ⏳ waiting |
| SELL L2 | 0.000419 | $0.000587 | 98,036 | ⏳ waiting |
| SELL L3 | 0.000440 | $0.000616 | 98,036 | ⏳ waiting |
| SELL L4 | 0.000462 | $0.000647 | 98,036 | ⏳ waiting |
| SELL L5 | 0.000486 | $0.000680 | 98,036 | ⏳ waiting |
| SELL L6 | 0.000510 | $0.000714 | 98,036 | ⏳ waiting |
| SELL L7 | 0.000535 | $0.000749 | 98,036 | ⏳ waiting |
| SELL L8 | 0.000562 | **$0.000787** | 98,036 | ⏳ breakeven |
| SELL L9 | 0.000590 | **$0.000826** | 98,036 | ⏳ near TP |

---

## 📋 Grid API

```bash
# Статус сетки
curl http://localhost:3000/api/grid/status

# Перестроить с другим шагом
curl -X POST http://localhost:3000/api/grid/build -H 'Content-Type: application/json' \
     -d '{"step_pct": 4.0, "sell_levels": 9}'

# Включить/выключить
curl -X POST http://localhost:3000/api/grid/activate
curl -X POST http://localhost:3000/api/grid/deactivate

# Изменить шаг на лету
curl -X POST http://localhost:3000/api/grid/step -d '{"step_pct": 6.0}'
```

---

## 📋 Контекст проекта

- **Боевой бот:** VPS 2.27.25.126, контейнер `bot-bot-1`, порт 3000 (nginx на 80)
- **Код на VPS:** `/opt/bot/` + `.env` (права 600)
- **SSH:** `sshpass -p "$VPS_SSH_KEY" ssh -o StrictHostKeyChecking=no root@2.27.25.126`
- **Деплой:** scp → docker cp → или `docker compose restart bot`
- **Replit используется как редактор** — превью не нужно, всё деплоится на VPS
- **DCA позиция:** 882k GRINCH, -31%, вложено 479 TON, breakeven $0.000785
- **Wallet:** 2.295 TON свободно (только газ)
