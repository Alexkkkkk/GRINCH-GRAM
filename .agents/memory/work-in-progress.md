---
name: Work In Progress
description: Текущая незавершённая работа — что делалось, какие файлы затронуты, что ещё нужно сделать. Обновлять в конце каждой сессии.
---

# Work In Progress

## Как использовать

В начале каждого ответа — перечитай этот файл и обнови его прямо в ответе пользователю.  
В конце сессии — перемести сделанное в "Завершено", добавь новые задачи в "В работе".

---

## ✅ Завершено (сессия 28.07.2026 — аудит дашборда)

| Файл | Что исправлено |
|------|----------------|
| `db_store.py` | `equity_get_all` — ORDER BY ts **DESC** |
| `app.py` | `/api/equity` — читает из БД, experience_manager только fallback |
| `wallet_manager.py` | `get_full_status` — DB-fallback при cold start |
| `security.py` | Whitelist `127.0.0.1` / `::1` |
| `ai_advisor.py` | Авто-фолбэк провайдеров: при quota/auth блокирует на 6ч, переключается на следующий |

---

## ✅ Завершено (сессия 29.07.2026 — rate-limit + DCA карточка)

| Файл | Что исправлено |
|------|----------------|
| `app.py` | `/api/ai/history` добавлен в `_PUBLIC_EXACT` — был 401 без логина на мобильном |
| `security.py` | RATE_API_MAX 80→300, RATE_GENERAL_MAX 150→300, RATE_STATIC_MAX 400→600, AUTO_BAN_THRESHOLD 200→500 |
| `trader.py` | `get_status()` self-heal: если open_trades есть но dca_entries_count=0 — пересчитывает из open_trades |
| `static/js/app.js` | Фаза DCA: если `last_buy_price > 0` но entries_count=0 — не падать в "Ожидание входа", показывать "Ожидание отката" |

---

## ✅ Завершено (сессия 29.07.2026 — советник + whitelist)

| Задача | Статус |
|--------|--------|
| Персистентный фолбэк советника (Groq AuthError / OpenAI quota) | ✅ Сделано |
| Whitelist IP владельца — защита от авто-бана | ✅ Сделано |

---

## ✅ Завершено (сессия 29.07.2026 — стабилизация вкладки Кошелёк)

| Файл | Что исправлено |
|------|----------------|
| `static/js/app.js` | `_updatePortfolioTracker` пропускает обновление элементов, если открыта вкладка «Кошелёк» — устранён конфликт двух циклов обновления (`pt-total`, `roi-ring`, `roi-goal-sub` и др.) |
| `static/js/app.js` | Удалён старый `initEquityChart()` IIFE — он сбрасывал `canvas.width/height` каждые 15 сек, уничтожая Chart.js-инстанс; теперь единственный хозяин `#eq-chart` — `drawEqChart()` из шаблона |

**Причина прыгания #1 (портфель):** `_updatePortfolioTracker` (каждые 2 сек) писал `pt-total` как `"$625.92"`, а `renderWalletFull` (каждые 6 сек) — как `"431.9750 TON / $625.92"`.
**Причина прыгания #2 (график):** старый raw-Canvas IIFE и новый Chart.js дрались за один `#eq-chart` canvas каждые 6/15 сек.

---

## ❌ Не сделано — открытые задачи

*(нет — все текущие задачи выполнены)*

---

## 🔄 Текущее состояние бота (29.07.2026)

- **Открытая позиция:** 882k GRINCH, ≈ -11%, ждёт ONLY_PROFIT_EXIT
- **DCA:** entries=1, stake=479.23 TON, wait_pullback=True
- **AI советник:** работает с персистентным фолбэком провайдеров
- **Bot stats:** 22 сделки, 20 побед (90.9%), PnL +147.99 TON

---

## 📋 Контекст проекта

- **Боевой бот:** VPS 2.27.25.126, контейнер `bot-bot-1`, порт 3000 (nginx на 80)
- **Код на VPS:** `/opt/bot/`
- **SSH:** `sshpass -p "$VPS_SSH_KEY" ssh -o StrictHostKeyChecking=no root@2.27.25.126`
- **Деплой:** `sshpass -p "$VPS_SSH_KEY" scp файл root@2.27.25.126:/opt/bot/файл` → `docker cp /opt/bot/файл bot-bot-1:/usr/src/app/файл`
- **Провайдеры AI советника (приоритет):** OpenAI(1) → DeepSeek(2) → xAI(3) → Anthropic(4) → Groq(5)
- **Replit используется как редактор** — превью не нужно, всё деплоится на VPS
