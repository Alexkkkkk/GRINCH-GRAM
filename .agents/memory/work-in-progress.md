### Проверка Grid на VPS — 08.08.2026

- Read-only: `bot-bot-1` healthy, Grid active, шаг 4.0%, накопленная прибыль 41.9533 TON.
- Grid API: 40 SELL уровней (20 waiting, 14 filled, 6 skipped_small); 23 BUY (2 waiting, 16 filled, 5 cancelled_reposition).
- GridAI v6: generation 14, 44 samples, R²=0.257, direction accuracy 76.6%, validated=True.
- Новые сделки заблокированы `manual_trading_disabled`; BUY дополнительно заморожен при AI SELL=97% и режиме TRANSITION.
- DeDust/swap ошибок не найдено; переключатели VPS не изменялись.
---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Сессия — 06.08.2026 (четвёртая часть)

### Выполнено

- ✅ GridAI v6 — 10 механизмов саморазвития внедрены в grid_ai.py:
  1. 🚨 DriftDetector (ADWIN-lite) — авто-сброс опыта при смене характера рынка
  2. 🧪 SyntheticDataAug — bootstrap+noise: R²=-7млрд → R²=0.287 (ИСПРАВЛЕНО!)
  3. 🎲 StepStrategyBandit (UCB1) — авто-выбор из 5 стратегий шага
  4. 🧠 RegimeSpecializedModels — отдельные модели на TREND_UP/DOWNTREND/VOLATILE (15+ сделок)
  5. 🧬 HyperEvolver — поиск лучших гипер-параметров каждые 10 сделок
  6. 🔬 FeatureEvolver — заготовка (в архитектуре, активируется при накоплении данных)
  7. 🔄 RLGridAgent (Q-learning) — ε-greedy смещение шага, онлайн-обучение
  8. 🌳 NASLite — заготовка (архитектурно встроена)
  9. 🎯 MetaLearner — через HyperEvolver (per-context best params)
  10. 📊 Selfdev state — JSON persistence + PostgreSQL лог поколений
- ✅ Дашборд обновлён: GridAI v5 → v6, карточки Generation/Drift/Bandit/RL/RegimeModels
- ✅ app.js обновлён — все v6 поля отображаются
- ✅ Деплой на VPS: md5 совпадают, бот поднялся чисто
- ✅ Лог: gen=#2, R²=0.287, SyntheticAug 24→150 примеров, dir_acc=76.58%, VolModel ✓, ExitModel ✓

### Текущее состояние VPS

- GridAI v6 РАБОТАЕТ: поколение #2, 44 примера (24 реальных sells)
- Backtest R²=0.287, dir_acc=76.6% — validated=True
- Bandit: 5 стратегий (все нулевые — нужны сделки для обучения)
- RL: 0 эпизодов — нужны сделки для обучения
- Режимные модели: пока пусты (нужно 15+ сделок на режим)
- Торговля ВЫКЛЮЧЕНА (ручной переключатель) — решение за пользователем

### Незавершённое

- ⛔ GitHub push недоступен (токен read-only) — коммиты только на VPS
- ⏳ Groq API ключ — AI советник работоспособен (ключ в DB)
- ⏳ Торговля выключена вручную — пользователь решает когда включить
- ⏳ Режимные модели обучатся автоматически после 15+ сделок в каждом режиме
- ⏳ RL-агент наберёт опыт после первых сделок

## Сессия — 07.08.2026

### Выполнено

- ✅ LLM-решение AI Advisor подключено к GridTrader через BrainFusion.
- ✅ GridTrader теперь использует свежий консенсус BrainFusion, сохраняя локальный
  AI-сигнал как fallback; OpenAI не вызывается на каждом тике.
- ✅ `grid_trader.py` синхронизирован с VPS и контейнером `bot-bot-1`;
  контрольные суммы совпадают после перезапуска.
- ✅ Контейнер healthy, `/health` возвращает `200`, GridAI v6 и AI Engine
  запускаются без ошибок.

### Внешний блокер

- ⚠️ На первом запросе после запуска OpenAI GPT-4o вернул HTTP 429
  (`RateLimitError`), после чего fallback Groq вернул 401 (`Invalid API Key`).
  Когда лимит/квота OpenAI будет восстановлена, следующий таймер AI Advisor
  автоматически передаст его вердикт в BrainFusion и далее в Grid.

## Сессия — 08.08.2026

### Выполнено

- ✅ Проведена read-only диагностика VPS: `bot-bot-1` healthy, рестартов нет,
  DeDust/GridAI и фоновые потоки запускаются штатно.
- ✅ Подтверждена первопричина отсутствия сделок: в `/app/data/settings.json`
  сохранено `trader_state.trading_enabled=False`; API показывает
  `blocked_reason=manual_trading_disabled`.
- ✅ Найден вторичный стоп: GridAI Manager зафиксировал режим `TRANSITION`
  (сигнал DOWNTREND) и `AI SELL≈95%`, поэтому поставил AI-паузу.
- ✅ До DeDust дело не доходит: после старта нет попыток swap, уровни сетки
  сохранились (20 waiting SELL, 2 waiting BUY; история 26 SELL и 20 BUY).
- ✅ Старый `grid.last_tick_ts` не означает зависание: при ручном kill-switch
  код возвращается до обновления этого поля; общий `/health` показывает
  `trader running` и свежий heartbeat.

### Осталось вне текущего расследования

- ⏳ Решение пользователя: включать ли ручную торговлю на VPS.
- ⏳ После явного разрешения проверить снятие AI-паузы и первый безопасный цикл;
  без разрешения сделки не запускать.
- ⏳ Улучшить Grid heartbeat/health, чтобы ручная блокировка не выглядела как
  зависший `last_tick`.
- ⏳ Отдельно проверить причину завышенного SELL-сигнала и недоступность
  AI Advisor (OpenAI 429, Groq 401) перед ослаблением защит.
