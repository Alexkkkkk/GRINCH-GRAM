---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Сессия — 06.08.2026 (вторая часть)

### Выполнено

- ✅ AI Engine v5 — комплексный апгрейд ai_engine.py (коммит 99d1a67):
    - BUY_THRESHOLD: 0.43 → 0.52 (меньше ложных входов)
    - SELL_THRESHOLD: 0.62 → 0.65
    - PROFIT_BIAS_PCT: 0.030 → 0.060 (labels покрывают DEX fees)
    - Label generation: endpoint → max(window) (ловит внутридневные пики)
    - Signal persistence: 2-3 последовательных BUY тика перед входом
    - EV_MIN_TRADES: 12 → 8 (EV фильтр активируется раньше)
    - 10 новых признаков: kama_er, rsi_div_bull/bear, up/dn_streak, ema_trend_str, vol_price_mom, stoch_rsi, vwap_dev_z, liq_proxy
    - Адаптивное мета-блендирование: 45-75% (было 60%)
    - Kelly: profit_margin = EV минус fee+gas

### Незавершённое

- ⛔ VPS SSH недоступен (пароль изменился, VPS_SSH_PASSWORD в секретах устарел)
- ⛔ GitHub push недоступен (токен read-only)
- ⏳ AI v5 применён локально, НЕ на VPS — нужен Task #2 для деплоя
- ⏳ Groq API ключ не установлен — AI советник офлайн (Task #3)
- ⏳ Торговля на VPS выключена вручную — решение за пользователем
