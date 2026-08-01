---
name: Grid Trading System
description: AI-управляемая сеточная торговля GRINCH/TON — архитектура, параметры, интеграция.
---

# Grid Trading System

## Версия

**v3** (текущая): GridAI v3 + пирамидальное распределение + momentum-aware перецентровка + Kelly.

## Архитектура

`grid_trader.py` — самостоятельный модуль, синглтон `get_grid_trader()`.

**Ключевые классы:**
- `GridConfig` — все параметры через env vars (GRID_STEP_PCT, GRID_SELL_LEVELS, etc.)
- `GridLevel` — один уровень сетки (id, side, price_ton, amount_grinch/ton, status)
- `GridState` — полное состояние (уровни, прибыль, флаги), сохраняется в `/app/data/grid_state.json`
- `GridTrader` — основной движок с фоновым поллером (30s интервал)
  - `_price_history: deque(maxlen=20)` — история цен для momentum
  - `_price_momentum_pct: float` — % momentum обновляется каждый тик
  - `_calc_price_momentum()` — линейная регрессия slope по `_price_history`

## GridAI v3 (`grid_ai.py`)

- **Step-ансамбль:** RandomForestRegressor + GradientBoostingRegressor + Ridge (average)
- **DCA-ансамбль:** RandomForestClassifier + LogisticRegression (calibrated proba)
- **Время-взвешенное обучение:** `sample_weight=exp(-age_days / 7)`, T½=7 дней
- **13 признаков** (vs 5 в v2): atr_pct, regime_enc, atr²,|regime|, atr×regime,
  win_streak, recent_avg_profit, profit_momentum, hour_sin, hour_cos,
  regime_duration, trend_binary, negative_binary
- **Kelly criterion:** `f = (p×avg_win - (1-p)×avg_loss) / avg_win × 0.5` → `kelly_mult`
- **Авто-калибровка MIN_STEP:** минимальный шаг прибыльных сделок – 0.25%
- **Пирамидальные веса:** `get_pyramid_weights(n)` → [1.30 → 0.70], среднее=1.0
- **Опыт:** `/app/data/grid_ai_experience.json`, max 2000 записей, полная обратная совместимость
- **Синглтон:** `get_grid_ai()`

## Пирамидальное распределение SELL-уровней

В `build_grid()`: вес L1=1.30, L9=0.70 (линейное убывание), среднее=1.0.
**Why:** нижние уровни срабатывают первыми → больше GRINCH = больше прибыли при умеренном росте.
Примечание в level.note: `вес×{w:.2f}`.

## Momentum-aware перецентровка

В `_maybe_recenter()`:
- mom < -0.5% → **не перецентровываться** (цена падает, ждём дна)
- mom > 1.0% → порог снижается до 1.8 шагов (быстро добавляем sell выше)
- mom 0.3–1.0% → порог 2.2 шагов
- боковик → стандартный RECENTER_STEPS (2.5)
**Why:** при тренде вниз перецентровка ниже = продажи по плохой цене; при росте — наоборот нужно быстрее.

## Параметры сетки (по умолчанию)

- Шаг: 5% (ATR-адаптивный, минимум 4.0% — ниже никогда)
- 9 SELL уровней выше центральной цены
- 5 BUY уровней ниже (активируются при накоплении TON от продаж)
- Минимальный ордер: 15 TON
- Газовый резерв: 5 TON
- **FEE_PCT = 0.01** (1% DeDust комиссия за сторону)
- **GAS_PER_TRADE_TON = 0.30**

## Profit-guard (только в плюс)

Перед каждой сделкой — проверка прибыльности, торговля только при положительном результате:

**SELL:** `received_ton = grinch × price × 0.99 - gas`. Если `received_ton < cost_ton` — пропустить.  
**BUY:** цикл BUY→SELL прибылен только если `ton × step% × 0.99 × 0.99 - gas×2 > 0`.  
**Breakeven step:** ≈ 3.8% → MIN_STEP_PCT = 4.0% (запас 0.2%).

## AI-фильтры

- AI BUY ≥ 75% → пропустить SELL на этом тике (не мешать росту)
- AI SELL ≥ 60% → пропустить BUY на этом тике (не ловить нож)
- Источник: BrainFusion.get_state() → fallback → AIEngine.analyze()

## Динамический шаг (ATR)

- ATR > 5% → step 8% (высокая волатильность, широкая сетка)
- ATR 3-5% → step 6%
- ATR 2-3% → step 5% (нормальный рынок)
- ATR < 2% → step 4.0% (минимально прибыльный, не 3.5%)

## Реинвестирование

После каждого SELL: добавляется BUY-уровень на шаг ниже с TON от продажи (минус газ).
После каждого BUY: добавляется SELL-уровень на шаг выше для замыкания цикла.

## API эндпоинты (app.py)

- `GET /api/grid/status` — полный статус (включает ai_stats с v3-метриками)
- `POST /api/grid/build` — построить/перестроить (params: step_pct, sell_levels, buy_levels)
- `POST /api/grid/activate` — активировать
- `POST /api/grid/deactivate` — остановить
- `POST /api/grid/step` — изменить шаг на лету

## Запуск

Поллер стартует в `start_background()` в app.py, после ai_market_scanner.
DeDust-клиент инжектируется из `getattr(trader, 'dedust', None)`.

## Важно: координация с DCA trader

**Why:** GridTrader и основной trader.py используют одни и те же GRINCH — нужно следить чтобы не было двойных продаж:
- Grid SELL L9 (~$0.000826) близко к DCA TP ($0.000831)
- При срабатывании grid sell надо убедиться что DCA trader не продаёт то же одновременно

## Комиссии и безубыточность

- DeDust комиссия: 1% за сторону = 2% round-trip
- Газ: ~0.3 TON на сделку
- При 882k GRINCH @ 0.000380 TON (~335 TON общая позиция):
  - 98k GRINCH / уровень ≈ 37 TON / уровень
  - Fee: 37 × 2% = 0.74 TON + 0.3 газ = 1.04 TON
  - 5% шаг приносит: 37 × 5% = 1.85 TON
  - Чистая прибыль/цикл ≈ 1.85 - 1.04 = **0.81 TON** (+2.2% чистого)
