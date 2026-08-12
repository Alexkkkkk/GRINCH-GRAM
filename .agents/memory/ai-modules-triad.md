---
name: AI-модули — тройка оптимизаторов
description: EntryOptimizer + TPOptimizer + MarketScanner — интеграция в трейдер и BrainFusion
---

## Три новых AI-модуля

### ai_entry_optimizer.py
- `should_enter_now()` → входить в DCA прямо сейчас или ждать дна
- `record_outcome()` → обратная связь после закрытия сделки
- До 15 примеров — правила RSI/sm_score/volume; после — GradientBoosting

### ai_tp_optimizer.py
- `predict_tp()` → оптимальный TP% для текущего режима/момента
- `record_trade_result()` → обучение на реальных peak_pct сделок
- До 12 примеров — режимные дефолты; после — ExtraTreesRegressor

### ai_market_scanner.py
- 4 паттерна: Double Bottom, Accumulation, Squeeze Breakout, Bull Engulfing
- Фоновый поток, сканирует каждые 30с через `start(get_candles_fn)`
- `get_last_signal()` → сигнал живёт 120с, отдаётся в BrainFusion

## Интеграция

**brain_fusion.py**: scanner как 4-й источник (_W_SCANNER=0.08); веса пересмотрены (AI=0.65, TA=0.18, ADV=0.09)

**trader.py**:
- DCA drop trigger: вызывает `entry_opt.should_enter_now()`, при conf>0.70 "не входить" — откладывает докупку
- Первый вход: EntryOpt с порогом conf>0.70 блокирует преждевременный вход
- Sell target: TPOpt заменяет Config.DCA_TARGET_PROFIT_PCT динамическим значением, НО не ниже Config (защита)
- После закрытия: `record_outcome()` и `record_trade_result()` для онлайн-обучения

**app.py**: `ai_market_scanner.start()` в `start_background()`; `/api/ai-modules` в `_PUBLIC_EXACT`

**dashboard**: карточка `#ai-modules-card` над equity-chart; JS-polling каждые 20с

**Why:** ML учится только на реальных сделках бота — модели пусты при первом запуске, работают через эвристики. После накопления 12-15 сделок автоматически переобучаются в фоне.
