---
name: RAM floor и оптимизации скорости
description: Физический минимум RSS и сделанные оптимизации скорости/памяти
---

# RAM floor и оптимизации скорости

## Правило
Бот НЕ может работать ниже ~220MB RSS при полном старте.
numpy+pandas+sklearn+flask изолированно = 173MB RSS.
pytoniq+dedust+cryptography+aiohttp добавляют ещё ~53MB.
Итог: ~226MB — физический минимум текущего стека.

**Why:** BLAS/LAPACK C-libs, загружаемые sklearn, занимают RSS вне tracemalloc.
tracemalloc показывает 68MB для sklearn, RSS = 154MB — разница = shared C-libs.

**How to apply:** Чтобы пробить 180MB нужно убрать sklearn (заменить onnx/tflite),
что требует полной переписи ai_engine.py. Не делать без явного запроса.

## Сделано

### RAM
- LOW_MEMORY_MODE default="1" в ai_engine.py И trader.py (исправлено несоответствие)
- 3 модели RF+ET+GB (tiny 12/10/8), REPLAY_SIZE=200, RETRAIN_EVERY=8
- _release_memory() после претрейна + каждый тик в LOW_MEMORY_MODE
- Лог-буфер trader 100 записей (было 200)

### Скорость  
- _safe_status() → orjson.OPT_SERIALIZE_NUMPY (5-10x быстрее recursive _walk)
- _analyze_locked() 12 аналитик параллельно ThreadPoolExecutor(6)
- _refit_all() параллельный max_workers=2, _release_memory после каждой модели
- get_advisor_summary() кэш 60s TTL
- trader.last_analysis кэш — get_status() не пересчитывает analyze() каждые 2с
- open_trades_save() + equity_bulk_insert() → execute_values bulk (1 roundtrip)
- DB statement_timeout 9000→7000ms
