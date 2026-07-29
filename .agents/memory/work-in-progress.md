---
name: Work In Progress
description: Текущая незавершённая работа — что делалось, какие файлы затронуты, что ещё нужно сделать. Обновлять в конце каждой сессии.
---

# Work In Progress

## Как использовать

В начале сессии читай этот файл.  
В конце сессии — обновляй: перемести сделанное в "Завершено", добавь новые задачи в "В работе".

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

**Причина DCA мигания:** после рестарта или между тиками было кратковременное рассинхронирование entries_count=0 при наличии open_trades. Теперь два уровня защиты.

---

## ✅ Проверено — не баги

- `/api/coin/trades` — работает корректно
- `entry_ai_contexts` — сохраняется при каждом DCA-входе
- `bot_ai_examples` (4 записи) — нормально, накапливается с каждой закрытой сделкой
- `BUG_REPORT.md` на VPS — все 30 багов закрыты, это архив
- Ошибка "Сохранить настройки" 28.07 — была потому что контейнер рестартился в этот момент

---

## 🔄 Текущее состояние бота (29.07.2026, 06:42 МСК)

- **Открытая позиция:** 882k GRINCH, -9.13%, пик $0.00077710, вход $0.00077660
- **ONLY_PROFIT_EXIT:** держит, нужно ещё +17.83% до цели продажи
- **DCA:** entries=1, stake=479.23 TON, wait_pullback=True
- **AI советник:** Groq 429 (rate limit) → OpenAI 429 (quota exceeded) — советник пропускает тик
- **Bot stats:** 22 сделки, 20 побед (90.9%), PnL +147.99 TON

---

## ❌ Не сделано — открытые задачи

### Task #3 — Авто-удаление нерабочего OpenAI ключа
- Фолбэк на Groq через `_failed_providers` сделан, но сбрасывается при рестарте
- Файл `/app/data/openai_key.txt` на VPS содержит истёкший ключ
- **Что нужно:** при старте советника проверять ключ test-запросом; если 401/429 — инвалидировать в settings_store (персистентно)
- **Файлы:** `ai_advisor.py` (функция `_effective_key` или `run_advisor`)

### Task #4 + Task #2 — Белый список IP владельца
- Без вайтлиста владелец может попасть под авто-бан при активном дашборде
- **Что нужно:** `WHITELIST_IPS` в `security.py` — IP которые никогда не банятся и не rate-limit'ятся
- IP вносится через дашборд Settings или `settings_store`
- **Файлы:** `security.py`, `app.py` (POST /api/config)

---

## 📋 Контекст проекта

- **Боевой бот:** VPS 2.27.25.126, контейнер `bot-bot-1`, порт 3000 (nginx на 80)
- **Код на VPS:** `/opt/bot/`
- **SSH:** `sshpass -p "$VPS_SSH_KEY" ssh -o StrictHostKeyChecking=no root@2.27.25.126`
- **Деплой:** `scp файл root@2.27.25.126:/opt/bot/файл` → `cd /opt/bot && docker compose up -d --build`
- **Провайдеры AI советника (приоритет):** OpenAI(1) → DeepSeek(2) → xAI(3) → Anthropic(4) → Groq(5)
- **Replit используется как редактор** — превью не нужно, всё деплоится на VPS
