import os
import threading
import time
from datetime import datetime
from config import Config
from exchange import ExchangeClient
from strategy import analyze
from ai_engine import AIEngine
from experience_manager import experience_manager
import liquidity_guard
try:
    import brain_fusion as _bf
except Exception as _bf_err:
    import logging as _bflog
    _bflog.getLogger("trader").warning(f"brain_fusion не загружен: {_bf_err} — используем заглушку")

    class _BFStub:
        """Заглушка BrainFusion — все методы возвращают безопасные значения."""
        class _FakeSig:
            action = "HOLD"; consensus_conf = 0.0; skip_confirmation = False
            is_scalp_window = False; is_pump_window = False
            scalp_tp_pct = 6.0; scalp_trail_pct = 3.5; position_boost = 1.0
            regime = "UNKNOWN"; reasoning = "stub"
        def update_ai(self, *a, **kw): pass
        def update_ta(self, *a, **kw): pass
        def update_advisor(self, *a, **kw): pass
        def update_wallet(self, *a, **kw): pass
        def get_fusion_signal(self): return self._FakeSig()
        def should_skip_confirmation(self, *a): return False
        def is_bullish_consensus(self, *a): return False
        def is_scalp_window(self): return False
        def on_trade_closed(self, *a, **kw): pass
        def log_decision(self, *a, **kw): pass
        def get_state(self): return {}

    class _BFModule:
        brain = _BFStub()
        def update_ai(self, *a, **kw): pass
        def update_ta(self, *a, **kw): pass
        def update_advisor(self, *a, **kw): pass
        def update_wallet(self, *a, **kw): pass
        def get_fusion_signal(self): return _BFStub._FakeSig()
        def should_skip_confirmation(self, *a): return False
        def is_bullish_consensus(self, *a): return False
        def is_scalp_window(self): return False
        def on_trade_closed(self, *a, **kw): pass
        def log_decision(self, *a, **kw): pass
        def get_state(self): return {}
    _bf = _BFModule()


class Trader:
    def __init__(self):
        self.exchange = ExchangeClient()
        self.ai       = AIEngine()
        self.running  = False
        self.training = False
        self.trades      = []
        self.open_trades = []
        self.logs        = []
        self.last_ai       = {}
        self.last_analysis = {}   # кэш последнего strategy.analyze() — обновляется в _tick()
        self.stats = {
            "total_trades":   0,
            "winning_trades": 0,
            "total_pnl":      0.0,
        }
        self._thread = None
        # ── Ручной выключатель торговли ──────────────────────────────────
        # После ДЕПЛОЯ (первый запуск, нет сохранённого состояния) — False.
        # После ПЕРЕЗАГРУЗКИ (рестарт процесса) — восстанавливается из DB.
        # Это НЕ то же самое, что exp.is_paused() (авто-пауза ИИ по просадке).
        self.trading_enabled = self._load_trading_enabled()
        self._last_disabled_log_ts = 0.0
        self.signal_callbacks = []
        self.on_training_progress = None
        # Сериализация закрытия позиций: не даём торговому циклу и ручному
        # закрытию продать одну и ту же позицию дважды.
        self._close_lock = threading.Lock()
        # Синхронизация чтения/записи open_trades между торговым циклом и
        # фоновыми читателями (wallet_manager.poll, API-запросы дашборда).
        # Без этого лока wallet_manager иногда читал позицию в момент,
        # когда amount/stake_ton уже обновлены, а остальные поля — ещё старые
        # (или наоборот), что давало скачущий P&L на дашборде при неизменной
        # цене входа. См. replit.md / memory: "wallet snapshot pnl flapping".
        self._ot_lock = threading.RLock()
        # Счётчик подтверждений BUY-сигнала (требуем 2 последовательных)
        self._buy_confirm_count = 0
        # Smart BUY: ожидаем откат к лучшей цене перед входом
        # Структура: {"target": float, "signal_price": float, "ai": dict,
        #              "analysis": dict, "ticks_left": int}
        self._pending_buy = None
        self.last_sm      = None   # последний сигнал умных денег (для статуса)
        self.decision_log = []     # кольцевой буфер AI-решений (макс 25)
        self._last_db_sync_ts = 0  # время последней синхронизации с DB
        self._last_entry_was_scalp = False  # флаг: последний вход был в скальп-режиме
        # ── Двусторонняя торговля ────────────────────────────────────────
        self.open_short_trades = []   # открытые SHORT-позиции (GRINCH→TON→GRINCH)
        self._sell_confirm_count = 0  # счётчик подтверждений SELL-сигнала для шорта
        self.last_entry   = {      # последняя оценка качества входа (для статуса)
            "quality": "C", "score": 0, "reasons": [],
            "vol_ratio": 1.0, "stoch_rsi": 0.5,
        }
        # ── DCA стратегия: состояние цикла ─────────────────────────────
        # dca_wait_pullback: True — ждём отката цены после продажи
        # dca_peak_price:    максимальная цена после последней продажи (база для отката)
        # dca_last_buy_price: цена последней DCA-покупки (база для докупки при падении)
        # dca_entries_count:  сколько DCA-входов сделано в текущем цикле
        # dca_total_stake:    суммарные затраты в TON за все входы текущего цикла
        self.dca_wait_pullback  = False
        self.dca_peak_price     = 0.0
        self.dca_last_buy_price = 0.0
        self.dca_entries_count  = 0
        self.dca_total_stake    = 0.0
        # ── Каскадный выход ───────────────────────────────────────────
        # True = уровень 1 (50%) уже продан, держим остаток до уровня 2
        self.dca_cascade_half_sold = False
        # ── Компаундирование ─────────────────────────────────────────
        # Накопленный бонус к ставке из прибылей предыдущих циклов
        self.dca_compound_bonus_ton = 0.0
        # ── Адаптивный триггер ───────────────────────────────────────
        # Скользящий буфер последних цен для детектора быстрого движения
        self._dca_price_history: list = []
        # Детектор крупных продаж: время последней безусловной покупки по этому триггеру
        self._last_large_sell_buy_ts = 0.0
        # DCA кулдаун: время последней DCA-докупки (защита от переторговли)
        self._last_dca_entry_ts = 0.0
        # Кулдаун после убыточного закрытия: не входить сразу в нисходящий тренд
        self._last_loss_ts = 0.0
        # Защита прибыли: пик стоимости портфеля (TON) для детектора разворота
        self.portfolio_high_water_ton = 0.0
        # Флаг: прибыль хотя бы раз достигла порога PROFIT_PROTECT_TON (не сбрасывается при откате)
        self.profit_protect_activated = False
        # Health-check: время и статус последнего успешного тика торгового цикла
        self.last_tick_ts = 0.0
        self.last_tick_ok = None

        # Кеш баланса: не долбим блокчейн при каждом /api/status (TTL 180 сек)
        self._balance_cache     = {}
        self._balance_cache_ts  = 0
        self._balance_cache_ttl = 30   # секунд (было 180) — быстрое обновление баланса
        # ── Долговременная память + само-управление ИИ ───────────────────
        self.exp = experience_manager
        self.exp.restore_trader(self)
        # Восстанавливаем Smart BUY из DB (если был при перезапуске)
        # Примечание: ai/analysis не сохраняются (тяжёлые объекты), поэтому
        # восстановленный ордер помечаем флагом restored=True — в _tick()
        # он будет исполнен по текущей рыночной цене без ожидания откатa.
        try:
            from settings_store import get_section
            pb_raw = get_section("trader_state").get("pending_buy")
            if pb_raw:
                import json as _json2
                pb_data = _json2.loads(pb_raw)
                if pb_data and pb_data.get("target"):
                    pb_data["restored"] = True   # флаг: ai/analysis отсутствуют
                    self._pending_buy = pb_data
        except Exception:
            pass
        # Восстанавливаем timestamp последней DCA-докупки (кулдаун переживает рестарт)
        try:
            from settings_store import get_section
            _ts_raw = get_section("trader_state").get("last_dca_entry_ts")
            if _ts_raw:
                self._last_dca_entry_ts = float(_ts_raw)
        except Exception:
            pass
        # Восстанавливаем DCA-состояние из открытых позиций:
        # dca_last_buy_price и dca_entries_count обнуляются при рестарте,
        # из-за чего блок докупки (строка 954) полностью пропускался —
        # условие `dca_last_buy_price > 0` никогда не выполнялось.
        try:
            _dca_trades = [t for t in self.open_trades if t.get("dca_entry")]
            # Fallback: если open_trades есть, но ни у одной нет dca_entry=True,
            # значит позиция была открыта до добавления поля или через старый путь.
            # В DCA-режиме любая открытая LONG-позиция — это DCA-вход.
            if not _dca_trades and self.open_trades and Config.DCA_MODE:
                _long_trades = [t for t in self.open_trades if t.get("trade_type") != "short"]
                if _long_trades:
                    _dca_trades = _long_trades
                    # Помечаем на лету, чтобы save_open_trades сохранил флаг в БД
                    for _idx, _t in enumerate(_long_trades, start=1):
                        _t.setdefault("dca_entry", True)
                        _t.setdefault("dca_index", _idx)
                    self.log(
                        f"🔧 DCA fallback: {len(_dca_trades)} позиций без dca_entry — "
                        f"помечены как DCA-входы автоматически",
                        "WARN",
                    )
            if _dca_trades:
                # Берём цену последней по времени DCA-покупки
                _dca_sorted = sorted(
                    _dca_trades,
                    key=lambda t: (t.get("dca_index") or 0, t.get("opened_at") or ""),
                )
                self.dca_last_buy_price = float(
                    _dca_sorted[-1].get("entry_price") or 0
                )
                # Количество входов = максимальный dca_index среди открытых
                self.dca_entries_count = max(
                    int(t.get("dca_index") or 1) for t in _dca_trades
                )
                # Суммарные затраты
                self.dca_total_stake = sum(
                    float(t.get("stake_ton") or 0) for t in _dca_trades
                )
                self.log(
                    f"🔄 DCA-состояние восстановлено: last_buy=${self.dca_last_buy_price:.8f} "
                    f"entries={self.dca_entries_count} stake={self.dca_total_stake:.2f} TON",
                    "INFO",
                )
        except Exception as _dca_restore_err:
            self.log(f"⚠️ DCA restore error: {_dca_restore_err}", "WARN")

        # Восстанавливаем dca_wait_pullback и dca_peak_price из открытых позиций.
        # Если позиция открыта — бот был в активном DCA-цикле и ждёт отката цены
        # для следующей докупки. high_water из сделки содержит реальный пик сессии.
        try:
            if self.open_trades:
                self.dca_wait_pullback = True
                _hw_prices = [float(t.get("high_water") or 0) for t in self.open_trades if t.get("high_water")]
                _peak = max(_hw_prices) if _hw_prices else 0.0
                if _peak <= 0 and self.dca_last_buy_price > 0:
                    _peak = self.dca_last_buy_price  # fallback: пик = цена входа
                if _peak > 0:
                    self.dca_peak_price = _peak
                self.log(
                    f"🔄 DCA wait_pullback восстановлен: пик=${self.dca_peak_price:.8f}",
                    "INFO",
                )
        except Exception as _dca_pull_err:
            self.log(f"⚠️ DCA pullback restore error: {_dca_pull_err}", "WARN")

        # ── Сверка с реальным балансом кошелька ────────────────────────
        # Сохранённая в БД/памяти позиция может отстать от реальности
        # (устаревшая запись, гонка при рестарте). Если расхождение с
        # реальным балансом GRINCH на кошельке больше 1% — считаем БД
        # устаревшей и подгоняем amount открытой позиции под факт,
        # чтобы дальнейшая торговля и дашборд не работали по неверным цифрам.
        try:
            real_bal = self.exchange.get_balance()
            real_grinch = float(real_bal.get("GRINCH", 0) or 0)
            book_grinch = sum(float(t.get("amount") or 0) for t in self.open_trades)
            if book_grinch > 0 and real_grinch < 1.0:
                # Кошелёк пустой, а в БД висит открытая позиция —
                # значит продажа прошла, но запись не была очищена.
                self.open_trades = []
                self.log(
                    f"🔧 Сверка баланса: кошелёк пуст ({real_grinch:.6f} GRINCH), "
                    f"но в БД открытая позиция {book_grinch:.2f} — позиция автоматически закрыта",
                    "WARN",
                )
                try:
                    self.exp.save_open_trades([])
                except Exception:
                    pass
            elif real_grinch > 0 and book_grinch > 0:
                diff_pct = abs(real_grinch - book_grinch) / real_grinch * 100
                if diff_pct > 1.0:
                    scale = real_grinch / book_grinch
                    # Лочим на время правки amount+stake_ton — иначе wallet_manager
                    # (фоновый поток) может прочитать позицию МЕЖДУ этими двумя
                    # присвоениями и увидеть newamount+oldstake (или наоборот),
                    # что даёт скачущий P&L на дашборде при неизменной цене входа.
                    with self._ot_lock:
                        for t in self.open_trades:
                            t["amount"] = round((t.get("amount") or 0) * scale, 6)
                            # stake_ton масштабируем так же, иначе прибыль будет
                            # завышена (ставка заниженная относительно реального количества).
                            t["stake_ton"] = round((t.get("stake_ton") or 0) * scale, 4)
                        # Обновляем dca_total_stake под тем же локом, иначе wallet_manager
                        # может прочитать открытые позиции (уже с новым stake_ton) и
                        # одновременно trader.dca_total_stake (ещё со старым значением),
                        # получив несогласованные данные и неверный P&L.
                        self.dca_total_stake = sum(
                            float(t.get("stake_ton") or 0) for t in self.open_trades
                        )
                    self.log(
                        f"🔧 Сверка баланса: БД показывала {book_grinch:.2f} GRINCH, "
                        f"на кошельке {real_grinch:.2f} (расхождение {diff_pct:.1f}%) — "
                        f"позиция скорректирована под реальный баланс",
                        "WARN",
                    )
                    try:
                        self.exp.save_open_trades(self._combined_open_trades())
                    except Exception:
                        pass
                    # Прямая запись в DB с повтором — exp.save_open_trades может
                    # тихо провалить DB-часть (пул ещё не прогрет), а молчаливый
                    # провал оставляет в DB устаревшие 85050/50 навсегда. Пишем
                    # напрямую через db_store и повторяем дважды для надёжности.
                    _corrected = self._combined_open_trades()
                    for _attempt in range(3):
                        try:
                            import db_store as _ds
                            if _ds.is_available():
                                _ds.open_trades_save(_corrected)
                                self.log(
                                    f"✅ Прямая DB-запись self-heal (попытка {_attempt+1}): "
                                    f"amount={sum(t.get('amount',0) for t in _corrected):.2f} "
                                    f"stake={sum(t.get('stake_ton',0) for t in _corrected):.4f}",
                                    "INFO",
                                )
                                break
                        except Exception as _dbe:
                            self.log(f"⚠️ Прямая DB-запись self-heal попытка {_attempt+1}: {_dbe}", "WARN")
                            time.sleep(1)
        except Exception as _bal_check_err:
            self.log(f"⚠️ Сверка баланса при старте не удалась: {_bal_check_err}", "WARN")

        # ── Летучие поля DCA/profit-protect/cooldown ───────────────────
        # Восстанавливаем после рестарта (cascade, compound bonus, HWM, cooldowns)
        self._restore_volatile_state()

        # ── Санитайз статистики ────────────────────────────────────────
        # winning_trades не может быть больше total_trades (иначе winrate
        # >100%, как показывал дашборд после старого бага в каскадном выходе).
        try:
            tt = int(self.stats.get("total_trades", 0) or 0)
            wt = int(self.stats.get("winning_trades", 0) or 0)
            if wt > tt:
                self.stats["winning_trades"] = tt
                try:
                    self.exp.data["stats"] = dict(self.stats)
                    self.exp._save_locked()
                except Exception:
                    pass
                self.log(
                    f"🔧 Санитайз статистики: winning_trades ({wt}) > total_trades ({tt}) — исправлено",
                    "WARN",
                )
        except Exception:
            pass

    def log(self, msg, level="INFO"):
        entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "level": level, "msg": msg}
        self.logs.append(entry)
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]
        print(f"[{entry['time']}] [{level}] {msg}")

    def start(self):
        if self.running:
            return
        self.running = True
        self._loop_stop_event = threading.Event()   # прерываемый сон главного цикла
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="trader-main")
        self._thread.start()
        self._start_deep_retrain_thread()
        self.log("Торговый агент запущен", "INFO")

    _DEEP_RETRAIN_INTERVAL_S = 2 * 24 * 3600  # раз в 2 дня

    def _start_deep_retrain_thread(self):
        """Фоновый поток: раз в 2 дня переобучает модели на ПОЛНОЙ истории
        из БД (bot_ai_examples), а не только на урезанном оперативном буфере
        в памяти. Так экономный режим не теряет старые обучающие примеры —
        они хоронятся в БД навсегда и периодически используются целиком.

        Инвариант «один поток за раз»:
        - _deep_retrain_stop_event сигнализирует потоку о необходимости выйти;
          event.wait(timeout) заменяет time.sleep, поэтому поток просыпается
          немедленно при вызове stop(), а не ждёт 600с или 2 дня.
        - Новый поток создаётся только если старый уже завершился (is_alive=False).
        """
        existing = getattr(self, "_deep_retrain_thread", None)
        if existing is not None and existing.is_alive():
            return  # старый поток ещё работает — не дублируем

        # Создаём/сбрасываем событие остановки для нового потока
        stop_event = threading.Event()
        self._deep_retrain_stop_event = stop_event

        def _worker():
            # Даём боту дособрать немного данных после старта; прерываемся
            # сразу, если пришёл сигнал остановки (не ждём 600с зря).
            if stop_event.wait(timeout=600):
                return  # stop() вызван во время начального ожидания

            while self.running and not stop_event.is_set():
                try:
                    try:
                        self.log("🔁 Запускаю глубокое переобучение ИИ на полной истории из БД...", "INFO")
                        ok = self.ai.deep_retrain_from_db(window=2000)
                        if ok:
                            self.log("✅ Глубокое переобучение (лёгкие модели) завершено", "INFO")
                    except Exception as e:
                        self.log(f"⚠️ Ошибка глубокого переобучения: {e}", "WARN")

                    # Тяжёлые модели (HGB/XGB/LGBM/MLP), убранные из "горячего"
                    # процесса ради RAM — обучаются ТОЛЬКО в изолированном
                    # сабпроцессе (свой PID, своя память ОС), результат кладётся
                    # в БД. Так их импорт xgboost/lightgbm никогда не раздувает
                    # RSS основного торгового процесса.
                    if not stop_event.is_set():
                        self._run_deep_model_subprocess()

                except Exception as e:
                    # Внешний catch: страховка от любых неожиданных исключений
                    # (MemoryError, OSError и т.п.) — поток НЕ умирает, просто
                    # ждёт следующего цикла переобучения.
                    self.log(f"⚠️ [deep-retrain] критическая ошибка итерации: {e}", "WARN")
                finally:
                    # Прерываемый sleep: просыпаемся мгновенно при stop(),
                    # а не блокируемся на 2 дня.
                    stop_event.wait(timeout=self._DEEP_RETRAIN_INTERVAL_S)

        self._deep_retrain_thread = threading.Thread(
            target=_worker, daemon=True, name="deep-retrain")
        self._deep_retrain_thread.start()

    def _run_deep_model_subprocess(self):
        """Запускает deep_retrain_worker.py отдельным процессом ОС, ждёт его
        завершения (обычно занимает минуты), затем — если хост подтверждённо
        не в LOW_MEMORY_MODE — подгружает свежеобученные модели из БД в живой
        ансамбль. На LOW_MEMORY_MODE-хостах модели остаются только в БД."""
        import subprocess
        import sys
        try:
            self.log("🧪 Запуск изолированного обучения тяжёлых моделей (HGB/XGB/LGBM/MLP)...", "INFO")
            result = subprocess.run(
                [sys.executable, "deep_retrain_worker.py"],
                cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
                capture_output=True, text=True, timeout=1800,
            )
            stdout_lines = (result.stdout or "").splitlines()
            # Показываем только значимые строки из сабпроцесса (без INFO-мусора БД)
            for line in stdout_lines[-20:]:
                if any(kw in line for kw in ("RESULT:", "acc=", "Итого:", "Примеров", "ошибка", "недоступна")):
                    self.log(f"[deep-retrain] {line}", "INFO")
            if result.returncode != 0:
                for line in (result.stderr or "").splitlines()[-10:]:
                    self.log(f"[deep-retrain][err] {line}", "WARN")
                return

            # Парсим маркер RESULT:... для точного сообщения
            result_marker = next(
                (ln for ln in reversed(stdout_lines) if ln.startswith("RESULT:")), "")
            if result_marker.startswith("RESULT:SAVED:"):
                saved_n = result_marker.split(":")[-1]
                loaded  = self.ai.load_deep_models()
                if loaded:
                    self.log(f"✅ Тяжёлые модели ({saved_n} шт.) обновлены в БД и подгружены в ансамбль", "INFO")
                else:
                    self.log(f"✅ Тяжёлые модели ({saved_n} шт.) обновлены в БД (LOW_MEMORY_MODE — в памяти не держим)", "INFO")
            elif result_marker == "RESULT:SKIPPED":
                self.log("ℹ️ Deep-обучение пропущено: мало примеров в БД (накапливается по мере торговли)", "INFO")
            else:
                self.log("ℹ️ Deep-обучение завершено (статус неизвестен)", "INFO")
        except subprocess.TimeoutExpired:
            self.log("⚠️ Обучение тяжёлых моделей превысило лимит 30 мин — прервано", "WARN")
        except Exception as e:
            self.log(f"⚠️ Ошибка запуска сабпроцесса глубокого обучения: {e}", "WARN")

    def stop(self):
        self.running = False
        self.training = False
        # Пробуждаем главный торговый цикл мгновенно (time.sleep(15) → event.wait)
        loop_event = getattr(self, "_loop_stop_event", None)
        if loop_event is not None:
            loop_event.set()
        # Пробуждаем deep-retrain поток мгновенно
        retrain_event = getattr(self, "_deep_retrain_stop_event", None)
        if retrain_event is not None:
            retrain_event.set()
        self.log("Торговый агент остановлен", "WARN")

    # ──────────────────────────────────────────
    # Ручной выключатель торговли (не путать с start/stop всего агента)
    # ──────────────────────────────────────────
    @staticmethod
    def _load_trading_enabled() -> bool:
        """Загружает последнее состояние кнопки торговли из DB/settings (с JSON fallback).
        Возвращает False если сохранённого состояния нет (первый запуск / деплой)."""
        try:
            from settings_store import get_section
            val = get_section("trader_state").get("trading_enabled")
            return str(val).lower() == "true" if val is not None else False
        except Exception:
            return False

    def _save_trading_enabled(self, state: bool) -> None:
        """Сохраняет состояние кнопки торговли в DB + JSON (settings_store) для пережития
        перезапуска и отказа БД."""
        try:
            from settings_store import update_section
            update_section("trader_state", {"trading_enabled": str(state)})
        except Exception:
            pass

    def _save_volatile_state(self) -> None:
        """Персистирует летучие поля DCA/profit-protect/cooldown в trader_state (DB+JSON).
        Вызывается при каждом изменении этих полей и периодически из тик-цикла.
        Best-effort: ошибка не должна ронять торговый цикл."""
        try:
            from settings_store import update_section
            update_section("trader_state", {
                "dca_cascade_half_sold":    str(self.dca_cascade_half_sold),
                "dca_compound_bonus_ton":   str(self.dca_compound_bonus_ton),
                "portfolio_high_water_ton": str(self.portfolio_high_water_ton),
                "profit_protect_activated": str(self.profit_protect_activated),
                "last_loss_ts":             str(self._last_loss_ts),
                "last_large_sell_buy_ts":   str(self._last_large_sell_buy_ts),
            })
        except Exception:
            pass

    def _restore_volatile_state(self) -> None:
        """Восстанавливает летучие поля из trader_state при старте бота."""
        try:
            from settings_store import get_section
            st = get_section("trader_state")
            if not st:
                return

            def _float(key, default=0.0):
                v = st.get(key)
                try:
                    return float(v) if v not in (None, "", "None") else default
                except (ValueError, TypeError):
                    return default

            def _bool(key, default=False):
                v = st.get(key)
                if v is None:
                    return default
                return str(v).lower() == "true"

            self.dca_cascade_half_sold    = _bool("dca_cascade_half_sold")
            self.dca_compound_bonus_ton   = _float("dca_compound_bonus_ton")
            self.portfolio_high_water_ton = _float("portfolio_high_water_ton")
            self.profit_protect_activated = _bool("profit_protect_activated")
            self._last_loss_ts            = _float("last_loss_ts")
            self._last_large_sell_buy_ts  = _float("last_large_sell_buy_ts")

            parts = []
            if self.dca_cascade_half_sold:
                parts.append("cascade=True")
            if self.dca_compound_bonus_ton > 0:
                parts.append(f"compound={self.dca_compound_bonus_ton:.2f} TON")
            if self.portfolio_high_water_ton > 0:
                parts.append(f"hwm={self.portfolio_high_water_ton:.4f} TON")
            if self.profit_protect_activated:
                parts.append("profit_protect=ON")
            cd_left = Config.LOSS_COOLDOWN_SEC - (time.time() - self._last_loss_ts)
            if cd_left > 0:
                parts.append(f"loss_cd={cd_left/60:.1f} мин")
            if parts:
                self.log(f"🔄 Volatile state восстановлен: {' | '.join(parts)}", "INFO")
        except Exception as _e:
            self.log(f"⚠️ _restore_volatile_state: {_e}", "WARN")

    def enable_trading(self):
        self.trading_enabled = True
        self._save_trading_enabled(True)
        self.log("🟢 Торговля ВКЛЮЧЕНА пользователем — бот может открывать/закрывать сделки", "INFO")

    def disable_trading(self):
        self.trading_enabled = False
        self._save_trading_enabled(False)
        self.log("🔴 Торговля ВЫКЛЮЧЕНА пользователем — бот приостановил все сделки", "WARN")

    def _trading_disabled_guard(self) -> bool:
        """True — торговля выключена, тик нужно пропустить (только логика сделок,
        мониторинг/цены/дашборд продолжают работать как обычно)."""
        if self.trading_enabled:
            return False
        now = time.time()
        if now - self._last_disabled_log_ts >= 300:  # не чаще раза в 5 минут
            self.log("⏸️ Торговля выключена (ручной переключатель) — сделки не выполняются", "INFO")
            self._last_disabled_log_ts = now
        return True

    # ──────────────────────────────────────────
    # Главный цикл
    # ──────────────────────────────────────────
    def _loop(self):
        self.training = True
        self.log("🧠 Начинаю предобучение AI модели...", "INFO")
        try:
            # LOW_MEMORY_MODE (Bothost и т.п.): меньше свечей → меньше признаков
            # и меньше пиковая память при обучении 3 моделей на старте.
            _pretrain_limit = 150 if os.getenv("LOW_MEMORY_MODE", "1") == "1" else 300
            ohlcv = self.exchange.get_ohlcv(limit=_pretrain_limit)
            self.ai.pretrain(ohlcv, on_progress=self._emit_progress)
        except Exception as e:
            self.log(f"⚠️ Ошибка предобучения: {e}", "WARN")
        self.training = False
        self.log("✅ Предобучение завершено. Запускаю торговый цикл.", "INFO")
        # Сразу после предобучения — отдаём ОС временные буферы обучения.
        # malloc_trim(0) возвращает glibc-арены даже то, что gc.collect() не отдал.
        try:
            from ai_engine import _release_memory
            _release_memory()
        except Exception:
            pass
        # Объединяем позиции, восстановленные с диска, в одну
        self._merge_long_trades()

        # СВЕРКА + восстановление сохранённого опыта (тёплый старт обучения).
        # Сначала показываем, что реально лежит на диске, затем подхватываем —
        # чтобы было видно: обучение продолжается, а НЕ начинается с нуля.
        try:
            mem = self.exp.ai_memory_summary()
            acc_part = (f", точность {mem['avg_accuracy']}%"
                        if mem.get("avg_accuracy") is not None else "")
            self.log(
                f"💾 Сверка памяти ИИ: на диске {mem['trades']} сделок, "
                f"{mem['confirmed']} подтверждённых примеров{acc_part}", "INFO"
            )
            n = self.exp.restore_ai(self.ai)
            if n:
                self.log(
                    f"✅ Память сверена и восстановлена: ИИ продолжает с {n} "
                    f"подтверждённых сделок (обучение НЕ с нуля)", "INFO"
                )
            elif mem["confirmed"] == 0:
                self.log(
                    "ℹ️ В памяти пока нет закрытых сделок — учиться не на чем. "
                    "Первая же закрытая сделка сохранится и переживёт перезапуск.",
                    "INFO"
                )
            else:
                self.log(
                    "⚠️ Опыт на диске несовместим с текущей моделью (изменился "
                    "набор признаков) — пропущен. Накопление начнётся заново.",
                    "WARN"
                )
        except Exception as e:
            self.log(f"Восстановление опыта ИИ: {e}", "WARN")

        _last_db_sync = 0.0
        while self.running:
            try:
                self._tick()
                self._record_equity()
                # Обновляем live-поля открытых сделок в DB раз в 60 секунд
                now = time.time()
                if self.open_trades and (now - _last_db_sync) >= 15:
                    self._sync_open_trades_to_db()
                    _last_db_sync = now
                # Летучие поля DCA/profit-protect/cooldown — сохраняем раз в 60 сек
                # (страховка: даже если точечный save в точке мутации не сработал)
                if not hasattr(self, "_last_volatile_save_ts"):
                    self._last_volatile_save_ts = 0.0
                if now - self._last_volatile_save_ts >= 60:
                    self._save_volatile_state()
                    self._last_volatile_save_ts = now
                self.last_tick_ts = time.time()
                self.last_tick_ok = True
            except Exception as e:
                self.log(f"Ошибка в цикле: {e}", "ERROR")
                self.last_tick_ts = time.time()
                self.last_tick_ok = False
            # На маломощных хостах (LOW_MEMORY_MODE) периодически отдаём ОС
            # память, освобождённую GC (glibc malloc иначе держит её в аренах).
            if os.getenv("LOW_MEMORY_MODE", "1") == "1":
                try:
                    from ai_engine import _release_memory
                    _release_memory()
                except Exception:
                    pass
            # Прерываемый сон: stop() немедленно разбудит через _loop_stop_event
            # 4 сек = оптимум: price_feed prefetch каждые 5с, реакция на откат быстрее
            self._loop_stop_event.wait(timeout=4)

    def _record_equity(self):
        """Снимок капитала кошелька в память (троттлинг внутри менеджера)."""
        try:
            from price_feed import price_feed
            self.exp.record_balance(self._get_balance_cached(),
                                    price_feed.get("GRINCH") or 0.0)
        except Exception:  # noqa: BLE001
            pass

    def _clear_pending_buy(self):
        """Сбрасывает Smart BUY и удаляет его из DB + JSON."""
        self._pending_buy = None
        try:
            from settings_store import update_section
            update_section("trader_state", {"pending_buy": ""})
        except Exception:
            pass

    def _combined_open_trades(self):
        """LONG + SHORT позиции вместе — единый список для сохранения/восстановления,
        чтобы SHORT-позиции тоже переживали рестарт бота (раньше терялись)."""
        return list(self.open_trades) + list(self.open_short_trades)

    def _sync_open_trades_to_db(self):
        """Обновляет live-поля открытых позиций (LONG + SHORT) в PostgreSQL (раз в 60 сек)."""
        try:
            from price_feed import price_feed
            import db_store
            if not db_store.is_available():
                return
            grinch_ton = price_feed.get_grinch_ton_price() or 0.0
            enriched   = self._enriched_open_trades(grinch_ton) + self._enriched_short_trades(grinch_ton)

            # Санитайз перед записью: если amount в памяти меньше 10% от того,
            # что уже лежит в DB — это признак чтения устаревших данных (race /
            # stale snapshot). Пропускаем запись, чтобы не откатить DB назад.
            try:
                cur_db = db_store.open_trades_get()
                if cur_db:
                    db_total  = sum(float(t.get("amount", 0) or 0) for t in cur_db)
                    new_total = sum(float(t.get("amount", 0) or 0) for t in enriched)
                    if db_total > 0 and new_total > 0 and new_total < db_total * 0.1:
                        self.log(
                            f"⚠️ _sync_open_trades_to_db: пропуск записи — "
                            f"in-memory amount {new_total:.2f} << DB {db_total:.2f} "
                            f"(возможен stale snapshot)",
                            "WARN",
                        )
                        return
            except Exception as _chk_e:
                self.log(f"⚠️ _sync_open_trades_to_db sanity-check: {_chk_e}", "WARN")

            mem_amount = sum(float(t.get("amount", 0) or 0) for t in enriched)
            mem_stake  = sum(float(t.get("stake_ton", 0) or 0) for t in enriched)
            self.log(
                f"[DB-sync] open_trades → amount={mem_amount:.2f} stake={mem_stake:.4f} "
                f"n={len(enriched)}",
                "INFO",
            )
            db_store.open_trades_save(enriched)
            self._last_db_sync_ts = time.time()
        except Exception as _sync_e:
            self.log(f"⚠️ _sync_open_trades_to_db ошибка: {_sync_e}", "WARN")

    def _merge_long_trades(self):
        """Объединяет все открытые LONG-позиции в одну с взвешенной средней ценой.
        Вызывается после каждой новой покупки GRINCH.
        SHORT-позиции не трогает."""
        long_trades = [t for t in self.open_trades if t.get("side") == "buy"]
        if len(long_trades) < 2:
            return

        total_amount = sum(t.get("amount", 0) for t in long_trades)
        total_stake  = sum(t.get("stake_ton", 0) for t in long_trades)
        if total_amount <= 0:
            return

        # Взвешенная средняя цена входа
        avg_entry_usd = sum(t.get("entry_price", 0) * t.get("amount", 0) for t in long_trades) / total_amount
        avg_entry_ton = sum(t.get("entry_price_ton", 0) * t.get("amount", 0) for t in long_trades) / total_amount

        # Пересчёт безубытка для объединённой позиции
        fee           = Config.FEE_PCT / 100.0
        sell_gas      = Config.SELL_GAS_TON
        buy_gas_each  = Config.BUY_GAS_TON
        total_buy_gas = buy_gas_each * len(long_trades)
        total_cost    = total_stake + total_buy_gas
        be_ton  = (total_cost + sell_gas) / (total_amount * (1 - fee)) if total_amount > 0 else 0
        entry_ton_avg = total_stake / total_amount if total_amount > 0 else avg_entry_ton
        be_usd  = round(avg_entry_usd * be_ton / entry_ton_avg, 8) if (entry_ton_avg > 0 and avg_entry_usd > 0) else 0

        min_gross = Config.required_gross_pct_with_gas(total_stake)
        tp        = round(avg_entry_usd * (1 + Config.TAKE_PROFIT_PCT / 100), 8)

        # Основа — новейшая (последняя) позиция
        newest = long_trades[-1]
        merged = dict(newest)
        merged["amount"]          = round(total_amount, 6)
        merged["stake_ton"]       = round(total_stake, 4)
        merged["entry_price"]     = round(avg_entry_usd, 8)
        merged["entry_price_ton"] = round(avg_entry_ton, 8)
        merged["breakeven_price"] = be_usd
        merged["min_gross_pct"]   = round(min_gross, 1)
        merged["high_water"]      = max(t.get("high_water", avg_entry_usd) for t in long_trades)
        merged["take_profit"]     = tp
        merged["stop_loss"]       = 0.0
        merged["trail_pct"]       = Config.TRAILING_STOP_PCT
        merged["opened_at"]       = min((t.get("opened_at") or "") for t in long_trades) or newest["opened_at"]
        merged["ai_confidence"]   = max(t.get("ai_confidence", 0) for t in long_trades)
        merged["merged"]          = True
        merged["merged_count"]    = len(long_trades)

        # Оставляем SHORT-позиции, заменяем все LONG на одну объединённую
        shorts = [t for t in self.open_trades if t.get("side") != "buy"]
        self.open_trades = shorts + [merged]

        # Обновляем запись в полном журнале сделок
        for t in self.trades:
            if t.get("id") == newest["id"]:
                t.update(merged)
                break

        self.log(
            f"🔀 Объединено {len(long_trades)} позиций → 1: "
            f"{total_amount:.2f} GRINCH @ ср.цена ${avg_entry_usd:.8f} | "
            f"ставка {total_stake:.2f} TON | BE ${be_usd:.8f} | TP ${tp:.8f}",
            "INFO"
        )
        try:
            self.exp.save_open_trades(self._combined_open_trades())
        except Exception:
            pass

    def _check_profit_protection(self, price_usd: float, grinch_ton: float) -> bool:
        """
        Защита прибыли: как только прибыль портфеля хотя бы раз достигла
        PROFIT_PROTECT_TON TON — ставим тесный трейл 2% от пика стоимости.
        При откате >= 2% или AI SELL >= 55% — продаём ВСЁ немедленно.

        Ключевое отличие от старой логики: флаг активации (profit_protect_activated)
        НЕ сбрасывается при временном откате ниже порога. Это гарантирует, что
        защита сработает даже если прибыль к моменту отката уже частично убралась
        (старая логика возвращала False при profit_ton < порог, из-за чего
        при малом пороге 3 TON откат в 4%+ уже съедал всю прибыль).

        Работает в DCA-режиме и AI-режиме. Уважает ONLY_PROFIT_EXIT.
        Сбрасывает portfolio_high_water_ton и флаг после продажи.
        """
        if not Config.PROFIT_PROTECT_ENABLED:
            return False
        if not self.open_trades:
            self.profit_protect_activated = False   # сброс при закрытии всех позиций
            return False
        if grinch_ton <= 0 or price_usd <= 0:
            return False

        # Текущая прибыль портфеля в TON
        total_cost_ton, total_value_ton = self._dca_portfolio_value(grinch_ton)
        if total_cost_ton <= 0 or total_value_ton <= 0:
            return False

        profit_ton = total_value_ton - total_cost_ton

        # Обновляем пик стоимости портфеля (всегда, не только в профите)
        if total_value_ton > self.portfolio_high_water_ton:
            self.portfolio_high_water_ton = total_value_ton
            self._save_volatile_state()  # новый HWM → сохраняем

        # Активируем защиту как только прибыль достигла порога — флаг остаётся
        # активным даже если цена временно откатилась ниже порога
        if profit_ton >= Config.PROFIT_PROTECT_TON:
            if not self.profit_protect_activated:  # только при первой активации
                self.profit_protect_activated = True
                self._save_volatile_state()
            else:
                self.profit_protect_activated = True

        if not self.profit_protect_activated:
            return False

        # ── Детектор разворота ── 1: тесный трейл 2% от пика портфеля ──
        # Используем фиксированные 2% (вместо адаптивных 4–12%), чтобы успеть
        # зафиксировать прибыль до того, как откат съест весь заработок.
        TIGHT_TRAIL_PCT = 2.0

        drop_from_peak = 0.0
        if self.portfolio_high_water_ton > total_value_ton:
            drop_from_peak = (
                (self.portfolio_high_water_ton - total_value_ton)
                / self.portfolio_high_water_ton * 100
            )
        price_fell = drop_from_peak >= TIGHT_TRAIL_PCT

        # ── Детектор разворота ── 2: AI говорит SELL ────────────────
        ai_sell = False
        if Config.PROFIT_PROTECT_AI_SELL:
            ai_action = (self.last_ai or {}).get("action", "")
            ai_conf   = float((self.last_ai or {}).get("confidence", 0) or 0)
            ai_sell   = (ai_action == "SELL" and ai_conf >= 55)

        if not price_fell and not ai_sell:
            return False

        # ── Продаём ВСЁ ────────────────────────────────────────────
        reason_parts = []
        if price_fell:
            reason_parts.append(
                f"откат -{drop_from_peak:.1f}% от пика портфеля (трейл {TIGHT_TRAIL_PCT:.1f}%)"
            )
        if ai_sell:
            ai_conf2 = float((self.last_ai or {}).get("confidence", 0) or 0)
            reason_parts.append(f"AI SELL {ai_conf2:.0f}%")
        reason = " + ".join(reason_parts)

        portfolio_pct = (total_value_ton - total_cost_ton) / total_cost_ton * 100
        total_grinch  = sum(t.get("amount", 0) for t in self.open_trades)

        # ONLY_PROFIT_EXIT: никогда не продаём в убыток даже через защиту прибыли.
        # Сценарий: прибыль была ≥ PROFIT_PROTECT_TON (флаг взведён), потом цена
        # упала ниже средней цены входа → portfolio_pct < 0. Продажа сейчас —
        # это реализованный убыток. Блокируем и ждём возврата в прибыль.
        if Config.ONLY_PROFIT_EXIT and portfolio_pct < 0:
            self.log(
                f"🛡️ ONLY_PROFIT_EXIT: защита прибыли заблокирована — "
                f"портфель {portfolio_pct:+.1f}% (убыток). Держим до возврата в плюс.",
                "WARN"
            )
            return False

        self.log(
            f"🛡️ ЗАЩИТА ПРИБЫЛИ: {profit_ton:+.4f} TON ({portfolio_pct:+.1f}%) | "
            f"пик {self.portfolio_high_water_ton:.4f} TON | "
            f"{reason} | продаём {total_grinch:.2f} GRINCH @ ${price_usd:.8f}",
            "INFO"
        )

        closed = self._dca_sell_all(price_usd, grinch_ton, portfolio_pct)
        if closed:
            self.portfolio_high_water_ton = 0.0    # сброс пика после продажи
            self.profit_protect_activated = False   # сброс флага активации
            self._emit_signal("SELL", price_usd, self.last_ai)
            self.log(
                f"✅ Защита прибыли ИСПОЛНЕНА: {profit_ton:+.4f} TON зафиксировано | {reason}",
                "INFO"
            )
            # В DCA-режиме — ждём откат перед следующим входом
            if Config.DCA_MODE:
                self.dca_wait_pullback = True
                self.dca_peak_price    = price_usd
        return closed

    def _check_large_sell_dca(self, price_usd: float, grinch_ton: float) -> bool:
        """
        Детектор крупных продаж. Если в пуле зафиксирована крупная продажа
        (>= Config.LARGE_SELL_MIN_TON) за последние 2 минуты — безусловно
        покупаем на LARGE_SELL_DCA_TON TON. Возвращает True если покупка выполнена.

        Безопасность: уважает ONLY_PROFIT_EXIT (не продаём в убыток), но
        покупку совершает ВСЕГДА — обходя AI-фильтры и DCA-ожидание отката.
        """
        if not Config.LARGE_SELL_DCA_ENABLED:
            return False
        now = time.time()
        # Выдерживаем cooldown между безусловными покупками
        if now - self._last_large_sell_buy_ts < Config.LARGE_SELL_COOLDOWN_SEC:
            return False
        try:
            from app import wallet_tracker
        except Exception:
            return False
        large_sells = wallet_tracker.get_large_sell_events(
            window_sec=120.0,
            min_ton=Config.LARGE_SELL_MIN_TON,
        )
        if not large_sells:
            return False
        total_ton = sum(e["ton"] for e in large_sells)
        max_sell  = max(e["ton"] for e in large_sells)
        self.log(
            f"🚨 КРУПНАЯ ПРОДАЖА в пуле: {len(large_sells)} сделок | "
            f"итого {total_ton:.1f} TON | макс. {max_sell:.1f} TON | "
            f"порог {Config.LARGE_SELL_MIN_TON:.0f} TON — "
            f"безусловно покупаем {Config.LARGE_SELL_DCA_TON:.0f} TON",
            "INFO"
        )
        import config as _cfg
        orig = _cfg.Config.TRADE_AMOUNT
        _cfg.Config.TRADE_AMOUNT = float(Config.LARGE_SELL_DCA_TON)
        ai = self.last_ai or {}
        result = self._get_analysis_snapshot()
        opened = self._open_trade("buy", price_usd, result or {}, ai)
        _cfg.Config.TRADE_AMOUNT = orig
        if opened:
            self._last_large_sell_buy_ts = now
            self._save_volatile_state()  # large-sell cooldown переживёт рестарт
            self._emit_signal("BUY", price_usd, ai)
            self.log(
                f"✅ Large Sell DCA: куплено {Config.LARGE_SELL_DCA_TON:.0f} TON @ ${price_usd:.8f}",
                "INFO"
            )
            return True
        self.log("⚠️ Large Sell DCA: _open_trade вернул False (позиция уже есть или нет цены)", "WARN")
        return False

    def force_buy(self, amount_ton=None):
        """Ручная покупка — обходит сигнальную логику, открывает по текущей цене."""
        try:
            from price_feed import price_feed
            price = price_feed.get("GRINCH") or 0
            if price <= 0:
                return {"ok": False, "error": "Нет цены"}
            # Не блокируем — если уже есть лонги, новая покупка объединится с ними
            if amount_ton:
                import config as _cfg
                orig = _cfg.Config.TRADE_AMOUNT
                _cfg.Config.TRADE_AMOUNT = float(amount_ton)
            ai = self.last_ai or {}
            result = self._get_analysis_snapshot()
            opened = self._open_trade("buy", price, result or {}, ai)
            if amount_ton:
                _cfg.Config.TRADE_AMOUNT = orig
            if opened:
                self._emit_signal("BUY", price, ai)
                self.log(f"🖐️ Ручная покупка: ${price:.8f} | {amount_ton or 'auto'} TON", "INFO")
                return {"ok": True, "price": price}
            return {"ok": False, "error": "Ордер не прошёл"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def force_sell_all(self):
        """Ручная продажа всех позиций (уважает ONLY_PROFIT_EXIT)."""
        try:
            from price_feed import price_feed
            price = price_feed.get("GRINCH") or 0
            if price <= 0:
                return {"ok": False, "error": "Нет цены"}
            if not self.open_trades:
                return {"ok": False, "error": "Нет открытых позиций"}
            result = self._get_analysis_snapshot()
            closed = self._close_all_trades(price, result or {})
            if closed:
                self.log(f"🖐️ Ручная продажа всех позиций: ${price:.8f}", "INFO")
                return {"ok": True, "closed": len(closed)}
            return {"ok": False, "error": "Продажа невозможна (ONLY_PROFIT_EXIT)"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get_analysis_snapshot(self):
        """Быстрый снимок анализа без блокировки."""
        try:
            ohlcv = self.exchange.get_ohlcv(limit=60)
            from strategy import analyze
            return analyze(ohlcv)
        except Exception:
            return {}

    def _emit_progress(self, progress_dict):
        if self.on_training_progress:
            try:
                self.on_training_progress(progress_dict)
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════
    # DCA (Усреднение позиции) стратегия
    # ══════════════════════════════════════════════════════════════════
    def _is_dead_hour(self) -> bool:
        """True если текущий UTC час — «мёртвый» (низкий объём, новые входы блокируются)."""
        import datetime as _dt
        return _dt.datetime.utcnow().hour in Config.DEAD_HOURS_UTC

    def _tick_dca(self):
        """
        DCA-стратегия торговли GRINCH/TON:

        Правила:
        1. Первый вход: покупаем DCA_STAKE_TON (100 TON) по рынку.
        2. Рост → когда суммарная стоимость GRINCH >= +DCA_TARGET_PROFIT_PCT%
           от суммарных затрат → продаём ВСЁ одной сделкой.
           После продажи: ждём отката на DCA_PULLBACK_WAIT_PCT% от пика.
        3. Падение → если цена упала на DCA_DROP_TRIGGER_PCT% от цены
           ПОСЛЕДНЕЙ покупки → докупаем ещё DCA_STAKE_TON.
        4. После достижения цели и продажи всего: ждём отката 25-30%
           от максимальной цены, затем начинаем новый цикл.
        Временной фильтр: в DEAD_HOURS_UTC первые входы и ре-входы блокируются;
        докупка к уже открытым позициям допускается с расширенным триггером x DEAD_HOURS_DROP_MULT.
        """
        if self._trading_disabled_guard():
            return

        from price_feed import price_feed

        price_usd = price_feed.get("GRINCH") or 0.0
        if price_usd <= 0:
            self.log("⚠️ DCA: нет цены GRINCH, пропускаем тик", "WARN")
            return

        grinch_ton = price_feed.get_grinch_ton_price() or 0.0

        # ── Защита прибыли (работает в любом режиме) ────────────────
        # Если портфель +N TON И рынок падает — продаём ВСЁ немедленно
        try:
            if self._check_profit_protection(price_usd, grinch_ton):
                return    # продали всё, выходим из тика
        except Exception as _ppe:
            self.log(f"⚠️ Profit protect check error: {_ppe}", "WARN")

        # ── Детектор крупных продаж (работает в любом режиме) ───────
        # Покупаем безусловно при крупной продаже в пуле — даже в фазе ожидания отката
        try:
            self._check_large_sell_dca(price_usd, grinch_ton)
        except Exception as _lse:
            self.log(f"⚠️ Large Sell DCA check error: {_lse}", "WARN")

        # ── Адаптивный триггер: отслеживаем скорость рынка ─────────────
        # Буфер последних 5 цен (один тик = 15 сек → 5 тиков = 75 сек).
        self._dca_price_history.append(price_usd)
        if len(self._dca_price_history) > 5:
            self._dca_price_history = self._dca_price_history[-5:]
        # Считаем движение за последние тики: «ракетный» рост → агрессивный триггер докупки
        _fast_market = False
        if (Config.DCA_ADAPTIVE_TRIGGER_ENABLED
                and len(self._dca_price_history) >= 3):
            oldest = self._dca_price_history[0]
            if oldest > 0:
                _price_velocity = (price_usd - oldest) / oldest * 100
                if abs(_price_velocity) >= Config.DCA_ADAPTIVE_FAST_MOVE_PCT:
                    _fast_market = True

        # ── Фаза 1: Ожидание отката после продажи ───────────────────
        if self.dca_wait_pullback:
            # Обновляем пик
            if price_usd > self.dca_peak_price:
                self.dca_peak_price = price_usd

            if self.dca_peak_price <= 0:
                self.dca_peak_price = price_usd
                self.log("📌 DCA: зафиксировали пик для отслеживания отката", "INFO")
                return

            drop_from_peak_pct = (self.dca_peak_price - price_usd) / self.dca_peak_price * 100

            # ── Умный реentri: AI/BrainFusion бычий → не ждём полного отката ────
            _ai_conf_now = float((self.last_ai or {}).get("confidence", 0) or 0)
            _ai_signal   = (self.last_ai or {}).get("ai_signal", "HOLD")
            _smart_reentry_possible = (
                Config.DCA_SMART_REENTRY_ENABLED
                and _ai_signal in ("BUY",)
                and _ai_conf_now >= Config.DCA_SMART_REENTRY_MIN_AI_CONF
            )
            # BrainFusion быстрый ре-вход: fusion консенсус бычий + небольшой откат
            _fusion_fast_reentry = (
                Config.FAST_REENTRY_ENABLED
                and _ai_signal == "BUY"
                and _ai_conf_now >= Config.FAST_REENTRY_MIN_CONF
                and _bf.is_bullish_consensus(_ai_conf_now)
            )
            # Выбираем наименьший требуемый откат из доступных режимов
            if _fusion_fast_reentry:
                pullback_needed = Config.FAST_REENTRY_PULLBACK_PCT
                _reentry_tag = (
                    f"🧠 fusion быстрый ре-вход (AI={_ai_conf_now:.0f}%, "
                    f"откат -{pullback_needed:.0f}%)"
                )
            elif _smart_reentry_possible:
                pullback_needed = Config.DCA_SMART_REENTRY_PULLBACK_PCT
                _reentry_tag = (
                    f"⚡ умный реentri (AI={_ai_conf_now:.0f}%, откат -{Config.DCA_SMART_REENTRY_PULLBACK_PCT:.0f}%)"
                )
            else:
                pullback_needed = Config.DCA_PULLBACK_WAIT_PCT
                _reentry_tag = f"стандартный откат -{Config.DCA_PULLBACK_WAIT_PCT:.0f}%"
            self.log(
                f"⏳ DCA ожидание отката: пик ${self.dca_peak_price:.8f} → "
                f"${price_usd:.8f} | откат {drop_from_peak_pct:.1f}% / нужно {pullback_needed:.0f}% "
                f"({_reentry_tag})",
                "INFO"
            )

            if drop_from_peak_pct >= pullback_needed:
                mode_label = "умный реentri (AI бычий)" if _smart_reentry_possible else "новый цикл после отката"
                # Временной фильтр: не открываем новый цикл в мёртвые часы
                if self._is_dead_hour():
                    self.log(
                        f"🌙 DCA: откат {drop_from_peak_pct:.1f}% достигнут, но мёртвый час "
                        f"({datetime.utcnow().hour:02d}:xx UTC) — ждём активного времени",
                        "INFO"
                    )
                    return
                self.log(
                    f"✅ DCA: откат {drop_from_peak_pct:.1f}% ≥ {pullback_needed:.0f}% — "
                    f"{mode_label}!",
                    "INFO"
                )
                self.dca_wait_pullback  = False
                self.dca_peak_price     = 0.0
                self.dca_entries_count  = 0
                self.dca_total_stake    = 0.0
                self._dca_buy(price_usd, grinch_ton, mode_label)
            return

        # ── Фаза 2: Проверка целевой прибыли портфеля ────────────────
        if self.open_trades:
            total_cost_ton, total_value_ton = self._dca_portfolio_value(grinch_ton)

            if total_cost_ton > 0 and total_value_ton > 0:
                portfolio_pct = (total_value_ton - total_cost_ton) / total_cost_ton * 100
                entries       = len(self.open_trades)
                total_grinch  = sum(t.get("amount", 0) for t in self.open_trades)
                profit_ton_abs = total_value_ton - total_cost_ton
                min_ton        = Config.MIN_PROFIT_TON_ABS

                # Показываем расширенный статус с индикатором каскада
                _cascade_tag = (
                    f" | 🎯 Ур.2 ({Config.DCA_CASCADE_LEVEL2_PCT:.0f}%) — держим остаток"
                    if self.dca_cascade_half_sold else
                    (f" | каскад вкл (Ур.1={Config.DCA_CASCADE_LEVEL1_PCT:.0f}%/Ур.2={Config.DCA_CASCADE_LEVEL2_PCT:.0f}%)"
                     if Config.DCA_CASCADE_ENABLED else "")
                )
                _compound_tag = (
                    f" | 🔄 компаунд +{self.dca_compound_bonus_ton:.1f} TON"
                    if self.dca_compound_bonus_ton > 0 else ""
                )
                _adapt_tag = " | ⚡ РАКЕТА" if _fast_market else ""
                self.log(
                    f"📊 DCA портфель: {entries} поз. | "
                    f"вложено {total_cost_ton:.2f} TON | "
                    f"сейчас {total_value_ton:.2f} TON | "
                    f"прибыль {portfolio_pct:+.1f}%{_cascade_tag}{_compound_tag}{_adapt_tag}",
                    "INFO"
                )

                # ── КАСКАДНЫЙ ВЫХОД ──────────────────────────────────
                if Config.DCA_CASCADE_ENABLED:
                    # Уровень 2: продаём остаток (уровень 1 уже был продан)
                    if self.dca_cascade_half_sold and portfolio_pct >= Config.DCA_CASCADE_LEVEL2_PCT:
                        if profit_ton_abs >= min_ton:
                            self.log(
                                f"🚀 КАСКАД Ур.2: портфель +{portfolio_pct:.1f}% ≥ "
                                f"+{Config.DCA_CASCADE_LEVEL2_PCT:.0f}% — продаём остаток! "
                                f"({total_grinch:.2f} GRINCH)",
                                "INFO"
                            )
                            closed = self._dca_sell_all(price_usd, grinch_ton, portfolio_pct)
                        else:
                            closed = False
                            self.log(
                                f"⏳ Каскад Ур.2: цель достигнута, но прибыль "
                                f"{profit_ton_abs:.3f} TON < мин {min_ton:.1f} — ждём",
                                "INFO"
                            )
                        if closed:
                            self.dca_wait_pullback = True
                            self.dca_peak_price    = price_usd
                            self._emit_signal("SELL", price_usd, self.last_ai)
                            self.log(
                                f"✅ Каскад завершён! Ждём откат "
                                f"-{Config.DCA_PULLBACK_WAIT_PCT:.0f}% от пика",
                                "INFO"
                            )
                        return

                    # Уровень 1: продаём 50% и держим остаток
                    if (not self.dca_cascade_half_sold
                            and portfolio_pct >= Config.DCA_CASCADE_LEVEL1_PCT):
                        if profit_ton_abs >= min_ton:
                            sold = self._dca_sell_partial(
                                price_usd, grinch_ton, portfolio_pct, sell_fraction=0.5
                            )
                            if sold:
                                self._emit_signal("SELL", price_usd, self.last_ai)
                        else:
                            self.log(
                                f"⏳ Каскад Ур.1: цель +{portfolio_pct:.1f}% достигнута, "
                                f"но прибыль {profit_ton_abs:.3f} TON < мин {min_ton:.1f} — ждём",
                                "INFO"
                            )
                        return

                else:
                    # ── Стандартный выход: продаём ВСЁ на целевом % ─────
                    if portfolio_pct >= Config.DCA_TARGET_PROFIT_PCT:
                        if profit_ton_abs < min_ton:
                            self.log(
                                f"⏳ DCA: цель +{portfolio_pct:.1f}% но прибыль "
                                f"{profit_ton_abs:.3f} TON < мин {min_ton:.1f} — ждём",
                                "INFO"
                            )
                        else:
                            self.log(
                                f"🎯 DCA ЦЕЛЬ: +{portfolio_pct:.1f}% ≥ "
                                f"+{Config.DCA_TARGET_PROFIT_PCT:.0f}% | "
                                f"{profit_ton_abs:.2f} TON — продаём ВСЁ!",
                                "INFO"
                            )
                        closed = self._dca_sell_all(price_usd, grinch_ton, portfolio_pct) \
                            if profit_ton_abs >= min_ton else False
                        if closed:
                            self.dca_wait_pullback = True
                            self.dca_peak_price    = price_usd
                            self._emit_signal("SELL", price_usd, self.last_ai)
                            self.log(
                                f"⏳ DCA: продали всё, ждём откат "
                                f"-{Config.DCA_PULLBACK_WAIT_PCT:.0f}%",
                                "INFO"
                            )
                        return

                # ── Докупка при падении (адаптивный триггер) ─────────
                # Не докупаем если уже продали половину по каскаду (ждём ракету вверх)
                if not self.dca_cascade_half_sold and self.dca_last_buy_price > 0:
                    drop_from_last_pct = (
                        (self.dca_last_buy_price - price_usd) / self.dca_last_buy_price * 100
                    )
                    # Адаптивный порог: рынок летит → докупаем агрессивнее
                    # В мёртвые часы — расширяем триггер (меньше ложных докупок в низком объёме)
                    _dead_now = self._is_dead_hour()
                    drop_trigger = (
                        Config.DCA_ADAPTIVE_FAST_DROP_PCT
                        if _fast_market
                        else Config.DCA_DROP_TRIGGER_PCT
                    )
                    if _dead_now and not _fast_market:
                        drop_trigger = drop_trigger * Config.DEAD_HOURS_DROP_MULT
                    _trigger_tag = (
                        f"⚡ адаптивный {drop_trigger:.0f}%" if _fast_market
                        else f"🌙 мёртвый час {drop_trigger:.1f}%" if _dead_now
                        else f"стандартный {drop_trigger:.0f}%"
                    )
                    _dca_cooldown_left = Config.DCA_REENTRY_COOLDOWN_SEC - (time.time() - self._last_dca_entry_ts)
                    if drop_from_last_pct >= drop_trigger:
                        if self.dca_entries_count < Config.DCA_MAX_ENTRIES and _dca_cooldown_left <= 0:
                            # Guard: не докупаем в "падающий нож" если AI уверен в SELL
                            _ai_sell_conf = float((self.last_ai or {}).get("confidence", 0) or 0)
                            _ai_sell_sig  = (self.last_ai or {}).get("ai_signal", "HOLD")
                            _dca_ai_blocked = (
                                Config.DCA_AI_SELL_BLOCK_CONF > 0
                                and _ai_sell_sig == "SELL"
                                and _ai_sell_conf >= Config.DCA_AI_SELL_BLOCK_CONF
                            )
                            if _dca_ai_blocked:
                                self.log(
                                    f"🛡️ DCA: блокировка докупки — AI SELL {_ai_sell_conf:.0f}% "
                                    f"≥ {Config.DCA_AI_SELL_BLOCK_CONF:.0f}% (падающий нож)",
                                    "WARN"
                                )
                            else:
                                self.log(
                                    f"📉 DCA ДОКУПКА: цена упала {drop_from_last_pct:.1f}% "
                                    f"(триггер: {_trigger_tag}) | "
                                    f"вход #{self.dca_entries_count + 1}",
                                    "INFO"
                                )
                                self._dca_buy(
                                    price_usd, grinch_ton,
                                    f"докупка #{self.dca_entries_count + 1} "
                                    f"(падение {drop_from_last_pct:.1f}%, {_trigger_tag})"
                                )
                        elif _dca_cooldown_left > 0:
                            self.log(
                                f"⏸️ DCA: кулдаун {_dca_cooldown_left:.0f}с до следующей докупки",
                                "INFO"
                            )
                        else:
                            self.log(
                                f"⏸️ DCA: лимит входов ({Config.DCA_MAX_ENTRIES}), "
                                f"ждём восстановления",
                                "WARN"
                            )
            return

        # ── Фаза 3: Нет позиций, не ждём — первый вход ───────────────
        if self.dca_entries_count == 0:
            # Временной фильтр: в мёртвые часы не открываем новый цикл
            if self._is_dead_hour():
                self.log(
                    f"🌙 DCA: мёртвый час ({datetime.utcnow().hour:02d}:xx UTC) — "
                    f"первый вход отложен до активного времени",
                    "INFO"
                )
                return
            self.log(
                f"🚀 DCA: нет позиций — открываем первый вход "
                f"({Config.DCA_STAKE_TON:.0f} TON @ ${price_usd:.8f})",
                "INFO"
            )
            self._dca_buy(price_usd, grinch_ton, "первый вход")

    def _dca_portfolio_value(self, grinch_ton_price):
        """Возвращает (суммарные затраты в TON, текущая стоимость в TON)."""
        fee      = Config.FEE_PCT / 100.0
        sell_gas = Config.SELL_GAS_TON
        buy_gas  = Config.BUY_GAS_TON

        total_cost_ton  = 0.0
        total_value_ton = 0.0

        for trade in self.open_trades:
            stake_ton = trade.get("stake_ton", 0) or 0
            amount    = trade.get("amount", 0) or 0
            total_cost_ton  += stake_ton + buy_gas
            # Ожидаемая выручка от продажи (за вычетом DEX-комиссии и газа)
            if grinch_ton_price > 0 and amount > 0:
                proceeds = amount * grinch_ton_price * (1 - fee) - sell_gas
                total_value_ton += max(proceeds, 0.0)

        return total_cost_ton, total_value_ton

    def _dca_buy(self, price_usd, grinch_ton, reason=""):
        """Открывает одну DCA позицию на DCA_STAKE_TON (+ compound-бонус если накоплен)."""
        stake_ton = Config.DCA_STAKE_TON
        # ── Компаундирование: прибавляем реинвест-бонус к первой покупке цикла ──
        if (Config.DCA_COMPOUND_ENABLED
                and self.dca_compound_bonus_ton > 0
                and self.dca_entries_count == 0):
            stake_ton = stake_ton + self.dca_compound_bonus_ton
            self.log(
                f"🔄 Компаунд: базовая ставка {Config.DCA_STAKE_TON:.1f} TON "
                f"+ бонус {self.dca_compound_bonus_ton:.2f} TON "
                f"= {stake_ton:.2f} TON (реинвест накоплен за прошлые циклы)",
                "INFO"
            )

        # Проверяем баланс
        bal     = self.exchange.get_balance() or {}
        ton_bal = bal.get("TON", 0) or 0
        buy_gas = 0.30
        reserve = Config.GAS_RESERVE_TON
        spendable = ton_bal - buy_gas - reserve

        if bal.get("error") or spendable < Config.MIN_STAKE_TON:
            why = bal.get("error") or (
                f"на кошельке {ton_bal:.3f} TON: после газа {buy_gas} + резерва "
                f"{reserve} TON остаётся {spendable:.3f} < мин {Config.MIN_STAKE_TON} TON"
            )
            self.log(f"⛔ DCA: нет средств для покупки ({reason}) — {why}", "WARN")
            return False

        if stake_ton > spendable:
            self.log(
                f"✂️ DCA: ставка {stake_ton:.0f} TON → урезаем до {spendable:.3f} TON "
                f"(недостаточный баланс)",
                "WARN"
            )
            stake_ton = spendable

        # ton_stake передаётся напрямую в place_order (DeDust-путь) —
        # мутировать Config.TRADE_AMOUNT небезопасно (race condition с дашбордом).
        try:
            # Используем price_usd как entry price; amount считается через stake/price
            amount = stake_ton / price_usd if price_usd > 0 else 0

            order = self.exchange.place_order("buy", amount, ton_stake=stake_ton)
            if not order or order.get("error"):
                err = (order or {}).get("error", "нет ответа")
                self.log(f"⚠️ DCA: ордер покупки не прошёл ({reason}) — {err}", "WARN")
                return False

            # Реальное количество GRINCH после свопа
            actual_grinch = (order.get("info") or {}).get("grinch_received", 0)
            if actual_grinch and actual_grinch > 0:
                amount = actual_grinch

            # SL/TP не нужны в DCA-режиме — сами управляем выходом
            sl = 0.0
            tp = price_usd * 100  # практически бесконечный TP (выход только через _dca_sell_all)

            trade = {
                "id":              order["id"],
                "symbol":          Config.SYMBOL,
                "side":            "buy",
                "entry_price":     price_usd,
                "entry_price_ton": grinch_ton,
                "amount":          round(amount, 6),
                "stake_ton":       round(stake_ton, 4),
                "stop_loss":       sl,
                "take_profit":     tp,
                "trail_pct":       0.0,
                "high_water":      price_usd,
                "opened_at":       datetime.utcnow().isoformat(),
                "pnl":             0.0,
                "status":          "open",
                "ai_confidence":   0.0,
                "dca_entry":       True,
                "dca_index":       self.dca_entries_count + 1,
                "breakeven_price": price_usd,
                "min_gross_pct":   Config.required_gross_pct_with_gas(stake_ton),
                "entry_regime":    "DCA",
                "entry_rsi":       0.0,
                "entry_atr_pct":   0.0,
                "entry_anomaly":   False,
                "entry_sm_score":  0.0,
                "entry_sm_label":  "",
                "entry_sm_buys_1h": 0,
                "entry_sm_sells_1h": 0,
                "entry_bo_signal": "FLAT",
                "entry_bo_score":  0.0,
                "entry_mom_signal": "CALM",
            }
            self.open_trades.append(trade)
            self.trades.append(trade)
            # total_trades теперь считается только в момент закрытия (там же,
            # где вызывается record_trade) — единая точка учёта, чтобы счётчик
            # никогда не расходился с журналом сделок bot_trades.
            self.exp.data["stats"] = dict(self.stats)
            # Объединяем с уже открытыми LONG-позициями в одну
            self._merge_long_trades()

            # Обновляем DCA-состояние
            self.dca_last_buy_price  = price_usd
            self.dca_entries_count  += 1
            self.dca_total_stake    += stake_ton
            self._last_dca_entry_ts  = time.time()   # кулдаун: фиксируем время входа
            # Персистируем timestamp в DB + JSON чтобы выжить перезапуск (и отказ БД)
            try:
                from settings_store import update_section
                update_section("trader_state", {
                    "last_dca_entry_ts": str(self._last_dca_entry_ts)
                })
            except Exception:
                pass

            self._emit_signal("BUY", price_usd, self.last_ai)

            self.log(
                f"✅ DCA вход #{self.dca_entries_count}: "
                f"{amount:.2f} GRINCH за {stake_ton:.2f} TON @ ${price_usd:.8f} "
                f"| итого вложено: {self.dca_total_stake:.2f} TON | {reason}",
                "INFO"
            )
            # Аналитический буфер: DCA-покупка
            try:
                from analytics_buffer import analytics_buffer as _ab
                _ab.push_trade("DCA_BUY", {
                    "price":    price_usd,
                    "stake_ton": stake_ton,
                    "regime":   (self.last_ai or {}).get("regime", {}) and
                                (self.last_ai or {}).get("regime", {}).get("name") or "DCA",
                    "ai_conf":  float((self.last_ai or {}).get("confidence", 0) or 0),
                    "dca_entries": self.dca_entries_count,
                })
            except Exception:
                pass
            return True
        finally:
            pass  # Config.TRADE_AMOUNT не изменялся — восстанавливать нечего

    def _dca_sell_partial(self, price_usd, grinch_ton, portfolio_pct, sell_fraction=0.5):
        """Каскадный выход: продаёт sell_fraction (0.5 = 50%) от всей позиции.
        Оставляет остаток открытым, пересчитывает stake_ton пропорционально."""
        if not self.open_trades:
            return False

        total_grinch = sum(t.get("amount", 0) for t in self.open_trades)
        if total_grinch <= 0:
            return False

        sell_amount = round(total_grinch * sell_fraction, 6)
        if sell_amount <= 0:
            return False

        self.log(
            f"🎯 КАСКАД Ур.1: продаём {sell_fraction*100:.0f}% позиции "
            f"({sell_amount:.4f} GRINCH) @ +{portfolio_pct:.1f}% прибыли | "
            f"держим {total_grinch - sell_amount:.4f} GRINCH до Ур.2 (+{Config.DCA_CASCADE_LEVEL2_PCT:.0f}%)",
            "INFO"
        )

        if self.exchange.mode == "dedust":
            # AMM preflight: считаем min_net_ton для доли продажи
            _total_stake   = sum(t.get("stake_ton", 0) or 0 for t in self.open_trades)
            _n_entries     = max(1, len(self.open_trades))
            _min_net_ton   = (_total_stake + Config.BUY_GAS_TON * _n_entries) * sell_fraction
            sell_result = self.exchange.place_order("sell", sell_amount, min_net_ton=_min_net_ton)
            if not sell_result or (sell_result.get("error") and not sell_result.get("amm_blocked")):
                # Retry только при сетевых ошибках, но НЕ при блокировке AMM preflight
                self.log("⚠️ Каскад: продажа Ур.1 не прошла — retry через 5с…", "WARN")
                time.sleep(5)
                sell_result = self.exchange.place_order("sell", sell_amount, min_net_ton=_min_net_ton)
            if not sell_result or sell_result.get("error"):
                err = (sell_result or {}).get("error", "нет ответа")
                blocked = (sell_result or {}).get("amm_blocked", False)
                level = "WARN"
                if blocked:
                    self.log(f"🛡️ Каскад: продажа заблокирована AMM preflight — {err}", level)
                else:
                    self.log(f"⚠️ Каскад: продажа Ур.1 не исполнена после retry — {err}", level)
                return False
            self.log(
                f"✅ Каскад Ур.1: продажа исполнена | id={sell_result.get('id', '—')}",
                "INFO"
            )

        fee      = Config.FEE_PCT / 100.0
        buy_gas  = Config.BUY_GAS_TON
        sell_gas = Config.SELL_GAS_TON

        # Уменьшаем amount/stake_ton во всех открытых позициях пропорционально
        # (под общим локом — см. _ot_lock: без него wallet_manager мог прочитать
        # позицию между обновлением amount и stake_ton и получить рваные данные).
        partial_pnl = 0.0
        with self._ot_lock:
            for trade in self.open_trades:
                old_amount = trade.get("amount", 0) or 0
                old_stake  = trade.get("stake_ton", 0) or 0
                new_amount = round(old_amount * (1 - sell_fraction), 6)
                new_stake  = round(old_stake  * (1 - sell_fraction), 4)
                # PNL от проданной части
                sold_part = old_amount * sell_fraction
                if grinch_ton > 0 and sold_part > 0:
                    proceeds = sold_part * grinch_ton * (1 - fee) - sell_gas * sell_fraction
                    cost     = old_stake * sell_fraction + buy_gas * sell_fraction
                    partial_pnl += round(proceeds - cost, 6)
                trade["amount"]    = new_amount
                trade["stake_ton"] = new_stake

            self.dca_total_stake   = sum(t.get("stake_ton", 0) for t in self.open_trades)
        self.dca_cascade_half_sold = True
        self._save_volatile_state()  # cascade флаг переживёт рестарт
        # Лок: self.stats мутируется из нескольких потоков — без него возможна
        # гонка при одновременном закрытии позиций (потерянный инкремент).
        with self._close_lock:
            self.stats["total_pnl"] = round(self.stats["total_pnl"] + partial_pnl, 6)
            # Каскадная частичная продажа закрывает часть позиции как отдельную
            # сделку статистически — считаем total_trades вместе с winning_trades,
            # иначе winning_trades может превысить total_trades (некорректный winrate).
            self.stats["total_trades"] += 1
            if partial_pnl > 0:
                self.stats["winning_trades"] += 1

        try:
            self.exp.save_open_trades(self._combined_open_trades())
        except Exception:
            pass

        # ── Постоянная история сделок (bot_trades) — иначе счётчик total_pnl
        # растёт, а аудиторского следа "откуда взялась прибыль" не остаётся.
        try:
            now_iso = datetime.utcnow().isoformat()
            self.exp.record_trade({
                "id":           f"cascade1_{int(time.time())}",
                "side":         "sell_partial",
                "amount":       sell_amount,
                "entry_price":  None,
                "exit_price":   price_usd,
                "pnl":          partial_pnl,
                "opened_at":    None,
                "closed_at":    now_iso,
                "close_reason": f"dca_cascade_level1_{portfolio_pct:.1f}pct",
                "status":       "closed",
                "outcome":      "win" if partial_pnl > 0 else "loss",
            }, self.stats, self.ai)
        except Exception as e:
            self.log(f"Запись сделки в историю (каскад Ур.1): {e}", "WARN")

        self.log(
            f"✅ Каскад Ур.1 зафиксирован: PNL ≈ {partial_pnl:+.4f} TON | "
            f"остаток {total_grinch*(1-sell_fraction):.4f} GRINCH ждёт +{Config.DCA_CASCADE_LEVEL2_PCT:.0f}%",
            "SELL"
        )
        return True

    def _dca_sell_all(self, price_usd, grinch_ton, portfolio_pct):
        """Продаёт все DCA позиции одной продажей суммарного GRINCH."""
        if not self.open_trades:
            return False

        total_grinch = sum(t.get("amount", 0) for t in self.open_trades)
        total_stake  = sum(t.get("stake_ton", 0) for t in self.open_trades)

        if total_grinch <= 0:
            return False

        # ── ЖЕЛЕЗНЫЙ ЗАМОК ONLY_PROFIT_EXIT ──────────────────────────────────
        # Второй барьер (первый — в вызывающем коде). Блокируем продажу в убыток
        # независимо от того, кто вызвал эту функцию.
        if Config.ONLY_PROFIT_EXIT and portfolio_pct < 0:
            self.log(
                f"🛡️ ONLY_PROFIT_EXIT (_dca_sell_all): портфель {portfolio_pct:+.1f}% — "
                f"продажа заблокирована, ждём возврата в прибыль.",
                "WARN"
            )
            return False

        # ── Консолидация: продаём ВЕСЬ GRINCH на балансе одной сделкой,
        # не только то, что учтено во внутренних позициях — так пыль/
        # расхождения не остаются непроданными после DCA-выхода.
        sell_amount = total_grinch
        if self.exchange.mode == "dedust":
            try:
                real_bal = self.exchange.get_balance() or {}
                real_grinch = float(real_bal.get("GRINCH", 0) or 0)
                reserve = Config.GRINCH_RESERVE if (
                    Config.SHORT_TRADING_ENABLED or self.open_short_trades
                ) else 0.0
                sweepable = max(0.0, real_grinch - reserve)
                if sweepable > sell_amount:
                    self.log(
                        f"🧹 Консолидация: на балансе {real_grinch:.4f} GRINCH "
                        f"(учтено {total_grinch:.4f}) — продаём всё "
                        f"{sweepable:.4f} одной сделкой",
                        "INFO"
                    )
                    sell_amount = sweepable
            except Exception as _sw_e:
                self.log(f"⚠️ Не удалось сверить баланс для консолидации DCA: {_sw_e}", "WARN")

        self.log(
            f"💸 DCA: продаём {sell_amount:.4f} GRINCH "
            f"(прибыль портфеля {portfolio_pct:+.1f}%)...",
            "INFO"
        )

        if self.exchange.mode == "dedust":
            # AMM preflight: полная стоимость всех позиций
            _total_stake  = sum(t.get("stake_ton", 0) or 0 for t in self.open_trades)
            _n_entries    = max(1, len(self.open_trades))
            _min_net_ton  = _total_stake + Config.BUY_GAS_TON * _n_entries
            self.log(
                f"🔍 AMM preflight: нужно ≥ {_min_net_ton:.3f} TON нетто "
                f"(стейк {_total_stake:.3f} + газ покупки {Config.BUY_GAS_TON * _n_entries:.3f})",
                "INFO"
            )
            sell_result = self.exchange.place_order("sell", sell_amount, min_net_ton=_min_net_ton)
            if not sell_result or (sell_result.get("error") and not sell_result.get("amm_blocked")):
                # Retry только при сетевых ошибках, НЕ при блокировке AMM preflight
                self.log("⚠️ DCA: продажа не прошла — retry через 5с…", "WARN")
                time.sleep(5)
                sell_result = self.exchange.place_order("sell", sell_amount, min_net_ton=_min_net_ton)
            if not sell_result or sell_result.get("error"):
                err = (sell_result or {}).get("error", "нет ответа")
                if (sell_result or {}).get("amm_blocked"):
                    self.log(f"🛡️ DCA: продажа заблокирована AMM preflight — {err}", "WARN")
                else:
                    self.log(f"⚠️ DCA: продажа не исполнена после retry — {err}. Позиции остаются.", "WARN")
                return False
            self.log(
                f"✅ DCA: продажа GRINCH → TON исполнена | "
                f"id={sell_result.get('id', '—')}",
                "INFO"
            )

        # Закрываем все позиции виртуально
        fee      = Config.FEE_PCT / 100.0
        buy_gas  = Config.BUY_GAS_TON
        sell_gas = Config.SELL_GAS_TON

        total_pnl = 0.0
        for trade in list(self.open_trades):
            amount    = trade.get("amount", 0) or 0
            stake_ton = trade.get("stake_ton", 0) or 0
            if grinch_ton > 0 and amount > 0:
                proceeds   = amount * grinch_ton * (1 - fee) - sell_gas
                total_cost = stake_ton + buy_gas
                pnl_ton    = round(proceeds - total_cost, 6)
            else:
                pnl_ton = 0.0
            trade["pnl"]          = pnl_ton
            trade["exit_price"]   = price_usd
            trade["closed_at"]    = datetime.utcnow().isoformat()
            trade["close_reason"] = f"dca_target_{portfolio_pct:.1f}pct"
            trade["status"]       = "closed"
            trade["outcome"]      = "win" if pnl_ton > 0 else "loss"
            total_pnl            += pnl_ton
            # Лок: self.stats мутируется из нескольких потоков — без него
            # возможна гонка при одновременном закрытии позиций.
            with self._close_lock:
                self.stats["total_pnl"] = round(self.stats["total_pnl"] + pnl_ton, 6)
                # total_trades раньше не увеличивался в этой функции — winning_trades
                # рос сам по себе, что могло дать winrate>100% или расхождение со
                # счётчиком сделок. Считаем total_trades вместе с winning_trades.
                self.stats["total_trades"] = self.stats.get("total_trades", 0) + 1
                if pnl_ton > 0:
                    self.stats["winning_trades"] += 1
            # AI feedback
            try:
                ai_snap  = self.last_ai or {}
                reg_name = (ai_snap.get("regime") or {}).get("name", "UNKNOWN")
                ai_conf  = float(ai_snap.get("confidence", 0) or 0)
                self.ai.feedback(outcome=trade["outcome"], pnl=float(pnl_ton),
                                 regime=reg_name, conf=ai_conf)
            except Exception as e:
                self.log(f"⚠️ AI feedback (DCA sell-all): {e}", "WARN")
            # Сохраняем в историю
            for t in self.trades:
                if t["id"] == trade["id"]:
                    t.update(trade)
                    break
            # ── Постоянная история сделок (bot_trades) — единственный источник
            # аудиторского следа "откуда взялась прибыль" на дашборде.
            try:
                self.exp.record_trade(dict(trade), self.stats, self.ai)
            except Exception as e:
                self.log(f"Запись сделки в историю (DCA sell-all): {e}", "WARN")

        # ── Компаундирование: накапливаем бонус к следующей ставке ──────
        if Config.DCA_COMPOUND_ENABLED and total_pnl > 0:
            bonus = round(total_pnl * Config.DCA_COMPOUND_RATIO, 4)
            old_bonus = self.dca_compound_bonus_ton
            self.dca_compound_bonus_ton = min(
                self.dca_compound_bonus_ton + bonus,
                Config.DCA_COMPOUND_MAX_TON
            )
            self.log(
                f"🔄 Компаунд: прибыль {total_pnl:.3f} TON × {Config.DCA_COMPOUND_RATIO:.0%} "
                f"= +{bonus:.3f} TON → бонус {old_bonus:.2f} → {self.dca_compound_bonus_ton:.2f} TON "
                f"(лимит {Config.DCA_COMPOUND_MAX_TON:.0f} TON)",
                "INFO"
            )
            self._save_volatile_state()  # compound bonus переживёт рестарт

        # Снимаем dca_entries ПЕРЕД сбросом (иначе советник получит 0)
        _dca_entries_snap = self.dca_entries_count
        self.open_trades         = []
        self.dca_entries_count   = 0
        self.dca_total_stake     = 0.0
        self.dca_cascade_half_sold = False   # сбрасываем каскад-флаг на новый цикл

        # ── AI Советник: ОДИН триггер на закрытие DCA-цикла ──────────
        try:
            from ai_advisor import notify_trade_closed
            _ai_snap = self.last_ai or {}
            notify_trade_closed(total_pnl, {
                "pnl_ton":      round(total_pnl, 4),
                "stake_ton":    total_stake,
                "pnl_pct":      round(portfolio_pct, 2),
                "close_reason": f"dca_target_{portfolio_pct:.1f}pct",
                "strategy":     "DCA",
                "dca_entries":  _dca_entries_snap,
                "exit_price":   price_usd,
                "outcome":      "win" if total_pnl >= 0 else "loss",
                "regime":       (_ai_snap.get("regime") or {}).get("name", "DCA"),
                "ai_conf":      float(_ai_snap.get("confidence", 0) or 0),
            })
        except Exception:
            pass

        # ── Аналитический буфер: DCA-продажа ─────────────────────────
        try:
            from analytics_buffer import analytics_buffer as _ab
            ai_snap = self.last_ai or {}
            _ab.push_trade("DCA_SELL", {
                "price":        price_usd,
                "stake_ton":    total_stake,
                "pnl_ton":      total_pnl,
                "pnl_pct":      round(portfolio_pct, 2),
                "regime":       (ai_snap.get("regime") or {}).get("name") or "DCA",
                "ai_conf":      float(ai_snap.get("confidence", 0) or 0),
                "close_reason": f"dca_target_{portfolio_pct:.1f}pct",
                "dca_entries":  self.dca_entries_count,
            })
        except Exception:
            pass

        self.log(
            f"🟩 DCA цикл завершён: продано {total_grinch:.4f} GRINCH | "
            f"суммарный PNL ≈ {total_pnl:+.4f} TON | "
            f"портфель был +{portfolio_pct:.1f}%",
            "SELL"
        )
        # ── BrainFusion: обратная связь после закрытия ───────────────────
        try:
            _bf.on_trade_closed(total_pnl, was_scalp=self._last_entry_was_scalp)
        except Exception:
            pass
        self._last_entry_was_scalp = False  # сбрасываем флаг скальпа

        # Обновляем память
        try:
            self.exp.save_open_trades([])
            from price_feed import price_feed as _pf
            self.exp.record_balance(
                self._get_balance_cached(),
                _pf.get("GRINCH") or price_usd,
                force=True,
            )
        except Exception:
            pass

        # (второй вызов notify убран — один notify на один DCA-цикл)
        return True

    # ──────────────────────────────────────────
    # Аналитический буфер: снимок тика
    # ──────────────────────────────────────────
    def _push_tick_analytics(self) -> None:
        """Пушим полный снимок текущего тика в analytics_buffer.
        Вызывается в конце каждого тика (DCA и AI режим).
        Не должен ломать торговлю — все ошибки подавляются.
        """
        try:
            from analytics_buffer import analytics_buffer as _ab
            from price_feed import price_feed as _pf
            import liquidity_guard as _lg

            ai      = self.last_ai or {}
            regime  = ai.get("regime") or {}
            bo      = ai.get("breakout") or {}
            mom     = ai.get("momentum") or {}

            price_usd = _pf.get("GRINCH") or 0.0
            price_ton = _pf.get_grinch_ton_price() or 0.0

            # ── DCA прогресс ──────────────────────────────────────────
            dca_profit_pct  = 0.0
            dca_profit_ton  = 0.0
            dca_avg_price   = 0.0
            if Config.DCA_MODE and self.open_trades and price_ton > 0:
                try:
                    cost_ton, val_ton = self._dca_portfolio_value(price_ton)
                    if cost_ton > 0:
                        dca_profit_pct = (val_ton - cost_ton) / cost_ton * 100
                        dca_profit_ton = val_ton - cost_ton
                    total_amt   = sum(t.get("amount", 0) for t in self.open_trades)
                    total_stake = sum(t.get("stake_ton", 0) for t in self.open_trades)
                    dca_avg_price = total_stake / total_amt if total_amt > 0 else 0
                except Exception:
                    pass

            # ── Ликвидность ───────────────────────────────────────────
            liq_usd = 0.0
            try:
                liq_usd = float(_lg.get_status().get("current_liq", 0) or 0)
            except Exception:
                pass

            # ── Баланс ────────────────────────────────────────────────
            ton_bal = 0.0
            try:
                bal = self._get_balance_cached()
                ton_bal = float(bal.get("TON", 0) or 0)
            except Exception:
                pass

            # ── Умные деньги ──────────────────────────────────────────
            sm       = self.last_sm or {}
            sm_score = float(sm.get("score", 0) or 0)
            sm_early = bool(sm.get("early_buy", False))

            # ── Последнее решение ──────────────────────────────────────
            last_dec = self.decision_log[-1] if self.decision_log else {}

            _ab.push_tick({
                "price_usd":      price_usd,
                "price_ton":      price_ton,
                "rsi":            float(ai.get("rsi") or last_dec.get("rsi") or 50),
                "adx":            float(regime.get("adx") or 0),
                "atr_pct":        float(regime.get("atr_pct") or 0),
                "bb_pct":         float(ai.get("bb_pct") or 0),
                "vol_ratio":      float(ai.get("vol_ratio") or 1.0),
                "macd_hist":      float(ai.get("macd_hist") or 0),
                "stoch_rsi":      float(ai.get("stoch_rsi") or 0.5),
                "regime":         regime.get("name") or last_dec.get("regime") or "?",
                "ai_signal":      ai.get("ai_signal") or last_dec.get("ai_sig") or "HOLD",
                "ai_conf":        float(ai.get("confidence") or last_dec.get("conf") or 0),
                "prob_up":        float(ai.get("prob_up") or 0),
                "prob_down":      float(ai.get("prob_down") or 0),
                "var_ratio":      float(ai.get("var_ratio") or 1.0),
                "pump":           str(ai.get("pump") or "NONE"),
                "anomaly":        bool((ai.get("anomaly") or {}).get("detected", False)),
                "momentum":       str(mom.get("signal") or "CALM"),
                "breakout":       str(bo.get("signal") or "FLAT"),
                "entry_quality":  self.last_entry.get("quality") or last_dec.get("quality") or "?",
                "entry_score":    int(self.last_entry.get("score") or last_dec.get("score") or 0),
                "sm_score":       sm_score,
                "sm_early":       sm_early,
                "final_signal":   last_dec.get("result") or "HOLD",
                "blocked":        bool(last_dec.get("blocked", False)),
                "blocked_reason": str(last_dec.get("reason") or ""),
                "open_positions": len(self.open_trades),
                "portfolio_pnl":  float(self.stats.get("total_pnl", 0)),
                "ton_balance":    ton_bal,
                "liq_usd":        liq_usd,
                "dca_entries":    self.dca_entries_count,
                "dca_avg_price":  dca_avg_price,
                "dca_profit_pct": round(dca_profit_pct, 4),
                "dca_profit_ton": round(dca_profit_ton, 4),
            })
        except Exception:
            pass  # буфер НИКОГДА не ломает торговлю

    # ──────────────────────────────────────────
    # Торговый тик
    # ──────────────────────────────────────────
    def _run_market_analysis_only(self) -> None:
        """Запускает ТОЛЬКО анализ рынка (свечи → TA → AI) без каких-либо сделок.
        Используется когда торговля выключена вручную, чтобы:
        - last_ai / last_analysis оставались актуальными для дашборда
        - тики в bot_ticks имели реальные regime/signal/conf данные
        Все ошибки подавляются — не должен влиять на основной цикл."""
        try:
            ohlcv = self.exchange.get_ohlcv(limit=100)
            if not ohlcv:
                return
            result = analyze(ohlcv)
            self.last_analysis = result
            ai = self.ai.analyze(ohlcv)
            self.last_ai = ai
            # Обновляем BrainFusion без торговых решений
            try:
                _bf.update_ai(ai)
                _bf.update_ta(result)
            except Exception:
                pass
        except Exception:
            pass  # никогда не ломаем цикл

    def _tick(self):
        # ── Ручной выключатель торговли: блокирует ОБА режима (DCA и AI) ──
        if self._trading_disabled_guard():
            # Анализируем рынок и пишем тики в bot_ticks даже при выключенной торговле —
            # цены, RSI, режим нужны для советника и дашборда.
            self._run_market_analysis_only()
            try:
                self._push_tick_analytics()
            except Exception:
                pass
            return

        # ── DCA режим: полностью заменяет AI-логику ─────────────────
        if Config.DCA_MODE:
            try:
                self._tick_dca()
            except Exception as e:
                self.log(f"⚠️ DCA тик: {e}", "ERROR")
            # Пушим аналитику после каждого DCA-тика
            try:
                self._push_tick_analytics()
            except Exception:
                pass
            return

        # ── Защита прибыли + детектор крупных продаж в AI-режиме ────
        try:
            from price_feed import price_feed as _pf
            _ls_price = _pf.get("GRINCH") or 0.0
            _ls_gton  = _pf.get_grinch_ton_price() or 0.0
            if _ls_price > 0:
                # Сначала — защита прибыли (если +N TON И падает → продаём и выходим)
                if self._check_profit_protection(_ls_price, _ls_gton):
                    return
                # Затем — безусловная покупка на крупной продаже
                self._check_large_sell_dca(_ls_price, _ls_gton)
        except Exception as _lse:
            self.log(f"⚠️ Profit/LargeSell check (AI mode): {_lse}", "WARN")

        ohlcv  = self.exchange.get_ohlcv(limit=100)
        result = analyze(ohlcv)
        self.last_analysis = result   # кэш для get_status() — не пересчитываем каждые 2с
        ai     = self.ai.analyze(ohlcv)
        self.last_ai = ai

        # ── BrainFusion: обновляем единый мозг ───────────────────────────
        try:
            _bf.update_ai(ai)
            _bf.update_ta(result)
            # ── Инъекция живого ордер-флоу DEX (DexScreener txns) ────────
            if Config.ORDER_FLOW_INJECT_ENABLED:
                try:
                    from coin_info import coin_info as _ci
                    _ci_data = _ci.get_market("GRINCH")
                    _buy_r = _ci_data.get("ratio_h1") or _ci_data.get("ratio_h6") or 0.5
                    _b_h1  = _ci_data.get("buys_h1") or 0
                    _s_h1  = _ci_data.get("sells_h1") or 0
                    _total_h1 = _b_h1 + _s_h1
                    _net_flow = ((_b_h1 - _s_h1) / _total_h1 * 100.0) if _total_h1 > 0 else 0.0
                    import ai_engine as _ae
                    _ae.inject_order_flow(float(_buy_r), _net_flow)
                except Exception:
                    pass
            # Обновляем баланс кошелька для анализа в мозге
            from price_feed import price_feed as _pf2
            _gton = _pf2.get_grinch_ton_price() or 0.0
            _bal  = getattr(self, "_last_balance_cache", {}) or {}
            _open_pnl = 0.0
            if self.open_trades and _gton > 0:
                _stake0 = self.open_trades[0].get("stake_ton", 0)
                _amt0   = self.open_trades[0].get("amount", 0)
                _ep0    = self.open_trades[0].get("entry_price_ton", 0)
                if _ep0 > 0 and _amt0 > 0:
                    _open_pnl = (_gton - _ep0) / _ep0 * 100.0
            _bf.update_wallet(
                ton_bal=float(_bal.get("TON", 0) or 0),
                grinch_bal=float(_bal.get("GRINCH", 0) or 0),
                grinch_price_ton=_gton,
                open_pnl_pct=_open_pnl,
            )
        except Exception as _bfe:
            pass

        signal      = result["signal"]
        ai_signal   = ai.get("ai_signal", "HOLD")
        price       = result["price"]
        conf        = ai.get("confidence", 0)
        rsi         = result.get("rsi", 50)
        vol_ratio   = result.get("vol_ratio", 1.0)
        regime      = ai.get("regime", {}) or {}
        regime_name = regime.get("name", "?")
        anomaly     = ai.get("anomaly", {}).get("detected", False)

        # ── Качество точки входа (A/B/C) — многофакторный скоринг ─────────
        entry_quality  = result.get("entry_quality", "C")
        entry_score    = result.get("entry_score", 0)
        entry_reasons  = result.get("entry_reasons", [])
        self.last_entry = {
            "quality":  entry_quality,
            "score":    entry_score,
            "reasons":  entry_reasons,
            "vol_ratio": result.get("vol_ratio", 1.0),
            "stoch_rsi": result.get("stoch_rsi", 0.5),
        }

        # Динамические параметры по грейду:
        #   A (≥7 очков) — элитный вход: 1 подтверждение, откат -0.3%
        #   B (≥3 очков) — стандарт:    2 подтверждения, откат -0.8%
        #   C (<3 очков) — слабый:      3 подтверждения, откат -1.5%
        _grade_params = {
            "A": {"confirm": 1, "pullback": 0.3},
            "B": {"confirm": 2, "pullback": Config.SMART_BUY_PULLBACK_PCT},
            "C": {"confirm": 3, "pullback": 1.5},
        }
        _gp = _grade_params.get(entry_quality, _grade_params["B"])
        confirm_needed = _gp["confirm"]
        pullback_pct   = _gp["pullback"]

        # Если SL/TP закрыл все позиции и разослал SELL — завершаем тик,
        # чтобы в этом же тике не открыть BUY поверх ещё не сведённого
        # пользовательского состояния (гонка SELL→BUY в одном окне).
        if self._check_stop_loss_take_profit(price):
            self._clear_pending_buy()
            return

        # ── Smart BUY: проверяем отложенный вход ───────────────────────────
        # Если ожидаем откат к лучшей цене — проверяем достигнута ли цель.
        if self._pending_buy and not self.open_trades:
            pb = self._pending_buy
            # Если ордер восстановлен из DB — нет ai/analysis: исполняем сразу
            _pb_mode = pb.get("mode_params") or {}   # скальп/памп параметры из момента сигнала
            if pb.get("restored"):
                self.log(
                    f"🔄 Smart BUY восстановлен после рестарта @ ${price:.8f} (цель была ${pb.get('target', 0):.8f})",
                    "INFO"
                )
                # Передаём mode_params из сохранённого ордера — иначе скальп-вход
                # откроется без скальп-TP/trail и с неправильным TP-флором.
                opened = self._open_trade("buy", price, result, ai, mode_params=_pb_mode)
                if opened:
                    self._buy_confirm_count = 0
                    self._last_entry_was_scalp = bool(_pb_mode.get("trail_pct"))
                    self._emit_signal("BUY", price, ai)
                self._clear_pending_buy()
                return
            pb["ticks_left"] -= 1
            if price <= pb["target"]:
                # Цена откатилась к цели — покупаем по лучшей цене!
                self.log(
                    f"🎯 Smart BUY: откат поймали! Сигнал был ${pb['signal_price']:.8f}, "
                    f"покупаем по ${price:.8f} (экономия {(pb['signal_price']-price)/pb['signal_price']*100:.2f}%)",
                    "INFO"
                )
                opened = self._open_trade("buy", price, pb["analysis"], pb["ai"], mode_params=_pb_mode)
                if opened:
                    self._buy_confirm_count = 0
                    self._last_entry_was_scalp = bool(_pb_mode.get("trail_pct"))
                    self._emit_signal("BUY", price, pb["ai"])
                self._clear_pending_buy()
                return
            elif pb["ticks_left"] <= 0:
                # Время вышло — берём по текущей рыночной цене
                self.log(
                    f"⏱️ Smart BUY: откат не пришёл за {Config.SMART_BUY_MAX_WAIT_TICKS} тика, "
                    f"покупаем по рынку ${price:.8f}",
                    "INFO"
                )
                opened = self._open_trade("buy", price, pb["analysis"], pb["ai"], mode_params=_pb_mode)
                if opened:
                    self._buy_confirm_count = 0
                    self._last_entry_was_scalp = bool(_pb_mode.get("trail_pct"))
                    self._emit_signal("BUY", price, pb["ai"])
                self._clear_pending_buy()
                return
            else:
                # Ещё ждём
                self.log(
                    f"⏳ Smart BUY: ждём откат до ${pb['target']:.8f} "
                    f"(сейчас ${price:.8f}, осталось {pb['ticks_left']} тика)",
                    "INFO"
                )
                return

        # ── Сигнал «умных денег» (мониторинг кошельков пула) ───────────
        sm = None
        wt = getattr(self, "wallet_tracker", None)
        if wt is not None:
            try:
                sm = wt.get_signal()
            except Exception:
                sm = None
        self.last_sm = sm
        sm_score = sm["score"] if sm else 0.0
        sm_early = bool(sm and sm.get("early_buy"))

        if anomaly:
            self.log(f"⚠️ АНОМАЛИЯ! Z-цена={ai['anomaly']['z_price']}", "WARN")

        # ══════════════════════════════════════════════════════════════════
        # ПОЛНАЯ АВТОНОМИЯ AI: AI — единственный распорядитель сделок.
        # Технический сигнал (signal) — лишь дополнительный контекст для AI.
        # AI выбирает направление сам, используя уверенность, режим рынка,
        # данные умных денег и расчёт комиссий.
        # ══════════════════════════════════════════════════════════════════
        final_signal = "HOLD"
        signal_source = ""  # для лога: откуда взялся финальный сигнал

        if Config.AI_AUTONOMOUS_MODE:
            # Порог уверенности AI (смягчается умными деньгами)
            min_conf = Config.AI_AUTONOMOUS_MIN_CONF
            if sm_score >= Config.SMART_MONEY_BOOST_AT or sm_early:
                min_conf = max(
                    Config.SMART_MONEY_MIN_FLOOR,
                    min_conf - Config.SMART_MONEY_CONF_BONUS,
                )
            if ai_signal != "HOLD" and conf >= min_conf:
                final_signal = ai_signal
                signal_source = "AI🤖"
                if signal == ai_signal:
                    signal_source = "AI🤖+ТА✅"  # технический анализ подтвердил
        else:
            # Легаси-режим: требуем совпадения технического и AI сигналов
            if signal == ai_signal and signal != "HOLD":
                final_signal = signal
                signal_source = "ТА+AI"
            elif ai_signal != "HOLD" and conf >= Config.AI_OVERRIDE_CONFIDENCE:
                final_signal = ai_signal
                signal_source = "AI-override"

        # ── Счётчик подтверждений ───────────────────────────────────────
        # В автономном режиме AI достаточно 1 подтверждения для A/B, 2 для C
        if final_signal == "BUY":
            self._buy_confirm_count += 1
        else:
            self._buy_confirm_count = 0

        # ── Фильтры входа (ТОЛЬКО для BUY) ─────────────────────────────
        blocked = None
        fee_feasible_reason = ""

        if final_signal == "BUY":
            hard_override     = conf >= Config.AI_HARD_OVERRIDE_CONFIDENCE
            mean_rev_override = (
                rsi <= Config.RSI_OVERSOLD_REVERSAL and
                conf >= Config.REVERSAL_AI_MIN
            )
            # AI ПОЛНЫЕ ПРАВА: при уверенности >= порога ATR-фильтр снимается
            ai_full_rights_active = (
                Config.AI_FULL_RIGHTS and
                conf >= Config.AI_FULL_RIGHTS_MIN_CONF
            )

            # ── Кулдаун после убытка ──────────────────────────────────────
            _loss_cd_left = Config.LOSS_COOLDOWN_SEC - (time.time() - self._last_loss_ts)
            if _loss_cd_left > 0 and not hard_override:
                blocked = f"кулдаун после убытка: {int(_loss_cd_left)}с (защита от «падающего ножа»)"

            # ── Расчёт комиссионной реалистичности ─────────────────────
            # Минимальный % движения цены нужен, чтобы покрыть:
            #   • DEX-комиссия покупки: 1% от суммы
            #   • DEX-комиссия продажи: 1% от суммы
            #   • Газ покупки: ~0.103 TON (фикс.)
            #   • Газ продажи: ~0.253 TON (фикс.)
            #   • Целевая нетто-прибыль: +20%
            stake_est = Config.TRADE_AMOUNT  # оценка без баланса
            min_gross_needed = Config.required_gross_pct_with_gas(stake_est)

            # ATR как прокси волатильности: можно ли ожидать такое движение?
            # При AI_FULL_RIGHTS + достаточной уверенности — ATR-проверка снимается
            atr_pct = (ai.get("regime", {}) or {}).get("atr_pct", 0)
            if atr_pct > 0 and not ai_full_rights_active:
                atr_capacity = atr_pct * Config.AI_ATR_FEASIBILITY_MULT
                if atr_capacity < min_gross_needed and not hard_override:
                    fee_feasible_reason = (
                        f"ATR={atr_pct:.1f}%×{Config.AI_ATR_FEASIBILITY_MULT}"
                        f"={atr_capacity:.1f}% < нужно {min_gross_needed:.1f}% (комиссии+20%)"
                    )

            # ── Применяем фильтры по приоритету ───────────────────────
            if self.exp.is_paused():
                blocked = "ИИ-пауза: просадка капитала"
            elif sm_score <= Config.SMART_MONEY_BLOCK and not hard_override and not ai_full_rights_active:
                blocked = f"умные деньги распродают ({sm_score:+.2f})"
            elif conf < (Config.AI_AUTONOMOUS_MIN_CONF if Config.AI_AUTONOMOUS_MODE
                         else Config.MIN_AI_CONFIDENCE):
                blocked = f"AI уверенность {conf}% < порога"
            elif mean_rev_override:
                self.log(
                    f"📈 Mean Reversion: RSI={rsi:.1f} + AI={conf}% → вход в {regime_name}",
                    "INFO"
                )
            elif fee_feasible_reason and not hard_override and not ai_full_rights_active:
                # ATR недостаточен для покрытия комиссий и цели — рынок стоит
                blocked = f"рынок слишком спокойный: {fee_feasible_reason}"
            elif Config.TREND_FILTER and regime_name == "DOWNTREND" and not hard_override and not ai_full_rights_active:
                blocked = "нисходящий тренд (AI недостаточно уверен для входа)"
            elif ai_full_rights_active and not hard_override:
                self.log(
                    f"🤖 AI ПОЛНЫЕ ПРАВА {conf}%: ATR-фильтр снят, входим в {regime_name}"
                    + (f" | Momentum={ai.get('momentum', {}).get('score', 0):.0f}" if ai.get('momentum') else ""),
                    "INFO"
                )
            elif hard_override:
                self.log(
                    f"🔥 Hard Override AI {conf}%: входим несмотря на {regime_name}"
                    + (f", аномалия Z={ai['anomaly']['z_price']:.2f}" if anomaly else ""),
                    "INFO"
                )
            elif rsi >= Config.RSI_OVERBOUGHT and not hard_override:
                blocked = f"перекупленность RSI={rsi:.1f}"
            elif (Config.CONFLUENCE_ENABLED
                  and rsi >= Config.CONFLUENCE_RSI_MAX
                  and not hard_override and not ai_full_rights_active
                  and not mean_rev_override):
                blocked = f"Confluence: RSI={rsi:.1f}≥{Config.CONFLUENCE_RSI_MAX:.0f} (перегрет для входа)"
            elif (Config.CONFLUENCE_ENABLED
                  and vol_ratio < Config.CONFLUENCE_VOL_MIN_RATIO
                  and not hard_override and not ai_full_rights_active):
                blocked = f"Confluence: объём {vol_ratio:.2f}x < {Config.CONFLUENCE_VOL_MIN_RATIO:.1f}x MA20 (нет подтверждения)"
            elif anomaly and not hard_override:
                blocked = f"рыночная аномалия Z={ai['anomaly']['z_price']:.2f}"
            elif Config.AI_AUTONOMOUS_MODE:
                # ── BrainFusion: пропускаем подтверждение если все три источника согласны ──
                _fusion_skip = (
                    Config.FUSION_ENABLED and
                    _bf.should_skip_confirmation(conf)
                )
                # Автономный режим: A/B грейд = 1 подтверждение, C = 2
                auto_confirm = 1 if entry_quality in ("A", "B") else 2
                if _fusion_skip and not blocked:
                    self.log(
                        f"⚡ BrainFusion: пропуск ожидания — консенсус AI+TA+LLM ≥{Config.FUSION_SKIP_CONFIRM_CONF:.0f}%",
                        "INFO"
                    )
                elif self._buy_confirm_count < auto_confirm and not sm_early:
                    blocked = (
                        f"жду {auto_confirm} подтверждение(я) AI "
                        f"({self._buy_confirm_count}/{auto_confirm}) [{entry_quality}]"
                    )
            elif self._buy_confirm_count < confirm_needed and not hard_override and not sm_early:
                blocked = (
                    f"ожидаем подтверждение "
                    f"({self._buy_confirm_count}/{confirm_needed}) [грейд {entry_quality}]"
                )

        # ── Расширенное логирование ────────────────────────────────────
        sm_txt = ""
        if sm and sm.get("basis") != "idle":
            sm_txt = f" | 🐋 {sm['score']:+.2f}({sm['label']})"
        if sm_early:
            sm_txt += f" | 🟢 ранний SM"

        grade_badge = {"A": "🏆A", "B": "⭐B", "C": "🔸C"}.get(entry_quality, "?")
        mode_tag = "🤖АВТО" if Config.AI_AUTONOMOUS_MODE else "🔗БЛОК"
        self.log(
            f"📊 [{mode_tag}] RSI={rsi:.1f} | {regime_name} | "
            f"ТА={signal} | AI={ai_signal}({conf}%) | "
            f"Источник={signal_source or 'HOLD'} | "
            f"Вход {grade_badge}({entry_score}пт)"
            f"{sm_txt} | "
            f"Итог={'HOLD' if blocked else final_signal}",
            level="INFO"
        )

        # Кольцевой буфер AI-решений
        from datetime import datetime as _dtnow
        _dec_entry = {
            "t":       _dtnow.now().strftime("%H:%M:%S"),
            "signal":  signal,
            "ai_sig":  ai_signal,
            "result":  "HOLD" if blocked else final_signal,
            "conf":    conf,
            "quality": entry_quality,
            "score":   entry_score,
            "rsi":     round(rsi, 1),
            "regime":  regime_name,
            "blocked": bool(blocked),
            "source":  signal_source or "HOLD",
            "reason":  blocked or "",
        }
        self.decision_log.append(_dec_entry)
        if len(self.decision_log) > 25:
            self.decision_log.pop(0)

        # Логируем причины хорошего входа (только при BUY-сигнале)
        if final_signal == "BUY" and entry_reasons:
            self.log(
                f"  └─ Факторы входа [{entry_quality}]: " + " · ".join(entry_reasons),
                level="INFO"
            )

        if final_signal == "BUY" and blocked:
            self.log(f"⏸️ Вход отменён: {blocked}", "WARN")
        elif final_signal == "BUY" and not self.open_short_trades:
            # ── BrainFusion: скальпинг + памп-ускоритель ─────────────────
            _scalp_mode = False
            _pump_mode  = False

            _fusion_sig = _bf.get_fusion_signal() if Config.FUSION_ENABLED else None

            # mode_params: локальные переопределения для скальп/памп — НЕ мутируем Config
            _mode_params: dict = {}

            # Скальп: RANGING/SQUEEZE + ATR мал → brain_fusion вычисляет ATR-адаптивные TP/trail
            # (brain_fusion уже гарантирует что scalp_tp_pct ≥ Config.SCALP_TP_PCT)
            if _fusion_sig and _fusion_sig.is_scalp_window and Config.SCALPING_ENABLED:
                _mode_params["tp_pct"]    = _fusion_sig.scalp_tp_pct    # ATR-адаптивный, ≥ Config.SCALP_TP_PCT
                _mode_params["trail_pct"] = _fusion_sig.scalp_trail_pct  # ATR-адаптивный, ≥ Config.SCALP_TRAIL_PCT
                _scalp_mode = True
                self.log(
                    f"⚡ СКАЛЬП-РЕЖИМ [{regime_name}]: "
                    f"TP={_fusion_sig.scalp_tp_pct:.1f}% | "
                    f"Trail={_fusion_sig.scalp_trail_pct:.1f}% | "
                    f"Цель +{Config.SCALP_TARGET_NET_PCT:.1f}% нетто",
                    "INFO"
                )
                _bf.log_decision({"mode": "SCALP", "regime": regime_name,
                                  "scalp_tp": _fusion_sig.scalp_tp_pct, "conf": conf})

            # Памп-ускоритель: UPTREND/BREAKOUT/EXPLOSIVE → увеличиваем позицию
            if _fusion_sig and _fusion_sig.is_pump_window and not _scalp_mode:
                _boost = min(Config.FUSION_PUMP_BOOST_MAX, _fusion_sig.position_boost)
                if _boost > 1.05:
                    _pump_size = min(Config.FUSION_PUMP_BOOST_MAX, Config.AI_SIZE_MULT * _boost)
                    _mode_params["size_mult"] = _pump_size
                    _pump_mode = True
                    self.log(
                        f"🚀 BrainFusion ПАМП-УСКОРИТЕЛЬ: позиция ×{_boost:.2f} "
                        f"(AI_SIZE_MULT {Config.AI_SIZE_MULT:.2f} → {_pump_size:.2f})",
                        "INFO"
                    )

            # ── Smart BUY: ждём откат или покупаем сразу ──────────────────
            # Грейд A при высокой уверенности — покупаем сразу (не ждём откат)
            is_elite_instant = (entry_quality == "A" and conf >= Config.SMART_BUY_SKIP_CONF - 10)
            # В скальп-режиме не ждём откат — рынок боковик, откат может не прийти
            use_smart = (
                Config.SMART_BUY_ENABLED
                and not is_elite_instant
                and not _scalp_mode              # скальп: входим немедленно
                and conf < Config.SMART_BUY_SKIP_CONF
                and not self._pending_buy
            )
            if use_smart:
                # Smart BUY: ждём откат — реальный вход будет в следующем тике.
                # mode_params сохраняем в pending_buy чтобы применить при фактическом входе.
                target = self.exchange._round(price * (1 - pullback_pct / 100))
                self._pending_buy = {
                    "target":        target,
                    "signal_price":  price,
                    "ai":            ai,
                    "analysis":      result,
                    "ticks_left":    Config.SMART_BUY_MAX_WAIT_TICKS,
                    "entry_quality": entry_quality,
                    "pullback_pct":  pullback_pct,
                    "mode_params":   _mode_params,   # скальп/памп параметры для будущего входа
                }
                # Персистируем в DB + JSON — переживёт перезапуск и отказ БД
                try:
                    from settings_store import update_section
                    import json as _json
                    _pb_save = {k: v for k, v in self._pending_buy.items() if k not in ("ai", "analysis")}
                    update_section("trader_state", {"pending_buy": _json.dumps(_pb_save)})
                except Exception:
                    pass
                self.log(
                    f"🎯 Smart BUY [{entry_quality}-грейд]: ждём откат до ${target:.8f} "
                    f"(сейчас ${price:.8f}, -{pullback_pct:.1f}%, "
                    f"макс {Config.SMART_BUY_MAX_WAIT_TICKS} тика) | AI {conf}%",
                    "INFO"
                )
            else:
                # AI очень уверен / скальп / A-грейд — берём сразу
                if _scalp_mode:
                    pass  # уже залогировано выше
                elif is_elite_instant:
                    self.log(
                        f"🚀 ЭЛИТНЫЙ ВХОД [{entry_quality}]: AI {conf}% + {entry_score} факторов → покупаем немедленно",
                        "INFO"
                    )
                elif conf >= Config.SMART_BUY_SKIP_CONF:
                    self.log(f"⚡ Smart BUY пропущен: AI {conf}% ≥ {Config.SMART_BUY_SKIP_CONF}% — покупаем сразу", "INFO")
                opened = self._open_trade("buy", price, result, ai, mode_params=_mode_params)
                # Сигнал пользователям шлём ТОЛЬКО если реальный ордер исполнился
                if opened:
                    self._buy_confirm_count = 0
                    # Флаг скальпа фиксируем только при реально открытой сделке
                    self._last_entry_was_scalp = _scalp_mode
                    self._emit_signal("BUY", price, ai)
        elif final_signal == "SELL":
            if self.open_trades:
                # Сначала закрываем существующие лонги
                closed = self._close_all_trades(price, result)
                if closed:
                    self._emit_signal("SELL", price, ai)
                    self._sell_confirm_count = 0
            elif (Config.SHORT_TRADING_ENABLED
                  and not self.open_short_trades
                  and conf >= Config.SHORT_MIN_AI_CONF):
                # Нет открытых позиций + AI уверен в падении → открываем шорт
                self._sell_confirm_count += 1
                short_confirm = 1 if entry_quality in ("A", "B") else 2
                if self._sell_confirm_count >= short_confirm:
                    self.log(
                        f"📉 AI ШОРТ сигнал: уверенность {conf}% | {regime_name} | "
                        f"RSI={rsi:.1f} | грейд={entry_quality} — открываем шорт",
                        "INFO"
                    )
                    self._open_short_trade(price, result, ai)
                    self._sell_confirm_count = 0
                else:
                    self.log(
                        f"📉 Шорт: ждём подтверждение ({self._sell_confirm_count}/{short_confirm})",
                        "INFO"
                    )
            else:
                self._sell_confirm_count = 0

        # ── Аналитический буфер (AI-режим) ────────────────────────────
        try:
            self._push_tick_analytics()
        except Exception:
            pass

    def _emit_signal(self, signal, price, ai=None):
        """Шлёт сигнал BUY/SELL всем подписчикам (UserTradingManager и т.п.).
        Вызывать ТОЛЬКО после подтверждённого реального исполнения сделки —
        иначе виртуальное состояние юзеров рассинхронизируется с реальными активами."""
        for cb in self.signal_callbacks:
            try:   cb(signal, price, ai)
            except Exception as e: self.log(f"Signal cb ошибка: {e}", "WARN")

    def _relevant_open(self):
        return [t for t in self.open_trades
                if t.get("symbol", Config.SYMBOL) == Config.SYMBOL]

    # ──────────────────────────────────────────
    # Торговые операции
    # ──────────────────────────────────────────
    def _adaptive_trail_pct(self, base_pct):
        """Адаптивная ШИРИНА трейлинга по силе тренда + Momentum + Breakout.

        В сильном восходящем тренде трейлинг расширяется → стоп подтягивается
        медленнее → прибыль успевает разрастись (ловим большие движения вроде
        недельного +343%). В боковике/слабости трейлинг сужается → быстрее
        фиксируем прибыль. ВАЖНО: нижний пол прибыли (floor_price = +N% нетто)
        не меняется — продажа в минус по-прежнему невозможна.

        Momentum + Breakout расширяют трейлинг ещё дальше — при GRINCH-пампе
        стоп отходит дальше от цены, чтобы не выбило раньше времени.
        """
        regime   = (self.last_ai or {}).get("regime") or {}
        momentum = (self.last_ai or {}).get("momentum") or {}
        breakout = (self.last_ai or {}).get("breakout") or {}
        name = regime.get("name", "")
        try:
            adx = float(regime.get("adx", 20) or 20)
        except (TypeError, ValueError):
            adx = 20.0

        # ── Базовый множитель по режиму ──────────────────────────────────
        if name == "UPTREND" and adx >= Config.TRAIL_TREND_ADX:
            mult = Config.TRAIL_TREND_WIDEN
        elif name in ("UPTREND", "SQUEEZE", "VOLATILE"):
            mult = (1.0 + Config.TRAIL_TREND_WIDEN) / 2.0
        elif name in ("RANGING", "TRANSITION", "DOWNTREND"):
            mult = Config.TRAIL_CHOP_TIGHTEN
        else:
            mult = 1.0

        # ── Momentum буст трейлинга (SURGE/EXPLOSIVE → дать памп пробежать) ──
        mom_sig = (momentum.get("signal") or "CALM").upper()
        if mom_sig == "EXPLOSIVE":
            mult *= 1.5   # при взрывном импульсе стоп сильно шире
        elif mom_sig == "SURGE":
            mult *= 1.25  # при разгоне — умеренно шире

        # ── Breakout буст трейлинга (BREAKOUT/RUNAWAY → ещё дальше) ─────
        bo_sig = (breakout.get("signal") or "FLAT").upper()
        if bo_sig == "RUNAWAY":
            mult *= 1.4
        elif bo_sig == "BREAKOUT":
            mult *= 1.2

        return base_pct * mult

    def _targets(self, price, ai, stake_ton=None, tp_override=None, is_scalp=False):
        """Рассчитывает SL/TP%.
        tp_override — если передан, используется вместо Config.TAKE_PROFIT_PCT
        (позволяет скальп-режиму задать меньший TP без мутации Config).
        is_scalp    — True в скальп-режиме: пол TP считается от SCALP_TARGET_NET_PCT,
                      а не от TARGET_NET_PCT, иначе скальп-выход всегда блокируется.
        """
        base_tp = tp_override if tp_override is not None else Config.TAKE_PROFIT_PCT
        atr_pct = (ai.get("regime", {}) or {}).get("atr_pct", 0) / 100.0 if ai else 0.0
        if Config.USE_DYNAMIC_TARGETS and atr_pct > 0:
            sl_pct = max(atr_pct * Config.ATR_SL_MULT * 100, Config.STOP_LOSS_PCT)
            tp_pct = max(atr_pct * Config.ATR_TP_MULT * 100, base_tp)
        else:
            sl_pct, tp_pct = Config.STOP_LOSS_PCT, base_tp

        # Жёсткий минимум TP учитывает и DEX-комиссию, и газ обоих свопов.
        # В скальп-режиме минимум считается от SCALP_TARGET_NET_PCT (3%),
        # а не от глобального TARGET_NET_PCT (13%), чтобы не блокировать быстрые выходы.
        if is_scalp:
            fee = Config.FEE_PCT / 100.0
            scalp_gross_floor = (Config.SCALP_TARGET_NET_PCT + Config.FEE_PCT * 2) / max((1 - fee) ** 2, 0.9801)
            min_gross_tp = max(scalp_gross_floor, Config.SCALP_TP_PCT)
        else:
            min_gross_tp = Config.required_gross_pct_with_gas(stake_ton)
        tp_pct = max(tp_pct, min_gross_tp)
        return sl_pct, tp_pct

    def _open_trade(self, side, price, analysis, ai=None, mode_params=None):
        if side == "buy" and liquidity_guard.is_buy_paused():
            status = liquidity_guard.get_status()
            self.log(
                f"⛔ BUY заблокирован LiquidityGuard: {status.get('pause_reason', 'низкая ликвидность')}",
                "WARN"
            )
            return False

        ai_conf = ai.get("confidence", 0) if ai else 0

        # ── Kelly-adjusted position sizing ────────────────────────────────
        # Base: пропорционально уверенности AI (50%→0.5× .. 90%→1.0×)
        conf_factor = 0.5 + min(max((ai_conf - 50) / 50.0, 0.0), 1.0) * 0.5
        # Kelly fraction: если накоплено ≥5 сделок, используем Kelly для масштаба
        kelly = (ai or {}).get("kelly", {})
        kelly_frac = kelly.get("fraction", 0.5)
        kelly_wr   = kelly.get("win_rate", 50.0)
        kelly_trades = kelly.get("trades", 0)
        if kelly_trades >= 5 and kelly_wr >= 50:
            # Хорошая статистика → Kelly увеличивает ставку (до 2×)
            kelly_mult = min(kelly_frac, 2.0)
        elif kelly_trades >= 5 and kelly_wr < 45:
            # Плохая статистика → уменьшаем ставку
            kelly_mult = max(kelly_frac, 0.3)
        else:
            kelly_mult = 1.0   # мало данных → нейтральный множитель

        # ── POWER SIZING: Breakout × Momentum масштабирование ────────────
        # Когда Breakout-детектор + Momentum одновременно сильные →
        # Kelly multiplier масштабируется до 2×, чтобы поймать GRINCH-памп
        breakout    = (ai or {}).get("breakout", {})
        momentum    = (ai or {}).get("momentum", {})
        bo_mult     = float(breakout.get("kelly_mult", 1.0))
        mom_sig     = (momentum.get("signal") or "CALM").upper()
        mom_mult_map = {"EXPLOSIVE": 1.6, "SURGE": 1.3, "BUILDING": 1.1, "CALM": 1.0}
        mom_mult    = mom_mult_map.get(mom_sig, 1.0)
        # Комбинированный power_mult: среднее (не произведение — чтобы не разогнать ×4)
        power_mult  = min(2.0, (bo_mult + mom_mult) / 2.0)

        bo_sig = (breakout.get("signal") or "FLAT").upper()
        if bo_sig in ("BREAKOUT", "RUNAWAY") or mom_sig == "EXPLOSIVE":
            self.log(
                f"⚡ POWER ENTRY: Breakout={bo_sig}(×{bo_mult:.1f}) "
                f"Momentum={mom_sig}(×{mom_mult:.1f}) "
                f"→ Kelly×{power_mult:.2f}",
                "INFO"
            )

        # mode_params: локальные переопределения для скальп/памп-режима (не мутируем Config)
        _mp = mode_params or {}
        _mp_size_mult = _mp.get("size_mult")   # None → берём из Config
        ai_size_mult = max(0.3, min(2.0, _mp_size_mult if _mp_size_mult is not None else Config.AI_SIZE_MULT))
        stake = Config.TRADE_AMOUNT * conf_factor * kelly_mult * power_mult * ai_size_mult

        # ── Резерв на комиссию + опрос баланса перед сделкой ─────────────
        # ВСЕГДА оставляем GAS_RESERVE_TON на газ будущей продажи GRINCH→TON.
        # Покупка не тратит резерв: при нехватке урезаем ставку, а если денег
        # нет даже на резерв + газ покупки — сделку отменяем (fail-closed).
        ton_stake = None
        if self.exchange.mode == "dedust" and side == "buy":
            bal     = self.exchange.get_balance() or {}
            ton_bal = bal.get("TON", 0) or 0
            buy_gas = 0.30                      # газ BUY-свопа (0.3 TON attach, подтверждено on-chain)
            reserve = Config.GAS_RESERVE_TON    # неприкосновенный резерв на комиссию продажи
            spendable = ton_bal - buy_gas - reserve
            if bal.get("error") or spendable < Config.MIN_STAKE_TON:
                why = bal.get("error") or (
                    f"на кошельке {ton_bal:.3f} TON: после газа {buy_gas} + резерва "
                    f"{reserve} TON остаётся {spendable:.3f} < мин. ставки "
                    f"{Config.MIN_STAKE_TON} TON"
                )
                self.log(f"⛔ Недостаточно средств для BUY: {why}. Сделка отменена.", "WARN")
                return False
            if stake > spendable:
                self.log(
                    f"✂️ Ставка урезана {stake:.3f} → {spendable:.3f} TON, "
                    f"чтобы всегда осталось на комиссию (резерв {reserve} TON)", "INFO"
                )
                stake = spendable
            ton_stake = stake
            amount = stake / price
        else:
            amount = stake / price

        # ── Детальный расчёт комиссий и цели (до исполнения) ────────────
        if side == "buy" and ton_stake:
            _fee_pct   = Config.FEE_PCT
            _buy_gas   = Config.BUY_GAS_TON
            _sell_gas  = Config.SELL_GAS_TON
            _min_gross = Config.required_gross_pct_with_gas(ton_stake)
            _target_net = Config.TARGET_NET_PCT
            _real_stake = ton_stake
            _total_cost = _real_stake + _buy_gas
            self.log(
                f"💰 Расчёт комиссий и цели:\n"
                f"   Ставка:       {_real_stake:.3f} TON\n"
                f"   Газ покупки:  {_buy_gas:.3f} TON  (→ пул, частично вернётся)\n"
                f"   Газ продажи:  {_sell_gas:.3f} TON  (→ фиксируем заранее)\n"
                f"   Комиссия DEX: {_fee_pct}% вход + {_fee_pct}% выход = {_fee_pct*2:.1f}% от суммы\n"
                f"   ИТОГО затрат: ~{_total_cost:.3f} TON\n"
                f"   Нужно вырасти как минимум: +{_min_gross:.2f}% (gross) для +{_target_net:.0f}% нетто",
                level="INFO"
            )

        order = self.exchange.place_order(side, amount, ton_stake=ton_stake)
        if not order or order.get("error"):
            err = (order or {}).get("error", "нет ответа") if order else "нет ответа"
            self.log(f"⚠️ BUY ордер не исполнен — {err}", "WARN")
            return False

        # В DeDust-режиме используем реальное кол-во GRINCH из подтверждённого свопа,
        # а не расчётное (stake_ton / usd_price), которое даёт неверные единицы измерения.
        if self.exchange.mode == "dedust":
            actual_grinch = (order.get("info") or {}).get("grinch_received", 0)
            if actual_grinch and actual_grinch > 0:
                self.log(
                    f"✅ Реальный GRINCH получен: {actual_grinch:.4f} "
                    f"(расч. по USD-цене было бы {stake / price:.4f})", "INFO"
                )
                amount = actual_grinch

        # Передаём stake в _targets: TP учтёт и DEX-комиссию, и газ обоих свопов
        # tp_override из mode_params позволяет скальп-режиму задать свой TP без мутации Config
        # is_scalp=True: пол TP от SCALP_TARGET_NET_PCT, а не от TARGET_NET_PCT
        _tp_override = _mp.get("tp_pct")   # None → Config.TAKE_PROFIT_PCT (через _targets)
        _is_scalp    = _tp_override is not None   # скальп всегда передаёт tp_override
        sl_pct, tp_pct = self._targets(price, ai, stake_ton=stake, tp_override=_tp_override, is_scalp=_is_scalp)
        sl = 0.0 if Config.ONLY_PROFIT_EXIT else self.exchange._round(price * (1 - sl_pct / 100))
        tp = self.exchange._round(price * (1 + tp_pct / 100))

        # Константы для карточки «ожидают продажи» — рассчитываем один раз при открытии
        fee       = Config.FEE_PCT / 100.0
        sell_gas  = Config.SELL_GAS_TON
        buy_gas   = Config.BUY_GAS_TON
        total_cost = stake + buy_gas
        # be_ton: цена GRINCH в TON при которой net = 0
        # amount * be_ton * (1 - fee) - sell_gas = total_cost
        be_ton    = (total_cost + sell_gas) / (amount * (1 - fee)) if amount > 0 else 0
        entry_ton = stake / amount if amount > 0 else 0
        be_usd    = round(price * be_ton / entry_ton, 8) if (entry_ton > 0 and price > 0) else 0
        min_gross = Config.required_gross_pct_with_gas(stake if stake > 0 else None)
        # Рыночный контекст при входе для AI-аналитики
        ai_snap_entry = self.last_ai or {}
        regime_entry  = ai_snap_entry.get("regime") or {}
        sm_entry = {}
        try:
            from wallet_tracker import wallet_tracker as _wt
            sm_entry = _wt.get_signal()
        except Exception:
            pass
        def _sf(v, d=0.0):
            try: return float(v) if v is not None else d
            except Exception: return d
        try:
            from price_feed import price_feed as _pf
            _grinch_ton_entry = _pf.get_grinch_ton_price() or 0.0
        except Exception:
            _grinch_ton_entry = 0.0
        trade = {
            "id":              order["id"],
            "symbol":          Config.SYMBOL,
            "side":            side,
            "entry_price":     price,
            "entry_price_ton": _grinch_ton_entry,
            "amount":          round(amount, 6),
            "stake_ton":       round(stake, 4),
            "stop_loss":       sl,
            "take_profit":     tp,
            "trail_pct":       _mp.get("trail_pct", Config.TRAILING_STOP_PCT),  # скальп задаёт свой трейл
            "high_water":      price,
            "opened_at":       datetime.utcnow().isoformat(),
            "pnl":             0.0,
            "status":          "open",
            "ai_confidence":   float(ai_conf),
            # Постоянные расчётные поля карточки (не меняются после открытия)
            "breakeven_price": be_usd,
            "min_gross_pct":   round(min_gross, 1),
            # Рыночный контекст при входе (явные Python-типы, не numpy!)
            "entry_regime":     str(regime_entry.get("name") or ""),
            "entry_rsi":        _sf(ai_snap_entry.get("rsi")),
            "entry_atr_pct":    _sf(regime_entry.get("atr_pct") or regime_entry.get("atr")),
            "entry_anomaly":    bool((ai_snap_entry.get("anomaly") or {}).get("detected", False)),
            "entry_sm_score":   _sf(sm_entry.get("score")),
            "entry_sm_label":   str(sm_entry.get("label") or ""),
            "entry_sm_buys_1h": int(sm_entry.get("buys_1h") or 0),
            "entry_sm_sells_1h":int(sm_entry.get("sells_1h") or 0),
            # Breakout + Momentum при входе
            "entry_bo_signal":  str((ai_snap_entry.get("breakout") or {}).get("signal") or "FLAT"),
            "entry_bo_score":   _sf((ai_snap_entry.get("breakout") or {}).get("score")),
            "entry_mom_signal": str((ai_snap_entry.get("momentum") or {}).get("signal") or "CALM"),
            "entry_mom_score":  _sf((ai_snap_entry.get("momentum") or {}).get("score")),
        }
        self.open_trades.append(trade)
        self.trades.append(dict(trade))
        # total_trades теперь считается только в момент закрытия (там же,
        # где вызывается record_trade) — единая точка учёта, чтобы счётчик
        # никогда не расходился с журналом сделок bot_trades.
        self.exp.data["stats"] = dict(self.stats)
        # Если уже есть другие LONG-позиции — объединяем всё в одну
        self._merge_long_trades()
        # АВТО-СОХРАНЕНИЕ: цена покупки + цель продажи на диск, чтобы после
        # перезапуска бот знал почём купил и не продал дешевле.
        try:
            self.exp.save_open_trades(self._combined_open_trades())
        except Exception as e:  # noqa: BLE001
            self.log(f"Сохранение позиции: {e}", "WARN")
        self.log(
            f"🟢 BUY @ {price} | {stake:.3f} TON | SL={sl}(-{sl_pct:.1f}%) | "
            f"TP={tp}(+{tp_pct:.1f}%) | AI={ai_conf}%", "BUY"
        )
        # ── Аналитический буфер: событие открытия позиции ─────────────
        try:
            from analytics_buffer import analytics_buffer as _ab
            _ab.push_trade("OPEN", {
                "price":    price,
                "stake_ton": stake,
                "regime":   str(regime_entry.get("name") or "?"),
                "ai_conf":  float(ai_conf),
            })
        except Exception:
            pass
        return True

    def _close_all_trades(self, price, analysis):
        relevant_before = self._relevant_open()
        for trade in list(relevant_before):
            if Config.ONLY_PROFIT_EXIT:
                entry      = trade["entry_price"]
                stake_ton  = trade.get("stake_ton") or None
                pnl_pct    = (price - entry) / entry * 100 if entry else 0.0
                # Порог включает газ обоих свопов для данной конкретной ставки
                net_floor_pct = Config.required_gross_pct_with_gas(stake_ton)
                if pnl_pct < net_floor_pct:
                    self.log(
                        f"⏸️ SELL-сигнал отклонён: прибыль {pnl_pct:+.1f}% < "
                        f"мин. +{net_floor_pct:.1f}% (режим «только в плюс», газ учтён). Держим.",
                        "INFO"
                    )
                    continue
            self._close_trade(trade, price, "signal")
        # Сигнал SELL юзерам безопасен ТОЛЬКО когда были позиции и ВСЕ они
        # реально закрылись. При частичном закрытии (одна продажа прошла, другая
        # нет) grinch_held обнулять нельзя — часть реального GRINCH ещё не продана.
        return bool(relevant_before) and not self._relevant_open()

    def _check_short_positions(self, price):
        """Управляет шорт-позициями: фиксируем прибыль когда цена упала достаточно.
        ONLY_PROFIT_EXIT: закрываем шорт ТОЛЬКО когда цена упала ≥ required_drop_pct.
        Трейлинг: когда цена отскакивает вверх от минимума на SHORT_TRAIL_PCT — фиксируем.
        """
        closed_any = False
        for trade in list(self.open_short_trades):
            entry_usd  = trade["entry_price"]
            low_water  = trade.get("low_water", entry_usd)
            grinch_val = trade.get("grinch_value_ton")
            required_drop = Config.required_drop_pct_for_short(grinch_val)

            # Обновляем низшую точку
            if price < low_water:
                trade["low_water"] = price
                low_water = price

            drop_pct = (entry_usd - price) / entry_usd * 100  # >0 = цена упала (нам выгодно)
            in_profit = drop_pct >= required_drop             # покрыли комиссии + цель

            if not in_profit:
                continue  # ONLY_PROFIT_EXIT: ждём пока не в прибыли

            # В прибыльной зоне — применяем трейлинг
            trail_pct   = Config.SHORT_TRAIL_PCT
            trail_price = low_water * (1 + trail_pct / 100)   # если цена выросла от дна

            big_tp = drop_pct >= required_drop * 2.0  # упало в 2× больше нужного → берём сразу

            if big_tp:
                self.log(
                    f"🎯 Шорт TP: цена упала -{drop_pct:.1f}% (нужно -{required_drop:.1f}%) → "
                    f"фиксируем прибыль немедленно", "INFO"
                )
                self._close_short_trade(trade, price, "take_profit")
                closed_any = True
            elif price >= trail_price:
                self.log(
                    f"📈 Шорт трейлинг: цена +{trail_pct:.1f}% от дна ${low_water:.8f} → "
                    f"фиксируем (drop={drop_pct:.1f}% ≥ нужно {required_drop:.1f}%)", "INFO"
                )
                self._close_short_trade(trade, price, "trailing")
                closed_any = True

        return closed_any

    def _open_short_trade(self, price, analysis, ai):
        """Открывает шорт-позицию: продаёт GRINCH→TON сейчас, откупит дешевле.
        Прибыль = получаем обратно больше GRINCH чем продали (≥+20% нетто).
        """
        if self.exchange.mode != "dedust":
            self.log("⚠️ Шорт доступен только в DeDust-режиме", "WARN")
            return False

        ai_conf = ai.get("confidence", 0) if ai else 0

        # Получаем баланс GRINCH
        bal        = self.exchange.get_balance() or {}
        grinch_bal = bal.get("GRINCH", 0) or 0
        grinch_reserve = Config.GRINCH_RESERVE

        # Текущий курс GRINCH в TON
        from price_feed import price_feed
        grinch_ton = price_feed.get_grinch_ton_price()
        if not grinch_ton or grinch_ton <= 0:
            self.log("⚠️ Шорт: не удалось получить курс GRINCH/TON — пропускаем", "WARN")
            return False

        # Количество GRINCH для шорта (эквивалент TRADE_AMOUNT TON × коэф. уверенности)
        conf_factor   = 0.5 + min(max((ai_conf - 50) / 50.0, 0.0), 1.0) * 0.5
        target_ton    = Config.TRADE_AMOUNT * conf_factor
        target_grinch = target_ton / grinch_ton
        available     = max(0.0, grinch_bal - grinch_reserve)

        if available < target_grinch:
            target_grinch = available  # урезаем до доступного

        # Минимальная осмысленная сумма
        min_grinch = Config.MIN_STAKE_TON / grinch_ton
        if target_grinch < min_grinch:
            self.log(
                f"⛔ Шорт отменён: доступно {available:.0f} GRINCH < "
                f"мин. {min_grinch:.0f} (≈{Config.MIN_STAKE_TON} TON). "
                f"Резерв: {grinch_reserve:.0f}, баланс: {grinch_bal:.0f}", "WARN"
            )
            return False

        grinch_value_ton = target_grinch * grinch_ton  # эквивалент в TON для расчёта комиссий
        required_drop    = Config.required_drop_pct_for_short(grinch_value_ton)

        # Детальный лог комиссий перед сделкой
        self.log(
            f"💰 Шорт — расчёт комиссий:\n"
            f"   Продаём:      {target_grinch:.2f} GRINCH (≈{grinch_value_ton:.3f} TON)\n"
            f"   Газ продажи:  {Config.SELL_GAS_TON:.3f} TON\n"
            f"   Газ откупки:  {Config.BUY_GAS_TON:.3f} TON\n"
            f"   Комиссия DEX: {Config.FEE_PCT}% + {Config.FEE_PCT}% = {Config.FEE_PCT*2:.1f}%\n"
            f"   Нужно упасть: ≥{required_drop:.2f}% для +{Config.TARGET_NET_PCT:.0f}% нетто",
            "INFO"
        )

        # AMM preflight для шорта: блокируем если пул вернёт меньше ожидаемого
        # (grinch_value_ton * (1 - SLIPPAGE)) — защита от чрезмерного проскальзывания.
        # min_net_ton — нетто после газа продажи; CPMM в dedust_client учитывает price impact.
        _short_min_net = max(0.0, grinch_value_ton * (1 - Config.SLIPPAGE_PCT / 100) - Config.SELL_GAS_TON)
        self.log(
            f"🔍 Шорт AMM preflight: продаём {target_grinch:.2f} GRINCH "
            f"(≈{grinch_value_ton:.3f} TON), нетто ≥ {_short_min_net:.3f} TON",
            "INFO"
        )

        # Исполняем продажу GRINCH → TON
        order = self.exchange.place_order("sell", target_grinch, min_net_ton=_short_min_net)
        if not order or order.get("error"):
            err = (order or {}).get("error", "нет ответа")
            if (order or {}).get("amm_blocked"):
                self.log(
                    f"🛡️ Шорт заблокирован AMM preflight: {err} "
                    f"(пул вернёт меньше {_short_min_net:.3f} TON нетто)", "WARN"
                )
            else:
                self.log(f"⚠️ Шорт: продажа GRINCH не исполнена — {err}", "WARN")
            return False

        # Реально полученный TON из ордера (если DEX вернул)
        info       = order.get("info") or {}
        ton_recv   = info.get("ton_received") or (grinch_value_ton * (1 - Config.FEE_PCT/100) - Config.SELL_GAS_TON)
        tp_price   = self.exchange._round(price * (1 - required_drop / 100))

        trade = {
            "id":               order["id"],
            "trade_type":       "short",
            "entry_price":      price,
            "entry_price_ton":  grinch_ton,
            "amount":           round(target_grinch, 6),       # GRINCH продано
            "grinch_value_ton": round(grinch_value_ton, 4),   # эквивалент в TON
            "ton_received":     round(ton_recv, 6),            # TON получено от продажи
            "take_profit":      tp_price,
            "low_water":        price,
            "required_drop_pct":round(required_drop, 2),
            "opened_at":        datetime.utcnow().isoformat(),
            "status":           "short_open",
            "ai_confidence":    ai_conf,
            "entry_regime":     (ai.get("regime") or {}).get("name") if ai else None,
        }
        self.open_short_trades.append(trade)
        # total_trades теперь считается только в момент закрытия (см. _close_short_trade),
        # единая точка учёта, чтобы счётчик не расходился с журналом bot_trades.
        self.exp.data["stats"] = dict(self.stats)
        try:
            self.exp.save_open_trades(self._combined_open_trades())
        except Exception as e:  # noqa: BLE001
            self.log(f"Сохранение SHORT-позиции: {e}", "WARN")

        self.log(
            f"📉 SHORT открыт: продали {target_grinch:.2f} GRINCH @ ${price:.8f} "
            f"| TON получено: ~{ton_recv:.3f} | TP @ ${tp_price:.8f} (-{required_drop:.1f}%) "
            f"| AI={ai_conf}%",
            "SELL"
        )
        return True

    def _close_short_trade(self, trade, price, reason):
        """Закрывает шорт: откупает GRINCH обратно за накопленный TON.
        Прибыль = grinch_received > grinch_sold → конвертируем в TON для статистики.
        """
        trade_id = trade.get("id")
        # Защита от двойного закрытия
        if trade_id not in {t.get("id") for t in self.open_short_trades}:
            return False

        ton_to_spend   = trade.get("ton_received", 0)
        grinch_sold    = trade.get("amount", 0)
        grinch_val_ton = trade.get("grinch_value_ton", 0)
        entry_price    = trade.get("entry_price", price)

        if self.exchange.mode == "dedust" and ton_to_spend > 0:
            # Необходимо оставить газ на покупку
            buy_gas    = Config.BUY_GAS_TON
            spend_net  = ton_to_spend
            est_grinch = spend_net / price  # примерное количество для place_order
            self.log(
                f"💸 Шорт откупка: тратим {ton_to_spend:.4f} TON → покупаем GRINCH @ ${price:.8f}",
                "INFO"
            )
            order = self.exchange.place_order("buy", est_grinch, ton_stake=ton_to_spend)
            if not order or order.get("error"):
                err = (order or {}).get("error", "нет ответа")
                self.log(f"⚠️ Шорт откупка не исполнена: {err} — позиция остаётся", "WARN")
                return False

            info           = order.get("info") or {}
            grinch_received = info.get("grinch_received") or (ton_to_spend * (1 - Config.FEE_PCT/100) / price)
        else:
            # Demo/fallback: расчётное значение
            fee            = Config.FEE_PCT / 100.0
            grinch_received = ton_to_spend * (1 - fee) / price

        grinch_received = float(grinch_received or 0)
        profit_grinch   = grinch_received - grinch_sold

        # Конвертируем прибыль в TON для статистики
        from price_feed import price_feed
        g_ton = price_feed.get_grinch_ton_price() or trade.get("entry_price_ton", 0.0001)
        pnl_ton = round(profit_grinch * g_ton, 6)
        drop_pct = (entry_price - price) / entry_price * 100 if entry_price else 0

        trade["exit_price"]      = price
        trade["closed_at"]       = datetime.utcnow().isoformat()
        trade["close_reason"]    = reason
        trade["status"]          = "short_closed"
        trade["grinch_received"] = round(grinch_received, 6)
        trade["profit_grinch"]   = round(profit_grinch, 6)
        trade["pnl"]             = pnl_ton
        trade["drop_pct"]        = round(drop_pct, 2)
        trade["pnl_pct"]         = round(profit_grinch / grinch_sold * 100, 2) if grinch_sold else 0

        # Лок: self.stats мутируется из нескольких потоков (тик трейдера,
        # ликвидатор, ручное закрытие) — без него возможна гонка при
        # одновременном закрытии позиций (потерянный инкремент total_trades).
        with self._close_lock:
            self.open_short_trades = [t for t in self.open_short_trades if t["id"] != trade_id]
            self.stats["total_pnl"] = round(self.stats["total_pnl"] + pnl_ton, 6)
            # Единая точка учёта: total_trades считается только здесь, в момент
            # закрытия — синхронно с вызовом record_trade() ниже.
            self.stats["total_trades"] = self.stats.get("total_trades", 0) + 1
            if pnl_ton > 0:
                self.stats["winning_trades"] += 1
        try:
            self.exp.save_open_trades(self._combined_open_trades())
            self.exp.record_trade(dict(trade), self.stats, self.ai)
        except Exception as e:  # noqa: BLE001
            self.log(f"Сохранение закрытия SHORT: {e}", "WARN")

        emoji = "🟩" if pnl_ton >= 0 else "🟥"
        self.log(
            f"{emoji} Шорт закрыт @ ${price:.8f} | GRINCH: продали {grinch_sold:.2f} → "
            f"откупили {grinch_received:.2f} | Профит: +{profit_grinch:.2f} GRINCH "
            f"(≈{pnl_ton:+.4f} TON) | Падение: -{drop_pct:.1f}% | {reason}",
            "SELL" if pnl_ton >= 0 else "ERROR"
        )
        # AI feedback
        try:
            outcome  = "win" if pnl_ton > 0 else "loss"
            ai_snap  = self.last_ai or {}
            reg_name = (ai_snap.get("regime") or {}).get("name", "UNKNOWN")
            ai_conf  = float(ai_snap.get("confidence", 0) or 0)
            self.ai.feedback(outcome=outcome, pnl=float(pnl_ton),
                             regime=reg_name, conf=ai_conf)
        except Exception as e:
            self.log(f"⚠️ AI feedback (шорт-закрытие): {e}", "WARN")
        return True

    def _check_stop_loss_take_profit(self, price):
        # Сначала проверяем шорт-позиции
        self._check_short_positions(price)

        had_relevant = bool(self._relevant_open())
        closed_any   = False
        for trade in list(self.open_trades):
            if trade.get("symbol", Config.SYMBOL) != Config.SYMBOL:
                continue

            entry      = trade["entry_price"]
            profit_pct = (price - entry) / entry * 100

            # Минимальный нетто-пол прибыли (в gross %): учитывает DEX-комиссию
            # И газ обоих свопов для данной ставки. Для мелких сделок порог выше.
            stake_ton     = trade.get("stake_ton") or None
            net_floor_pct = Config.required_gross_pct_with_gas(stake_ton)
            floor_price   = self.exchange._round(entry * (1 + net_floor_pct / 100))

            if Config.ONLY_PROFIT_EXIT:
                # ── Режим «только в плюс, минимум N% нетто» ──────────────────
                # «Взведённый» стоп = трейлинг уже активирован (стоп ≥ floor_price).
                # До взведения стоп = 0 и вниз не срабатывает (держим позицию,
                # никакого стоп-лосса в убыток не существует).
                armed = trade["stop_loss"] > 0

                # Прибыль достигла пола → взводим/подтягиваем трейлинг. Стоп
                # НИКОГДА не опускается ниже floor_price (гарантия +N% нетто).
                if profit_pct >= net_floor_pct:
                    if price > trade.get("high_water", entry):
                        trade["high_water"] = price
                    high_water = trade.get("high_water", entry)

                    # ── Smart TP: ИИ решает держать или фиксировать ────────────
                    # Если ИИ уверен в продолжении роста (≥ порога) и сигнал BUY —
                    # используем тугой трейлинг (1.5%) чтобы дать цене расти дальше.
                    # Как только уверенность падает — переключаемся на обычный трейл.
                    ai_conf   = (self.last_ai or {}).get("confidence", 0)
                    ai_action = (self.last_ai or {}).get("action", "")
                    smart_active = (
                        Config.SMART_TP_ENABLED
                        and ai_conf >= Config.SMART_TP_MIN_CONF
                        and ai_action == "BUY"
                    )
                    if smart_active:
                        trail_pct = Config.SMART_TP_TIGHT_TRAIL_PCT
                        if not trade.get("smart_tp_active"):
                            trade["smart_tp_active"] = True
                            self.log(
                                f"🧠 Smart TP активен: AI {ai_conf:.0f}% BUY — "
                                f"держим позицию, трейл {trail_pct}% (ищем больше прибыли)",
                                "INFO"
                            )
                    else:
                        trail_pct = self._adaptive_trail_pct(Config.TRAIL_STAGE4_PCT)
                        if trade.get("smart_tp_active"):
                            trade["smart_tp_active"] = False
                            self.log(
                                f"🧠 Smart TP выключен: AI ослаб до {ai_conf:.0f}% / {ai_action} — "
                                f"обычный трейл {trail_pct:.1f}%, фиксируем прибыль",
                                "INFO"
                            )

                    new_sl = self.exchange._round(high_water * (1 - trail_pct / 100))
                    new_sl = max(new_sl, floor_price)    # пол ≥ +N% нетто

                    if new_sl > trade["stop_loss"]:
                        old_sl = trade["stop_loss"]
                        trade["stop_loss"] = new_sl
                        regime_name = ((self.last_ai or {}).get("regime") or {}).get("name", "")
                        smart_label = " [🧠 Smart TP]" if smart_active else ""
                        self.log(
                            f"🔼 Стоп: {old_sl} → {new_sl} | прибыль {profit_pct:+.1f}% | "
                            f"трейл {trail_pct:.1f}% [{regime_name}]{smart_label} "
                            f"(пол +{net_floor_pct:.0f}% нетто)",
                            "INFO"
                        )
                    armed = True

                # Если стоп взведён — проверяем выход КАЖДЫЙ тик (даже если цена
                # уже просела ниже пола): иначе зафиксированную прибыль можно
                # «забыть» снять. Стоп всегда ≥ floor_price → выход в плюс.
                if armed and price <= trade["stop_loss"]:
                    if self._close_trade(trade, price, "take_profit"):
                        closed_any = True
                continue

            # ── Классический режим (SL/TP, трейлинг с безубытком) ───────────
            if profit_pct >= Config.TRAIL_STAGE4_AT:
                trail_pct = Config.TRAIL_STAGE4_PCT
            elif profit_pct >= Config.TRAIL_STAGE3_AT:
                trail_pct = Config.TRAIL_STAGE3_PCT
            elif profit_pct >= Config.TRAIL_STAGE2_AT:
                trail_pct = Config.TRAIL_STAGE2_PCT
            else:
                # Читаем трейл из торговой записи (может быть скальп-значением)
                trail_pct = trade.get("trail_pct", Config.TRAILING_STOP_PCT)

            if price > trade.get("high_water", entry):
                trade["high_water"] = price
            high_water = trade.get("high_water", entry)

            new_sl = self.exchange._round(high_water * (1 - trail_pct / 100))

            if profit_pct >= Config.TRAIL_BREAKEVEN_AT:
                breakeven_sl = self.exchange._round(entry * (1 + Config.FEE_ROUND_TRIP / 100))
                new_sl = max(new_sl, breakeven_sl)

            if new_sl > trade["stop_loss"]:
                old_sl = trade["stop_loss"]
                trade["stop_loss"] = new_sl
                stage_label = (
                    f"≥{Config.TRAIL_STAGE4_AT:.0f}% (trail {trail_pct}%)" if profit_pct >= Config.TRAIL_STAGE4_AT else
                    f"≥{Config.TRAIL_STAGE3_AT:.0f}% (trail {trail_pct}%)" if profit_pct >= Config.TRAIL_STAGE3_AT else
                    f"≥{Config.TRAIL_STAGE2_AT:.0f}% (trail {trail_pct}%)" if profit_pct >= Config.TRAIL_STAGE2_AT else
                    f"≥{Config.TRAIL_BREAKEVEN_AT:.0f}% → безубыток" if profit_pct >= Config.TRAIL_BREAKEVEN_AT else
                    f"trail {trail_pct}%"
                )
                self.log(
                    f"🔼 Стоп: {old_sl} → {new_sl} | "
                    f"прибыль {profit_pct:+.1f}% | {stage_label}", "INFO"
                )

            if price <= trade["stop_loss"]:
                reason = "trailing_stop" if trade["stop_loss"] > entry else "stop_loss"
                if self._close_trade(trade, price, reason):
                    closed_any = True
            elif price >= trade["take_profit"]:
                if self._close_trade(trade, price, "take_profit"):
                    closed_any = True

        # Если SL/TP реально закрыл позиции и больше открытых нет — сводим
        # виртуальное состояние юзеров (обнуляем grinch_held, разблокируем вывод).
        # Только при полном закрытии: частичное оставляет реальный GRINCH в рынке.
        if closed_any and had_relevant and not self._relevant_open():
            self._emit_signal("SELL", price, self.last_ai)
            return True
        return False

    def close_trade(self, trade_id):
        """Ручное закрытие ОДНОЙ позиции по её id (рыночная продажа сейчас)."""
        trade = next((t for t in self.open_trades
                      if str(t.get("id")) == str(trade_id)), None)
        if not trade:
            return {"ok": False, "error": "Позиция не найдена или уже закрыта"}
        try:
            from price_feed import price_feed
            price = price_feed.get("GRINCH") or trade.get("entry_price")
        except Exception:
            price = trade.get("entry_price")
        # Режим «только в плюс»: даже РУЧНОЕ закрытие не продаёт в минус.
        # Порог включает газ обоих свопов — настоящая гарантия реальной прибыли.
        if Config.ONLY_PROFIT_EXIT:
            entry         = trade.get("entry_price") or 0
            stake_ton     = trade.get("stake_ton") or None
            pnl_pct       = (price - entry) / entry * 100 if entry else 0.0
            net_floor_pct = Config.required_gross_pct_with_gas(stake_ton)
            if pnl_pct < net_floor_pct:
                self.log(
                    f"⏸️ Ручная продажа отклонена: прибыль {pnl_pct:+.1f}% < "
                    f"мин. +{net_floor_pct:.1f}% (газ учтён). Держим.",
                    "INFO"
                )
                return {"ok": False, "error": (
                    f"Продажа в минус отключена: прибыль {pnl_pct:+.1f}% ниже "
                    f"минимума +{net_floor_pct:.1f}% (с учётом газа). Ждём роста цены.")}
        self.log(f"🖐 Ручное закрытие позиции {trade_id} @ {price}", "INFO")
        ok = self._close_trade(trade, price, "manual")
        return {"ok": True} if ok else {
            "ok": False, "error": "Продажа не исполнена — попробуйте ещё раз позже"}

    def delete_trade(self, trade_id):
        """Удалить позицию из списка БЕЗ продажи на блокчейне (только из памяти/БД)."""
        with self._close_lock:
            trade = next((t for t in self.open_trades
                          if str(t.get("id")) == str(trade_id)), None)
            if not trade:
                return {"ok": False, "error": "Позиция не найдена или уже удалена"}
            self.open_trades = [t for t in self.open_trades
                                if str(t.get("id")) != str(trade_id)]
            self.trades = [t for t in self.trades
                           if str(t.get("id")) != str(trade_id)]
        self.log(f"🗑 Позиция {trade_id} удалена вручную (без продажи)", "WARNING")
        try:
            import db_store
            db_store.open_trades_save(self._combined_open_trades())
        except Exception:
            pass
        try:
            self.exp.save_open_trades(self._combined_open_trades())
        except Exception:
            pass
        return {"ok": True}

    def acknowledge_liquidator_sell(self, price_usd: float):
        """Вызывается ликвидатором после успешной on-chain продажи GRINCH.

        Закрывает все открытые лонг-позиции в памяти и DB без повторного
        on-chain свопа — ликвидатор уже продал, нам нужно только почистить
        ghost-запись, чтобы дашборд не показывал несуществующую позицию.
        """
        with self._close_lock:
            if not self.open_trades:
                return
            long_trades = [t for t in self.open_trades if t.get("side") != "short"]
            if not long_trades:
                return
            now_iso = datetime.utcnow().isoformat()
            for trade in long_trades:
                trade["exit_price"]   = price_usd
                trade["closed_at"]    = now_iso
                trade["close_reason"] = "liquidator_auto_sell"
                trade["status"]       = "closed"
                # PnL посчитаем честно с учётом комиссии
                fee = Config.FEE_PCT / 100.0
                buy_gas  = Config.BUY_GAS_TON
                sell_gas = Config.SELL_GAS_TON
                grinch_ton = price_usd / (self._get_grinch_price_ton() or 1) if price_usd else 0
                try:
                    from price_feed import price_feed as _pf
                    _gtpy = _pf.get("TON") or 1.0
                    grinch_ton = price_usd / _gtpy if _gtpy else 0
                except Exception:
                    pass
                amount    = trade.get("amount", 0) or 0
                stake_ton = trade.get("stake_ton", 0) or 0
                # TON получено ≈ amount × price_usd / TON_price × (1 - fee)
                try:
                    from price_feed import price_feed as _pf2
                    _ton_usd = _pf2.get("TON") or 1.64
                    proceeds  = amount * (price_usd / _ton_usd) * (1 - fee) - sell_gas
                except Exception:
                    proceeds = 0.0
                pnl_ton = round(proceeds - stake_ton - buy_gas, 6)
                trade["pnl"]     = pnl_ton
                trade["outcome"] = "win" if pnl_ton > 0 else "loss"
                self.stats["total_pnl"] = round(self.stats.get("total_pnl", 0) + pnl_ton, 6)
                if pnl_ton > 0:
                    self.stats["winning_trades"] = self.stats.get("winning_trades", 0) + 1
                self.stats["total_trades"] = self.stats.get("total_trades", 0) + 1
            # Удаляем лонги из открытых
            self.open_trades = [t for t in self.open_trades if t.get("side") == "short"]
            self.dca_entries_count = 0
            self.dca_total_stake   = 0.0
        self.log(
            f"🔧 Ликвидатор продал GRINCH — ghost-позиция закрыта "
            f"({len(long_trades)} запись/ей) @ ${price_usd:.8f}",
            "WARN",
        )
        try:
            self.exp.save_open_trades(self._combined_open_trades())
        except Exception:
            pass
        try:
            import db_store
            db_store.open_trades_save(self._combined_open_trades())
        except Exception:
            pass
        # ── Записываем каждую закрытую сделку в журнал (bot_trades) ──────────
        # Без этого закрытые позиции не попадают в историю сделок и не
        # учитываются в analyze_and_adapt (AI learning loop).
        for trade in long_trades:
            try:
                self.exp.record_trade(dict(trade), dict(self.stats), None)
                self.trades.append(dict(trade))
            except Exception as _rt_err:
                self.log(f"⚠️ record_trade (liquidator): {_rt_err}", "WARN")
        try:
            self.exp.save()
        except Exception:
            pass

    def _close_trade(self, trade, price, reason):
        """Сериализует закрытие (лок) и защищает от двойной продажи позиции."""
        with self._close_lock:
            if trade.get("id") not in {t.get("id") for t in self.open_trades}:
                return False   # уже закрыта другим потоком
            return self._close_trade_locked(trade, price, reason)

    def _close_trade_locked(self, trade, price, reason):
        """
        Закрывает позицию:
        1. Исполняет реальную продажу GRINCH на блокчейне (DeDust режим)
        2. Рассчитывает виртуальный P&L
        3. Обновляет статистику и AI feedback
        """
        # ── 1. РЕАЛЬНАЯ продажа GRINCH через DeDust ─────────────────────
        if self.exchange.mode == "dedust":
            grinch_amount = trade.get("amount", 0)
            # ── Консолидация: если это последняя открытая LONG-позиция,
            # продаём ВЕСЬ GRINCH на балансе одной сделкой (не только
            # то, что учтено в trade["amount"]) — так пыль/расхождения
            # после ре-мержа или ручных операций не остаются непроданными.
            if trade.get("side") == "buy":
                other_longs = [t for t in self.open_trades
                               if t.get("side") == "buy" and t.get("id") != trade.get("id")]
                if not other_longs:
                    try:
                        real_bal = self.exchange.get_balance() or {}
                        real_grinch = float(real_bal.get("GRINCH", 0) or 0)
                        reserve = Config.GRINCH_RESERVE if (
                            Config.SHORT_TRADING_ENABLED or self.open_short_trades
                        ) else 0.0
                        sweepable = max(0.0, real_grinch - reserve)
                        if sweepable > grinch_amount:
                            self.log(
                                f"🧹 Консолидация: на балансе {real_grinch:.4f} GRINCH "
                                f"(учтено {grinch_amount:.4f}) — продаём всё "
                                f"{sweepable:.4f} одной сделкой",
                                "INFO"
                            )
                            grinch_amount = sweepable
                    except Exception as _sw_e:
                        self.log(f"⚠️ Не удалось сверить баланс для консолидации: {_sw_e}", "WARN")
            if grinch_amount > 0:
                # ── ЖЕЛЕЗНЫЙ ЗАМОК: проверяем TON-цену перед блокчейном ──────
                # Даже если все верхние проверки пройдены, делаем финальную
                # верификацию по РЕАЛЬНОЙ on-chain цене (TON/GRINCH).
                # Продажа в минус по TON абсолютно невозможна.
                if Config.ONLY_PROFIT_EXIT:
                    try:
                        from price_feed import price_feed as _pf2
                        cur_ton = _pf2.get_grinch_ton_price() or 0.0
                        entry_ton = trade.get("entry_price_ton") or 0.0
                        if cur_ton > 0 and entry_ton > 0:
                            min_sell_ton = entry_ton * (1.0 + Config.FEE_ROUND_TRIP / 100.0)
                            if cur_ton < min_sell_ton:
                                self.log(
                                    f"🛡️ ЖЕЛЕЗНЫЙ ЗАМОК: продажа заблокирована — "
                                    f"цена {cur_ton:.8f} TON < порог {min_sell_ton:.8f} TON "
                                    f"(вход {entry_ton:.8f}, нужно +{Config.FEE_ROUND_TRIP:.1f}%). Держим.",
                                    "WARN"
                                )
                                return False
                    except Exception:
                        pass
                # AMM preflight: стоимость этой конкретной позиции
                _stake_ton   = float(trade.get("stake_ton", 0) or 0)
                _min_net_ton = _stake_ton + Config.BUY_GAS_TON
                self.log(
                    f"💸 Продаём {grinch_amount:.6f} GRINCH на DeDust "
                    f"(причина: {reason}, AMM min_net ≥ {_min_net_ton:.3f} TON)...", "INFO"
                )
                sell_ok = False
                try:
                    sell_result = self.exchange.place_order("sell", grinch_amount, min_net_ton=_min_net_ton)
                    if sell_result and not sell_result.get("error"):
                        sell_ok = True
                        self.log(
                            f"✅ Продажа GRINCH → TON исполнена | "
                            f"id={sell_result.get('id', '—')}", "INFO"
                        )
                    else:
                        err = sell_result.get("error", "нет ответа") if sell_result else "нет ответа"
                        if (sell_result or {}).get("amm_blocked"):
                            self.log(f"🛡️ AMM preflight заблокировал продажу: {err}", "WARN")
                        else:
                            self.log(f"⚠️ Продажа не исполнена: {err}", "WARN")
                except Exception as e:
                    self.log(f"⚠️ Ошибка продажи GRINCH: {e}", "WARN")

                if not sell_ok:
                    # Реальная продажа не прошла — НЕ закрываем позицию виртуально.
                    # Оставляем её открытой и повторим продажу на следующем тике.
                    # Так виртуальное состояние юзеров остаётся синхронным с реальными
                    # активами (grinch_held>0 → вывод заблокирован, пока не продадим).
                    self.log("⏳ Позиция остаётся открытой — повтор продажи позже", "WARN")
                    return False

        # ── 2. Виртуальный P&L ───────────────────────────────────────────
        gross = (price - trade["entry_price"]) * trade["amount"]
        # Комиссия обеих ног DeDust: FEE_PCT за вход + FEE_PCT за выход
        fee   = (trade["entry_price"] + price) * trade["amount"] * Config.FEE_PCT / 100
        pnl_raw = gross - fee

        # В DeDust-режиме цены в USD → конвертируем P&L в TON для корректного отображения
        if self.exchange.mode == "dedust":
            from price_feed import price_feed
            ton_usd = price_feed.get("TON") or 2.44
            pnl = round(pnl_raw / ton_usd, 6)
        else:
            pnl = round(pnl_raw, 6)

        trade["pnl"]          = pnl
        trade["fee"]          = round(fee, 6)
        trade["exit_price"]   = price
        trade["closed_at"]    = datetime.utcnow().isoformat()
        trade["close_reason"] = reason
        trade["status"]       = "closed"

        self.stats["total_pnl"] = round(self.stats["total_pnl"] + pnl, 6)
        # Единая точка учёта: total_trades считается только здесь, в момент
        # закрытия — синхронно с вызовом record_trade() ниже (в блоке 4).
        self.stats["total_trades"] = self.stats.get("total_trades", 0) + 1
        if pnl > 0:
            self.stats["winning_trades"] += 1

        self.open_trades = [t for t in self.open_trades if t["id"] != trade["id"]]
        for t in self.trades:
            if t["id"] == trade["id"]:
                t.update(trade)
                break

        # ── 3. AI feedback: самообучение с режимом и уверенностью ──────────
        try:
            outcome  = "win" if pnl > 0 else "loss"
            ai_snap  = self.last_ai or {}
            reg_name = (ai_snap.get("regime") or {}).get("name", "UNKNOWN")
            ai_conf  = float(ai_snap.get("confidence", 0) or 0)
            self.ai.feedback(outcome=outcome, pnl=float(pnl),
                             regime=reg_name, conf=ai_conf)
            self.log(f"🧠 AI feedback: {outcome}({reg_name}) PNL={pnl:+.6f} TON conf={ai_conf:.0f}%", "INFO")
            # Кулдаун после убытка: фиксируем время для защиты от повторного входа
            if outcome == "loss":
                self._last_loss_ts = time.time()
                self._save_volatile_state()  # cooldown переживёт рестарт
                self.log(
                    f"⏸️ Loss cooldown активирован: пауза {Config.LOSS_COOLDOWN_SEC//60} мин перед следующим входом",
                    "WARN"
                )
        except Exception as e:
            self.log(f"AI feedback ошибка: {e}", "WARN")

        # ── Добавляем рыночный контекст при закрытии для аналитики ──────
        try:
            ai_snap = self.last_ai or {}
            regime  = ai_snap.get("regime") or {}
            opened_ts = None
            try:
                from datetime import timezone
                from dateutil import parser as _dp
                opened_ts = _dp.parse(trade.get("opened_at", "")).replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                pass
            closed_ts = time.time()
            trade["duration_min"] = round((closed_ts - opened_ts) / 60, 1) if opened_ts else None
            trade["exit_ai_confidence"] = ai_snap.get("confidence")
            trade["exit_ai_signal"]     = ai_snap.get("ai_signal")
            trade["exit_regime"]        = regime.get("name")
            trade["exit_rsi"]           = ai_snap.get("rsi")
            trade["exit_atr_pct"]       = (regime.get("atr_pct") or regime.get("atr"))
            trade["exit_anomaly"]       = (ai_snap.get("anomaly") or {}).get("detected", False)
            # Умные деньги в момент закрытия
            try:
                from wallet_tracker import wallet_tracker as _wt
                sm = _wt.get_signal()
                trade["exit_sm_score"] = sm.get("score")
                trade["exit_sm_label"] = sm.get("label")
            except Exception:
                pass
            trade["outcome"] = "win" if pnl > 0 else "loss"
            trade["pnl_pct"] = round(pnl / trade.get("stake_ton", 1) * 100, 2) if trade.get("stake_ton") else None
        except Exception as e:
            self.log(f"Контекст сделки: {e}", "WARN")

        # ── 4. Память + само-управление ИИ ───────────────────────────────
        try:
            self.exp.save_open_trades(self._combined_open_trades())   # позиция закрыта → обновляем диск
            self.exp.record_trade(trade, self.stats, self.ai)
            from price_feed import price_feed
            self.exp.record_balance(
                self._get_balance_cached(),
                price_feed.get("GRINCH") or price,
                force=True,
            )
            self.exp.analyze_and_adapt(self, self.ai)
        except Exception as e:
            self.log(f"Память/адаптация: {e}", "WARN")

        # ── Уведомляем AI Советника (триггер адаптации) — полный контекст ─
        try:
            from ai_advisor import notify_trade_closed
            notify_trade_closed(pnl, {
                **{k: trade.get(k) for k in (
                    "pnl_pct", "stake_ton", "exit_price", "close_reason",
                    "outcome", "duration_min", "exit_ai_confidence",
                    "exit_ai_signal", "exit_regime", "exit_rsi", "exit_atr_pct",
                )},
                "strategy": "AI",
            })
        except Exception:
            pass

        emoji = "🟩" if pnl >= 0 else "🟥"
        self.log(
            f"{emoji} Закрыто @ {price} | PNL={pnl:+.6f} TON | {reason}", 
            "SELL" if pnl >= 0 else "ERROR"
        )
        # ── BrainFusion: обратная связь после закрытия AI-сделки ─────────────
        try:
            _bf.on_trade_closed(pnl, was_scalp=self._last_entry_was_scalp)
        except Exception:
            pass
        self._last_entry_was_scalp = False  # сброс флага скальпа
        # ── Аналитический буфер: событие закрытия позиции ────────────
        try:
            from analytics_buffer import analytics_buffer as _ab
            _ab.push_trade("CLOSE", {
                "price":        price,
                "stake_ton":    trade.get("stake_ton", 0),
                "pnl_ton":      pnl,
                "pnl_pct":      trade.get("pnl_pct") or 0,
                "regime":       trade.get("exit_regime") or trade.get("entry_regime") or "?",
                "ai_conf":      trade.get("exit_ai_confidence") or trade.get("ai_confidence") or 0,
                "close_reason": reason,
                "dca_entries":  self.dca_entries_count,
            })
        except Exception:
            pass
        return True

    # ──────────────────────────────────────────
    # Статус
    # ──────────────────────────────────────────
    def _get_balance_cached(self) -> dict:
        """Возвращает кешированный баланс (TTL 30 сек) — не тратим RTT к блокчейну на каждый poll."""
        now = time.time()
        if now - self._balance_cache_ts < self._balance_cache_ttl and self._balance_cache:
            return self._balance_cache
        bal = self.exchange.get_balance()
        # Кешируем только если оба баланса ненулевые — нули могут быть из-за сбоя API
        if bal and not bal.get("error") and (bal.get("TON", 0) > 0 or bal.get("GRINCH", 0) > 0):
            self._balance_cache    = bal
            self._balance_cache_ts = now
        elif bal and not self._balance_cache:
            # Если кеш пустой — сохраняем даже нули (лучше чем ничего)
            self._balance_cache    = bal
            self._balance_cache_ts = now
        # Если GRINCH=0 но ликвидатор уже знает баланс — берём из ликвидатора
        try:
            from grinch_liquidator import liquidator as _liq
            if bal and bal.get("GRINCH", 0) == 0:
                liq_st = _liq.get_status()
                grn = liq_st.get("grinch_balance", 0) or 0
                ton = liq_st.get("ton_balance")
                if grn > 0:
                    bal = dict(bal)
                    bal["GRINCH"] = grn
                    if ton is not None and bal.get("TON", 0) == 0:
                        bal["TON"] = ton
                    self._balance_cache    = bal
                    self._balance_cache_ts = now
        except Exception:
            pass
        return bal

    def _enriched_open_trades(self, grinch_ton):
        """Открытые сделки + расчёт «если продать сейчас» с учётом ОБЕИХ
        транзакций и газа ОБОИХ свопов.

        Схема реальных затрат пользователя:
          Покупка:  stake_ton (в пул) + buy_gas (~0.25 TON газа сети)  → получает amount GRINCH
          Продажа:  amount * cur_ton * (1 - fee) − sell_gas            → получает TON обратно

        Итоговый результат = выручка_от_продажи − stake_ton − buy_gas
        Безубыток = цена, при которой этот результат = 0.
        """
        out = []
        fee      = Config.FEE_PCT / 100.0
        sell_gas = Config.SELL_GAS_TON
        buy_gas  = Config.BUY_GAS_TON
        cur_ton  = grinch_ton or 0
        for t in self.open_trades:
            c = dict(t)
            amount    = t.get("amount", 0) or 0
            stake_ton = t.get("stake_ton", 0) or 0
            entry_usd = t.get("entry_price", 0) or 0
            # Минимальный gross % для выхода в реальный плюс с учётом газа
            min_gross_pct = Config.required_gross_pct_with_gas(stake_ton if stake_ton > 0 else None)
            c["min_gross_pct"] = round(min_gross_pct, 1)
            if cur_ton > 0 and amount > 0 and stake_ton > 0:
                value_now  = amount * cur_ton                        # текущая стоимость в TON
                proceeds   = value_now * (1 - fee) - sell_gas       # выручка после комиссии продажи и газа
                total_cost = stake_ton + buy_gas                    # реальные затраты: ставка + газ покупки
                net_ton    = proceeds - total_cost                  # чистый результат (+ = прибыль)
                c["value_ton_now"] = round(value_now, 6)
                c["net_ton_now"]   = round(net_ton, 6)
                c["net_pct_now"]   = round(net_ton / total_cost * 100, 2)
                c["in_profit"]     = bool(net_ton > 0)
                # Безубыточная цена за GRINCH (где net=0), в USD для карточки.
                # amount * be_ton * (1 - fee) - sell_gas = total_cost
                # be_ton = (total_cost + sell_gas) / (amount * (1 - fee))
                entry_ton = stake_ton / amount
                if entry_ton > 0 and entry_usd > 0:
                    be_ton = (total_cost + sell_gas) / (amount * (1 - fee))
                    c["breakeven_price"] = round(entry_usd * be_ton / entry_ton, 8)
            out.append(c)
        return out

    def _enriched_short_trades(self, grinch_ton):
        """Шорт-позиции + расчёт текущего P&L и прогресса к цели."""
        out = []
        cur_ton = grinch_ton or 0
        for t in self.open_short_trades:
            c = dict(t)
            entry_usd   = t.get("entry_price", 0) or 0
            amount      = t.get("amount", 0) or 0        # GRINCH продано
            grinch_val  = t.get("grinch_value_ton", 0)
            ton_recv    = t.get("ton_received", 0)
            required_dr = t.get("required_drop_pct", Config.required_drop_pct_for_short(grinch_val))

            if cur_ton > 0 and entry_usd > 0:
                drop_pct = (entry_usd - cur_ton) / entry_usd * 100
                c["drop_pct_now"]  = round(drop_pct, 2)
                c["in_profit"]     = drop_pct >= required_dr
                c["progress_pct"]  = round(min(drop_pct / required_dr * 100, 200) if required_dr > 0 else 0, 1)
                # Если сейчас откупить: сколько GRINCH получим
                fee = Config.FEE_PCT / 100.0
                if cur_ton > 0:
                    grinch_back_est = ton_recv * (1 - fee) / cur_ton
                    profit_grinch   = grinch_back_est - amount
                    c["grinch_profit_est"] = round(profit_grinch, 4)
                    c["pnl_ton_now"]       = round(profit_grinch * cur_ton, 6)
            c["required_drop_pct"] = round(required_dr, 2)
            out.append(c)
        return out

    def get_status(self):
        # Используем last_analysis из последнего торгового тика (обновляется каждые 15с).
        # Fallback: считаем напрямую только при первом обращении до первого тика.
        ohlcv    = self.exchange.get_ohlcv(limit=100)
        analysis = self.last_analysis if self.last_analysis else analyze(ohlcv)
        # Единый источник «текущей цены» для всего UI: спотовая цена DexScreener
        # (price_feed.get), та же, что использует авто-ликвидатор и карточка монеты.
        # Иначе hero/кошелёк показывают close последней свечи (GeckoTerminal), а
        # ликвидатор — спот, и числа расходятся (~1%). Свечи для графика/индикаторов
        # не трогаем — меняем только отображаемую цену.
        grinch_ton = None
        try:
            from price_feed import price_feed
            spot = price_feed.get("GRINCH")
            if spot and spot > 0:
                analysis["price"] = spot
            # Курс 1 GRINCH в GRAM (TON) — реальный курс пула (priceNative)
            grinch_ton = price_feed.get_grinch_ton_price()
        except Exception:
            pass
        ai       = self.last_ai if self.last_ai else self.ai.analyze(ohlcv)
        balance  = self._get_balance_cached()
        winrate  = 0
        _tt = int(self.stats.get("total_trades") or 0)
        _wt = int(self.stats.get("winning_trades") or 0)
        if _tt > 0:
            winrate = round(_wt / _tt * 100, 1)
        # Гарантируем что stats не содержит None (защита от устаревших БД-записей)
        self.stats["total_trades"]   = _tt
        self.stats["winning_trades"] = _wt
        self.stats["total_pnl"]      = float(self.stats.get("total_pnl") or 0)
        pb = self._pending_buy
        # AI-управление: текущие адаптированные параметры (просадка, пауза, порог)
        ai_mgmt = {}
        try:
            ai_mgmt = self.exp.get_report()
        except Exception:
            pass
        # AI Full Rights: активен ли в текущем тике
        _ai_conf = ai.get("confidence", 0) if ai else 0
        _ai_full_rights_active = (
            Config.AI_FULL_RIGHTS and
            _ai_conf >= Config.AI_FULL_RIGHTS_MIN_CONF
        )
        # ── DCA статус ───────────────────────────────────────────────
        dca_portfolio_pct = None
        if Config.DCA_MODE and self.open_trades and grinch_ton:
            try:
                cost_ton, val_ton = self._dca_portfolio_value(grinch_ton)
                if cost_ton > 0:
                    dca_portfolio_pct = round((val_ton - cost_ton) / cost_ton * 100, 2)
            except Exception:
                pass

        return {
            "running":       self.running,
            "trading_enabled": self.trading_enabled,
            "training":      self.training,
            "demo_mode":     self.exchange.demo_mode,
            "symbol":        Config.SYMBOL,
            "grinch_ton":    grinch_ton,
            "balance":       balance,
            "analysis":      analysis,
            "ai":            ai,
            "smart_money":   self.last_sm,
            "ai_management": ai_mgmt,
            "open_trades":       self._enriched_open_trades(grinch_ton),
            "open_short_trades": self._enriched_short_trades(grinch_ton),
            "recent_trades": self.trades[-20:],
            "logs":          self.logs[-50:],
            "stats":         {**self.stats, "winrate": winrate},
            "training_progress": self.ai.training_progress,
            "entry_quality": self.last_entry,
            "decision_log":  list(reversed(self.decision_log))[:12],
            "db_synced_secs": int(time.time() - self._last_db_sync_ts) if self._last_db_sync_ts else None,
            "ai_full_rights":        Config.AI_FULL_RIGHTS,
            "ai_full_rights_min_conf": Config.AI_FULL_RIGHTS_MIN_CONF,
            "ai_full_rights_active": _ai_full_rights_active,
            "pending_buy":   {
                "target":        pb["target"],
                "signal_price":  pb["signal_price"],
                "ticks_left":    pb["ticks_left"],
                "ai_conf":       (pb.get("ai") or {}).get("confidence", 0),
                "entry_quality": pb.get("entry_quality", "B"),
                "pullback_pct":  pb.get("pullback_pct", Config.SMART_BUY_PULLBACK_PCT),
            } if pb else None,
            # ── DCA стратегия ──────────────────────────────────────────
            "dca_mode":             Config.DCA_MODE,
            "dca_state": {
                "wait_pullback":   self.dca_wait_pullback,
                "peak_price":      self.dca_peak_price,
                "last_buy_price":  self.dca_last_buy_price,
                "entries_count":   self.dca_entries_count,
                "total_stake":     self.dca_total_stake,
                "portfolio_pct":   dca_portfolio_pct,
                "target_pct":      Config.DCA_TARGET_PROFIT_PCT,
                "drop_trigger_pct": Config.DCA_DROP_TRIGGER_PCT,
                "pullback_wait_pct": Config.DCA_PULLBACK_WAIT_PCT,
                "stake_ton":       Config.DCA_STAKE_TON,
                "max_entries":     Config.DCA_MAX_ENTRIES,
                # ── Новые поля: 4 улучшения ───────────────────────────
                "cascade_enabled":     Config.DCA_CASCADE_ENABLED,
                "cascade_half_sold":   self.dca_cascade_half_sold,
                "cascade_level1_pct":  Config.DCA_CASCADE_LEVEL1_PCT,
                "cascade_level2_pct":  Config.DCA_CASCADE_LEVEL2_PCT,
                "compound_enabled":    Config.DCA_COMPOUND_ENABLED,
                "compound_bonus_ton":  round(self.dca_compound_bonus_ton, 3),
                "compound_ratio":      Config.DCA_COMPOUND_RATIO,
                "smart_reentry_enabled": Config.DCA_SMART_REENTRY_ENABLED,
                "smart_reentry_pullback": Config.DCA_SMART_REENTRY_PULLBACK_PCT,
                "smart_reentry_ai_conf":  Config.DCA_SMART_REENTRY_MIN_AI_CONF,
                "adaptive_trigger_enabled": Config.DCA_ADAPTIVE_TRIGGER_ENABLED,
                "adaptive_fast_move_pct": Config.DCA_ADAPTIVE_FAST_MOVE_PCT,
                "adaptive_fast_drop_pct": Config.DCA_ADAPTIVE_FAST_DROP_PCT,
            } if Config.DCA_MODE else None,
        }
