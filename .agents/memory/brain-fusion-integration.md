---
name: BrainFusion integration
description: Ключевые архитектурные решения при интеграции brain_fusion.py в торговый бот — потокобезопасность, источник истины для параметров, жизненный цикл флагов.
---

# BrainFusion — дурабельные решения

## Параметры скальп/памп-режима — НЕ мутируем Config глобально

**Правило:** Скальп и памп переопределяют TP/trail/size через `mode_params` dict,
передаваемый в `_open_trade(mode_params=None)` и `_targets(tp_override=None)`.
Config не мутируется — это thread-safe и устраняет race condition с советником.

**Why:** Config — разделяемое mutable состояние между потоками (торговый + API + советник).
Временная мутация `Config.TAKE_PROFIT_PCT = SCALP_TP_PCT` может быть прочитана
другим потоком в неправильный момент.

**How to apply:**
- `mode_params = {"tp_pct": X, "trail_pct": Y, "size_mult": Z}` — собирается в `_tick()`
- Передаётся в `_open_trade(mode_params=mode_params)` и `pending_buy["mode_params"]`
- `_targets(tp_override=mode_params.get("tp_pct"))` — floor всегда min_gross_tp
- `trade["trail_pct"] = mode_params.get("trail_pct", Config.TRAILING_STOP_PCT)` — в трейд-записи
- Мониторинг читает `trade.get("trail_pct", Config.TRAILING_STOP_PCT)` — не из Config

## brain_fusion — единственный источник истины для скальп-параметров

**Правило:** `_compute_fusion()` читает все пороги из Config:
`SCALP_MIN_AI_CONF, SCALP_MAX_ATR_PCT, SCALP_TP_PCT, SCALP_TRAIL_PCT, FUSION_PUMP_BOOST_MAX`.
ATR-адаптивные значения: `scalp_tp_pct = max(Config.SCALP_TP_PCT, atr * 2.2)`.
trader.py использует `_fusion_sig.scalp_tp_pct` — brain_fusion уже гарантирует floor.

**Why:** Избегаем двойной логики — brain_fusion вычисляет, trader применяет.

## RLock вместо Lock в BrainFusion

**Правило:** BrainFusion использует `threading.RLock()`, не `Lock()`.

**Why:** `get_state()` вызывает `get_wallet_analysis()`, оба под локом → дедлок.

## Fallback-заглушка при импорте brain_fusion

**Правило:** `import brain_fusion as _bf` в try/except с классом-заглушкой `_BFStub`.
Все методы заглушки возвращают нейтральные значения (False, None, 0).

## _last_entry_was_scalp — жизненный цикл

**Правило:** Флаг устанавливается в `True` ТОЛЬКО после `opened = self._open_trade(...)` 
когда `opened` is True и `_scalp_mode` is True.
Сбрасывается в `False` во ВСЕХ путях закрытия:
- `_dca_sell_all` → `on_trade_closed(was_scalp=self._last_entry_was_scalp)`; затем `= False`
- `_close_trade` → то же
- `pending_buy` restoration paths → `_last_entry_was_scalp = bool(_pb_mode.get("trail_pct"))`

## Инъекция ордер-флоу

**Правило:** `Config.ORDER_FLOW_INJECT_ENABLED` проверяется перед каждой инъекцией.

## coerce ai_conf в should_skip_confirmation

**Правило:** `ai_conf = float(ai_conf or 0.0)` — защита от None/строк.
`should_skip_confirmation()` вычисляет ПОЛНЫЙ fusion-консенсус (AI+TA+LLM),
никогда не short-circuits по AI-alone.
