# 🐛 Полный аудит багов — QuantumBrain GRINCH Bot

> Аудит проведён: 22.07.2026  
> Охват: trader.py, app.py, ai_engine.py, ai_advisor.py, brain_fusion.py, dedust_client.py, db_store.py, experience_manager.py, wallet_tracker.py, deposit_monitor.py, settings_store.py, liquidity_guard.py, organism.py, user_trader.py

---

## 🔴 CRITICAL

### [C1] `db_store.py:650` — Потеря открытых сделок при краше
`open_trades_save` делает `DELETE FROM bot_open_trades` → `INSERT`. Если процесс падает между этими двумя операциями — **все открытые позиции пропадают из БД навсегда**.  
**Фикс:** завернуть в одну транзакцию с `TRUNCATE + INSERT` или использовать `UPSERT` (`INSERT … ON CONFLICT DO UPDATE`).

### [C2] `wallet_tracker.py:177,512` — Повторная обработка старых транзакций
Сет дедупликации `_seen` при переполнении (>6000) обрезается вдвое. Старые хэши транзакций "забываются" → при следующем появлении в API они обрабатываются повторно → **двойной подсчёт сделок**.  
**Фикс:** хранить `_seen` в отдельной таблице БД или использовать LRU с достаточным размером.

### [C3] `experience_manager.py:285–286` — Неверная классификация позиций после рестарта
При восстановлении из БД позиции разделяются по `trade_type == "short"`, но не все пути открытия сделки пишут это поле → **short-позиции после рестарта воспринимаются как long**.  
**Фикс:** явно писать `trade_type` в каждом пути `_open_trade`.

### [C4] `dedust_client.py:54–180` — Race condition в кэше баланса (HTTP storm)
Логика HTTP-запросов вне `_BAL_CACHE_LOCK`. Несколько потоков (trader, liquidator, deposit_monitor) могут одновременно пробить кэш и запустить параллельные запросы → **429 от TonCenter + непоследовательное состояние кэша**.  
**Фикс:** перенести весь блок fetch внутрь lock или использовать событие (`threading.Event`) для ожидания завершения уже идущего запроса.

---

## 🟠 HIGH

### [H1] `dedust_client.py:1126–1127` — Потеря GRINCH при продаже
Если оба API (TonCenter + backup) недоступны, fallback использует SDK для получения адреса jetton-кошелька. Комментарий в коде сам предупреждает: SDK возвращает **неверный адрес для нестандартных jetton** вроде GRINCH → `jetton_transfer` на неверный адрес = **безвозвратная потеря токенов**.  
**Фикс:** при недоступности обоих API — прерывать продажу с ошибкой, не делать fallback.

### [H2] `deposit_monitor.py:148–151` — Потеря депозита пользователя
`last_checked_lt` обновляется в БД **до** зачисления средств пользователю. При краше между этими шагами — депозит навсегда теряется (lt уже обновлён, повторная обработка невозможна).  
**Фикс:** обернуть `lt = update` и `credit_deposit` в одну атомарную транзакцию.

### [H3] `db_store.py:308` — Гонка на пуле соединений
`pool_ref = _pool` читается без лока. Если `_pool` заменяется другим потоком в момент чтения → `pool_ref` может оказаться `None` или устаревшей ссылкой → `AttributeError` или возврат коннекции в закрытый пул.  
**Фикс:** читать `_pool` внутри `_pool_lock`.

### [H4] `ai_advisor.py:1414–1434` — Stale AI сигнал при rate-limit Groq
При rate-limit advisor возвращает `{"ok": False}`. Если нет резервных ключей — BrainFusion использует устаревший сигнал без явного признака "данные протухли".  
**Фикс:** помечать сигнал как stale, не передавать его в BrainFusion как свежий.

### [H5] `brain_fusion.py:453–456` — Ошибка в логике `all_agree`
`ai_agrees = not ai_fresh or self._ai.signal == fs.action`. Если AI **не** свежий — `ai_agrees = True` автоматически. Это позволяет пропустить подтверждение на основе устаревших AI-данных.  
**Фикс:** `ai_agrees = ai_fresh and self._ai.signal == fs.action`.

### [H6] `user_trader.py:132,142` — Гонка при зачислении депозита
`credit_deposit` обновляет баланс в памяти, затем в БД. Concurrent trade/withdrawal между этими шагами может использовать устаревший баланс.  
**Фикс:** mutex вокруг всего блока или атомарный `UPDATE balance = balance + delta WHERE user_id = ?`.

---

## 🟡 MEDIUM

### [M1] `trader.py:935` — Гонка при обновлении `open_trades`
`self.open_trades` переприсваивается после merge **без** `_ot_lock`/`_close_lock`. Другие потоки могут видеть частично обновлённое состояние.

### [M2] `trader.py:1724–1725` — Небезопасный `append` в open_trades
`self.open_trades.append()` и `self.trades.append()` вызываются без лока. Параллельный trade может повредить список.

### [M3] `trader.py:805–808` — "Зомби" цикл при персистентной ошибке
Главный `_loop` ловит все `Exception` и продолжает. При персистентной ошибке (например, битый `experience.json`) — **спам в лог каждые 4 секунды** без backoff и без самолечения.  
**Фикс:** экспоненциальный backoff + счётчик повторяющихся ошибок → пауза/алерт.

### [M4] `trader.py:1927–1945` — Продажа всего при недостатке баланса в `_dca_sell_all`
Если `get_balance()` падает — `sell_amount = total_grinch` (из памяти). Реальный баланс может быть меньше → ошибка on-chain.

### [M5] `brain_fusion.py:143,306` — Inconsistent state в RLock
`_compute_fusion` обращается к мутабельным dataclass-полям. Между чтением `ai_fresh` и `ai_num` возможно вмешательство `update_ai` — неконсистентное состояние.

### [M6] `ai_engine.py:2408` — NaN в polyfit features
`np.polyfit` на константных или идентичных значениях возвращает `inf`/`NaN`. `nan_to_num` применяется поздно (только для последнего сэмпла) → потенциально кривые веса моделей при обучении.

### [M7] `ai_engine.py:2217,2247` — Неверные вероятности при неполных классах
`_align_proba` возвращает `[1/3, 1/3, 1/3]` если модель обучена только на части классов. SELL вероятность = 33% вместо 0% → ложные SELL-сигналы.

### [M8] `experience_manager.py:609` — Bypass DD_PAUSE в начале работы
`peak = ctrl.get("peak_equity", 0)`. При `peak=0` drawdown = 0 → защитная пауза `DD_PAUSE` не срабатывает при первом же убытке.

### [M9] `liquidity_guard.py:82` — Сброс пика ликвидности при восстановлении
После восстановления `_peak_liq = liq` (текущее, не исторический максимум). Второй дроп меряется от заниженного пика → guard менее чувствителен.

### [M10] `organism.py:426` — Несоответствие порогов `confidence`
В runtime `confidence` обновляется при `total >= 5`, при restore — при `total >= 3`. Метрика здоровья ведёт себя по-разному после рестарта.

### [M11] `settings_store.py:124` — Гонка при migrate_to_db
Миграция вызывается на уровне модуля при импорте. Если два процесса стартуют одновременно → двойная миграция / race condition.

### [M12] `ai_advisor.py:53,1848` — Stale Groq клиент
`GROQ_API_KEY` — глобальная переменная. `reload_key` обновляет её, но старые cached клиенты не видят изменения.

---

## 🔵 LOW

### [L1] `trader.py:1642,1711,3178` — Hardcoded gas значения
Gas (0.30 TON и др.) прошит литералами, не берётся из Config. При изменении сети — нужно менять в коде.

### [L2] `trader.py:2586–2588` — Hardcoded grade параметры
Confirmations и pullback % для grade A/B/C — литералы, должны быть в Config.

### [L3] `experience_manager.py:227` — Нет fsync перед заменой файла
`os.replace` атомарен, но без `fsync` на файловой дескрипторе — data loss при аппаратном сбое.

### [L4] `dedust_client.py:1087` — Dead code: лишний буфер к gas
`needed_nano = gas_nano + int(0.01 * TON)` — `gas_nano` уже включает всё необходимое. Двойной счёт.

### [L5] `dedust_client.py:184` — Утечка event loop в `_run()`
При исключении внутри `run_until_complete` loop закрывается в `finally`, но при частых вызовах возможен overhead от создания/закрытия петель.

### [L6] `organism.py:500` — Слабая защита от потерь в `get_size_multiplier`
При `mood = -1` (страх) множитель 0.8 — слишком мало, чтобы реально ограничить убытки.

### [L7] `db_store.py:1174` — `updated_at` не используется в `wallets_load`
Загружается без учёта актуальности записей.

### [L8] `liquidity_guard.py:142` — Side effect при импорте
`start()` вызывается на уровне модуля → background thread стартует при любом `import liquidity_guard` (усложняет тестирование).

---

## 📊 Сводка

| Severity  | Кол-во |
|-----------|--------|
| 🔴 CRITICAL | 4 |
| 🟠 HIGH    | 6 |
| 🟡 MEDIUM  | 12 |
| 🔵 LOW     | 8 |
| **Итого**  | **30** |

---

## 🤖 Статус AI-советника (Groq)

Groq AI advisor (`ai_advisor.py`) полностью реализован. Для активации нужен `GROQ_API_KEY` в secrets или через дашборд Settings → AI Advisor Key.

Известные проблемы с advisor: H4, M12 (см. выше).
