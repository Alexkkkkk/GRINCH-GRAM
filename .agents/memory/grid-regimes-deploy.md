---
name: Grid regime aliases and deployment
description: Live market regime names must match GridAI policies, and VPS self-update must include Grid modules.
---

GridAI получает от live-сканера режимы `SQUEEZE`, `RANGING`, `UPTREND`, `TREND_DOWN` и `DOWNTREND` помимо исторических названий. Менеджер сетки должен иметь явную политику для этих алиасов; иначе `get_status()` и перестройки незаметно используют `UNKNOWN`.

**Why:** На VPS фактический режим был `SQUEEZE`, но в `GridAIManager.REGIME_POLICY` его не было, поэтому рыночная политика не применялась, хотя GridAI правильно классифицировал рынок.

**How to apply:** При добавлении новых режимов обновлять одновременно `grid_ai.py` (границы/кодирование), `grid_trader.py` (политики/перестройки/heuristic) и проверять `/api/grid/status`. VPS self-update должен включать `grid_trader.py` и `grid_ai.py`; после обновления нужен полный restart, а не только SIGHUP/preload reload.