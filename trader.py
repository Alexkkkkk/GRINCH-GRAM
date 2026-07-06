import threading
import time
from datetime import datetime
from config import Config
from exchange import ExchangeClient
from strategy import analyze
from ai_engine import AIEngine
from experience_manager import experience_manager
import liquidity_guard


class Trader:
    def __init__(self):
        self.exchange = ExchangeClient()
        self.ai       = AIEngine()
        self.running  = False
        self.training = False
        self.trades      = []
        self.open_trades = []
        self.logs        = []
        self.last_ai     = {}
        self.stats = {
            "total_trades":   0,
            "winning_trades": 0,
            "total_pnl":      0.0,
            "start_balance":  10000.0,
        }
        self._thread = None
        self.signal_callbacks = []
        self.on_training_progress = None
        # Сериализация закрытия позиций: не даём торговому циклу и ручному
        # закрытию продать одну и ту же позицию дважды.
        self._close_lock = threading.Lock()
        # Счётчик подтверждений BUY-сигнала (требуем 2 последовательных)
        self._buy_confirm_count = 0
        # Smart BUY: ожидаем откат к лучшей цене перед входом
        # Структура: {"target": float, "signal_price": float, "ai": dict,
        #              "analysis": dict, "ticks_left": int}
        self._pending_buy = None
        self.last_sm      = None   # последний сигнал умных денег (для статуса)
        self.decision_log = []     # кольцевой буфер AI-решений (макс 25)
        self._last_db_sync_ts = 0  # время последней синхронизации с DB
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
        # Детектор крупных продаж: время последней безусловной покупки по этому триггеру
        self._last_large_sell_buy_ts = 0.0
        # Защита прибыли: пик стоимости портфеля (TON) для детектора разворота
        self.portfolio_high_water_ton = 0.0
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
            import db_store as _dbs2
            pb_raw = _dbs2.settings_get("trader_state", "pending_buy")
            if pb_raw:
                import json as _json2
                pb_data = _json2.loads(pb_raw)
                if pb_data and pb_data.get("target"):
                    pb_data["restored"] = True   # флаг: ai/analysis отсутствуют
                    self._pending_buy = pb_data
        except Exception:
            pass

    def log(self, msg, level="INFO"):
        entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "level": level, "msg": msg}
        self.logs.append(entry)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        print(f"[{entry['time']}] [{level}] {msg}")

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log("Торговый агент запущен", "INFO")

    def stop(self):
        self.running = False
        self.training = False
        self.log("Торговый агент остановлен", "WARN")

    # ──────────────────────────────────────────
    # Главный цикл
    # ──────────────────────────────────────────
    def _loop(self):
        self.training = True
        self.log("🧠 Начинаю предобучение AI модели...", "INFO")
        try:
            ohlcv = self.exchange.get_ohlcv(limit=300)
            self.ai.pretrain(ohlcv, on_progress=self._emit_progress)
        except Exception as e:
            self.log(f"⚠️ Ошибка предобучения: {e}", "WARN")
        self.training = False
        self.log("✅ Предобучение завершено. Запускаю торговый цикл.", "INFO")
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
                if self.open_trades and (now - _last_db_sync) >= 60:
                    self._sync_open_trades_to_db()
                    _last_db_sync = now
                self.last_tick_ts = time.time()
                self.last_tick_ok = True
            except Exception as e:
                self.log(f"Ошибка в цикле: {e}", "ERROR")
                self.last_tick_ts = time.time()
                self.last_tick_ok = False
            time.sleep(15)

    def _record_equity(self):
        """Снимок капитала кошелька в память (троттлинг внутри менеджера)."""
        try:
            from price_feed import price_feed
            self.exp.record_balance(self._get_balance_cached(),
                                    price_feed.get("GRINCH") or 0.0)
        except Exception:  # noqa: BLE001
            pass

    def _clear_pending_buy(self):
        """Сбрасывает Smart BUY и удаляет его из DB."""
        self._pending_buy = None
        try:
            import db_store as _dbs
            _dbs.settings_update_section("trader_state", {"pending_buy": ""})
        except Exception:
            pass

    def _sync_open_trades_to_db(self):
        """Обновляет live-поля открытых позиций в PostgreSQL (раз в 60 сек)."""
        try:
            from price_feed import price_feed
            import db_store
            if not db_store.is_available():
                return
            grinch_ton = price_feed.get_grinch_ton_price() or 0.0
            enriched   = self._enriched_open_trades(grinch_ton)
            db_store.open_trades_save(enriched)
            self._last_db_sync_ts = time.time()
        except Exception:
            pass  # молча: live-синк не критичен

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
        buy_gas_each  = getattr(Config, "BUY_GAS_TON", 0.103)
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
            self.exp.save_open_trades(self.open_trades)
        except Exception:
            pass

    def _check_profit_protection(self, price_usd: float, grinch_ton: float) -> bool:
        """
        Защита прибыли: если портфель в плюсе >= PROFIT_PROTECT_TON TON
        И рынок начал падать (откат от пика портфеля >= PROFIT_PROTECT_DROP_PCT%
        ИЛИ AI-сигнал SELL с уверенностью >= 55%) — продаём ВСЁ немедленно.

        Работает в DCA-режиме и AI-режиме. Уважает ONLY_PROFIT_EXIT.
        Сбрасывает portfolio_high_water_ton после продажи.
        """
        if not Config.PROFIT_PROTECT_ENABLED:
            return False
        if not self.open_trades:
            return False
        if grinch_ton <= 0 or price_usd <= 0:
            return False

        # Текущая прибыль портфеля в TON
        total_cost_ton, total_value_ton = self._dca_portfolio_value(grinch_ton)
        if total_cost_ton <= 0 or total_value_ton <= 0:
            return False

        profit_ton = total_value_ton - total_cost_ton

        # Обновляем пик стоимости портфеля
        if total_value_ton > self.portfolio_high_water_ton:
            self.portfolio_high_water_ton = total_value_ton

        # Активируем только когда прибыль достигла порога
        if profit_ton < Config.PROFIT_PROTECT_TON:
            return False

        # ── Детектор разворота ── 1: откат от пика портфеля ───────────
        drop_from_peak = 0.0
        if self.portfolio_high_water_ton > total_value_ton:
            drop_from_peak = (
                (self.portfolio_high_water_ton - total_value_ton)
                / self.portfolio_high_water_ton * 100
            )
        price_fell = drop_from_peak >= Config.PROFIT_PROTECT_DROP_PCT

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
            reason_parts.append(f"откат -{drop_from_peak:.1f}% от пика портфеля")
        if ai_sell:
            ai_conf2 = float((self.last_ai or {}).get("confidence", 0) or 0)
            reason_parts.append(f"AI SELL {ai_conf2:.0f}%")
        reason = " + ".join(reason_parts)

        portfolio_pct = (total_value_ton - total_cost_ton) / total_cost_ton * 100
        total_grinch  = sum(t.get("amount", 0) for t in self.open_trades)

        self.log(
            f"🛡️ ЗАЩИТА ПРИБЫЛИ: +{profit_ton:.4f} TON (+{portfolio_pct:.1f}%) | "
            f"{reason} | продаём {total_grinch:.2f} GRINCH @ ${price_usd:.8f}",
            "INFO"
        )

        closed = self._dca_sell_all(price_usd, grinch_ton, portfolio_pct)
        if closed:
            self.portfolio_high_water_ton = 0.0   # сброс после продажи
            self._emit_signal("SELL", price_usd, self.last_ai)
            self.log(
                f"✅ Защита прибыли ИСПОЛНЕНА: +{profit_ton:.4f} TON зафиксировано | {reason}",
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
        orig = _cfg.Config.TRADE_AMOUNT_TON
        _cfg.Config.TRADE_AMOUNT_TON = float(Config.LARGE_SELL_DCA_TON)
        ai = self.last_ai or {}
        result = self._get_analysis_snapshot()
        opened = self._open_trade("buy", price_usd, result or {}, ai)
        _cfg.Config.TRADE_AMOUNT_TON = orig
        if opened:
            self._last_large_sell_buy_ts = now
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
                orig = _cfg.Config.TRADE_AMOUNT_TON
                _cfg.Config.TRADE_AMOUNT_TON = float(amount_ton)
            ai = self.last_ai or {}
            result = self._get_analysis_snapshot()
            opened = self._open_trade("buy", price, result or {}, ai)
            if amount_ton:
                _cfg.Config.TRADE_AMOUNT_TON = orig
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
        """
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
            pullback_needed    = Config.DCA_PULLBACK_WAIT_PCT

            self.log(
                f"⏳ DCA ожидание отката: пик ${self.dca_peak_price:.8f} → "
                f"сейчас ${price_usd:.8f} | "
                f"откат {drop_from_peak_pct:.1f}% / нужно {pullback_needed:.0f}%",
                "INFO"
            )

            if drop_from_peak_pct >= pullback_needed:
                self.log(
                    f"✅ DCA: откат {drop_from_peak_pct:.1f}% ≥ {pullback_needed:.0f}% — "
                    f"запускаем новый цикл покупок!",
                    "INFO"
                )
                self.dca_wait_pullback  = False
                self.dca_peak_price     = 0.0
                self.dca_entries_count  = 0
                self.dca_total_stake    = 0.0
                # Совершаем первую покупку нового цикла
                self._dca_buy(price_usd, grinch_ton, "новый цикл после отката")
            return

        # ── Фаза 2: Проверка целевой прибыли портфеля ────────────────
        if self.open_trades:
            total_cost_ton, total_value_ton = self._dca_portfolio_value(grinch_ton)

            if total_cost_ton > 0 and total_value_ton > 0:
                portfolio_pct = (total_value_ton - total_cost_ton) / total_cost_ton * 100
                entries       = len(self.open_trades)
                total_grinch  = sum(t.get("amount", 0) for t in self.open_trades)

                self.log(
                    f"📊 DCA портфель: {entries} позиций | "
                    f"вложено {total_cost_ton:.2f} TON | "
                    f"сейчас {total_value_ton:.2f} TON | "
                    f"прибыль {portfolio_pct:+.1f}% / цель +{Config.DCA_TARGET_PROFIT_PCT:.0f}%",
                    "INFO"
                )

                # Цель достигнута → продаём ВСЁ (при условии мин. 3 TON абсолютной прибыли)
                if portfolio_pct >= Config.DCA_TARGET_PROFIT_PCT:
                    profit_ton_abs = total_value_ton - total_cost_ton
                    min_ton = getattr(Config, "MIN_PROFIT_TON_ABS", 3.0)
                    if profit_ton_abs < min_ton:
                        self.log(
                            f"⏳ DCA: цель по % достигнута (+{portfolio_pct:.1f}%) "
                            f"но прибыль {profit_ton_abs:.3f} TON < мин {min_ton:.1f} TON — "
                            f"держим позицию, ждём роста",
                            "INFO"
                        )
                    else:
                        self.log(
                            f"🎯 DCA ЦЕЛЬ: портфель +{portfolio_pct:.1f}% ≥ "
                            f"+{Config.DCA_TARGET_PROFIT_PCT:.0f}% | прибыль {profit_ton_abs:.2f} TON ✅ "
                            f"— продаём ВСЁ! ({total_grinch:.2f} GRINCH)",
                            "INFO"
                        )
                    closed = self._dca_sell_all(price_usd, grinch_ton, portfolio_pct) if profit_ton_abs >= min_ton else False
                    if closed:
                        self.dca_wait_pullback = True
                        self.dca_peak_price    = price_usd
                        self._emit_signal("SELL", price_usd, self.last_ai)
                        self.log(
                            f"⏳ DCA: продали всё, ждём откат -{Config.DCA_PULLBACK_WAIT_PCT:.0f}% "
                            f"от текущего пика ${price_usd:.8f}",
                            "INFO"
                        )
                    return

                # Проверяем DCA-докупку при падении цены
                if self.dca_last_buy_price > 0:
                    drop_from_last_pct = (
                        (self.dca_last_buy_price - price_usd) / self.dca_last_buy_price * 100
                    )
                    if drop_from_last_pct >= Config.DCA_DROP_TRIGGER_PCT:
                        if self.dca_entries_count < Config.DCA_MAX_ENTRIES:
                            self.log(
                                f"📉 DCA ДОКУПКА: цена упала {drop_from_last_pct:.1f}% "
                                f"от последней покупки ${self.dca_last_buy_price:.8f} → "
                                f"${price_usd:.8f} | вход #{self.dca_entries_count + 1}",
                                "INFO"
                            )
                            self._dca_buy(price_usd, grinch_ton,
                                          f"докупка #{self.dca_entries_count + 1} "
                                          f"(падение {drop_from_last_pct:.1f}%)")
                        else:
                            self.log(
                                f"⏸️ DCA: достигнут лимит входов ({Config.DCA_MAX_ENTRIES}), "
                                f"ждём восстановления портфеля",
                                "WARN"
                            )
            return

        # ── Фаза 3: Нет позиций, не ждём — первый вход ───────────────
        if self.dca_entries_count == 0:
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
        """Открывает одну DCA позицию на DCA_STAKE_TON."""
        stake_ton = Config.DCA_STAKE_TON

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

        # Изменяем TRADE_AMOUNT_TON временно для _open_trade
        orig_amount = getattr(Config, "TRADE_AMOUNT", Config.DCA_STAKE_TON)
        try:
            Config.TRADE_AMOUNT = stake_ton
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
            self.stats["total_trades"] += 1
            # Объединяем с уже открытыми LONG-позициями в одну
            self._merge_long_trades()

            # Обновляем DCA-состояние
            self.dca_last_buy_price  = price_usd
            self.dca_entries_count  += 1
            self.dca_total_stake    += stake_ton

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
            Config.TRADE_AMOUNT = orig_amount

    def _dca_sell_all(self, price_usd, grinch_ton, portfolio_pct):
        """Продаёт все DCA позиции одной продажей суммарного GRINCH."""
        if not self.open_trades:
            return False

        total_grinch = sum(t.get("amount", 0) for t in self.open_trades)
        total_stake  = sum(t.get("stake_ton", 0) for t in self.open_trades)

        if total_grinch <= 0:
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
            sell_result = self.exchange.place_order("sell", sell_amount)
            if not sell_result or sell_result.get("error"):
                err = (sell_result or {}).get("error", "нет ответа")
                self.log(f"⚠️ DCA: продажа не исполнена — {err}. Позиции остаются.", "WARN")
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
            self.stats["total_pnl"] = round(self.stats["total_pnl"] + pnl_ton, 6)
            if pnl_ton > 0:
                self.stats["winning_trades"] += 1
            # AI feedback
            try:
                ai_snap  = self.last_ai or {}
                reg_name = (ai_snap.get("regime") or {}).get("name", "UNKNOWN")
                ai_conf  = float(ai_snap.get("confidence", 0) or 0)
                self.ai.feedback(outcome=trade["outcome"], pnl=float(pnl_ton),
                                 regime=reg_name, conf=ai_conf)
            except Exception:
                pass
            # Сохраняем в историю
            for t in self.trades:
                if t["id"] == trade["id"]:
                    t.update(trade)
                    break

        # Снимаем dca_entries ПЕРЕД сбросом (иначе советник получит 0)
        _dca_entries_snap = self.dca_entries_count
        self.open_trades       = []
        self.dca_entries_count = 0
        self.dca_total_stake   = 0.0

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
    def _tick(self):
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
        ai     = self.ai.analyze(ohlcv)
        self.last_ai = ai

        signal      = result["signal"]
        ai_signal   = ai.get("ai_signal", "HOLD")
        price       = result["price"]
        conf        = ai.get("confidence", 0)
        rsi         = result.get("rsi", 50)
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
            if pb.get("restored"):
                self.log(
                    f"🔄 Smart BUY восстановлен после рестарта @ ${price:.8f} (цель была ${pb.get('target', 0):.8f})",
                    "INFO"
                )
                opened = self._open_trade("buy", price, result, ai)
                if opened:
                    self._buy_confirm_count = 0
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
                opened = self._open_trade("buy", price, pb["analysis"], pb["ai"])
                if opened:
                    self._buy_confirm_count = 0
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
                opened = self._open_trade("buy", price, pb["analysis"], pb["ai"])
                if opened:
                    self._buy_confirm_count = 0
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
                getattr(Config, "AI_FULL_RIGHTS", True) and
                conf >= getattr(Config, "AI_FULL_RIGHTS_MIN_CONF", 68.0)
            )

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
            elif anomaly and not hard_override:
                blocked = f"рыночная аномалия Z={ai['anomaly']['z_price']:.2f}"
            elif Config.AI_AUTONOMOUS_MODE:
                # Автономный режим: A/B грейд = 1 подтверждение, C = 2
                auto_confirm = 1 if entry_quality in ("A", "B") else 2
                if self._buy_confirm_count < auto_confirm and not sm_early:
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
            # ── Smart BUY: ждём откат или покупаем сразу ──────────────────
            # Грейд A при высокой уверенности — покупаем сразу (не ждём откат)
            is_elite_instant = (entry_quality == "A" and conf >= Config.SMART_BUY_SKIP_CONF - 10)
            use_smart = (
                Config.SMART_BUY_ENABLED
                and not is_elite_instant            # А-грейд + сильный AI → сразу
                and conf < Config.SMART_BUY_SKIP_CONF
                and not self._pending_buy           # не дублируем ожидание
            )
            if use_smart:
                target = self.exchange._round(price * (1 - pullback_pct / 100))
                self._pending_buy = {
                    "target":        target,
                    "signal_price":  price,
                    "ai":            ai,
                    "analysis":      result,
                    "ticks_left":    Config.SMART_BUY_MAX_WAIT_TICKS,
                    "entry_quality": entry_quality,
                    "pullback_pct":  pullback_pct,
                }
                # Персистируем в DB — переживёт перезапуск
                try:
                    import db_store as _dbs, json as _json
                    _pb_save = {k: v for k, v in self._pending_buy.items() if k not in ("ai", "analysis")}
                    _dbs.settings_update_section("trader_state", {"pending_buy": _json.dumps(_pb_save)})
                except Exception:
                    pass
                self.log(
                    f"🎯 Smart BUY [{entry_quality}-грейд]: ждём откат до ${target:.8f} "
                    f"(сейчас ${price:.8f}, -{pullback_pct:.1f}%, "
                    f"макс {Config.SMART_BUY_MAX_WAIT_TICKS} тика) | AI {conf}%",
                    "INFO"
                )
            else:
                # AI очень уверен ИЛИ A-грейд + сильный сигнал — берём сразу
                if is_elite_instant:
                    self.log(
                        f"🚀 ЭЛИТНЫЙ ВХОД [{entry_quality}]: AI {conf}% + {entry_score} факторов → покупаем немедленно",
                        "INFO"
                    )
                elif conf >= Config.SMART_BUY_SKIP_CONF:
                    self.log(f"⚡ Smart BUY пропущен: AI {conf}% ≥ {Config.SMART_BUY_SKIP_CONF}% — покупаем сразу", "INFO")
                opened = self._open_trade("buy", price, result, ai)
                # Сигнал пользователям шлём ТОЛЬКО если реальный ордер исполнился —
                # иначе у юзеров спишется виртуальный TON и откроется позиция без
                # реальной сделки (рассинхрон балансов, блокировка вывода).
                if opened:
                    self._buy_confirm_count = 0   # сбрасываем после входа
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

    def _targets(self, price, ai, stake_ton=None):
        atr_pct = (ai.get("regime", {}) or {}).get("atr_pct", 0) / 100.0 if ai else 0.0
        if Config.USE_DYNAMIC_TARGETS and atr_pct > 0:
            sl_pct = max(atr_pct * Config.ATR_SL_MULT * 100, Config.STOP_LOSS_PCT)
            tp_pct = max(atr_pct * Config.ATR_TP_MULT * 100, Config.TAKE_PROFIT_PCT)
        else:
            sl_pct, tp_pct = Config.STOP_LOSS_PCT, Config.TAKE_PROFIT_PCT

        # Жёсткий минимум TP учитывает и DEX-комиссию, и газ обоих свопов.
        # Чем меньше ставка — тем выше требуемый gross% для реального плюса.
        min_gross_tp = Config.required_gross_pct_with_gas(stake_ton)
        tp_pct = max(tp_pct, min_gross_tp)
        return sl_pct, tp_pct

    def _open_trade(self, side, price, analysis, ai=None):
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

        ai_size_mult = max(0.3, min(1.5, getattr(Config, "AI_SIZE_MULT", 1.0)))
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
        if not order:
            self.log("⚠️ BUY ордер не исполнен — пропускаем", "WARN")
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
        sl_pct, tp_pct = self._targets(price, ai, stake_ton=stake)
        sl = 0.0 if Config.ONLY_PROFIT_EXIT else self.exchange._round(price * (1 - sl_pct / 100))
        tp = self.exchange._round(price * (1 + tp_pct / 100))

        # Константы для карточки «ожидают продажи» — рассчитываем один раз при открытии
        fee       = Config.FEE_PCT / 100.0
        sell_gas  = Config.SELL_GAS_TON
        buy_gas   = getattr(Config, "BUY_GAS_TON", 0.25)
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
            "trail_pct":       Config.TRAILING_STOP_PCT,
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
        self.stats["total_trades"] += 1
        # Если уже есть другие LONG-позиции — объединяем всё в одну
        self._merge_long_trades()
        # АВТО-СОХРАНЕНИЕ: цена покупки + цель продажи на диск, чтобы после
        # перезапуска бот знал почём купил и не продал дешевле.
        try:
            self.exp.save_open_trades(self.open_trades)
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

        # Исполняем продажу GRINCH → TON
        order = self.exchange.place_order("sell", target_grinch)
        if not order or order.get("error"):
            err = (order or {}).get("error", "нет ответа")
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
        self.stats["total_trades"] += 1

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

        self.open_short_trades = [t for t in self.open_short_trades if t["id"] != trade_id]
        self.stats["total_pnl"] = round(self.stats["total_pnl"] + pnl_ton, 6)
        if pnl_ton > 0:
            self.stats["winning_trades"] += 1

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
        except Exception:
            pass
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
                trail_pct = Config.TRAILING_STOP_PCT   # начальный (7%)

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
            db_store.open_trades_save(self.open_trades)
        except Exception:
            pass
        try:
            self.exp.save_open_trades(self.open_trades)
        except Exception:
            pass
        return {"ok": True}

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
                self.log(
                    f"💸 Продаём {grinch_amount:.6f} GRINCH на DeDust "
                    f"(причина: {reason})...", "INFO"
                )
                sell_ok = False
                try:
                    sell_result = self.exchange.place_order("sell", grinch_amount)
                    if sell_result and not sell_result.get("error"):
                        sell_ok = True
                        self.log(
                            f"✅ Продажа GRINCH → TON исполнена | "
                            f"id={sell_result.get('id', '—')}", "INFO"
                        )
                    else:
                        err = sell_result.get("error", "нет ответа") if sell_result else "нет ответа"
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
            self.exp.save_open_trades(self.open_trades)   # позиция закрыта → обновляем диск
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
        buy_gas  = getattr(Config, "BUY_GAS_TON", 0.25)
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
        ohlcv    = self.exchange.get_ohlcv(limit=100)
        analysis = analyze(ohlcv)
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
        if self.stats["total_trades"] > 0:
            winrate = round(self.stats["winning_trades"] / self.stats["total_trades"] * 100, 1)
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
            getattr(Config, "AI_FULL_RIGHTS", True) and
            _ai_conf >= getattr(Config, "AI_FULL_RIGHTS_MIN_CONF", 68.0)
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
            "ai_full_rights":        getattr(Config, "AI_FULL_RIGHTS", True),
            "ai_full_rights_min_conf": getattr(Config, "AI_FULL_RIGHTS_MIN_CONF", 68.0),
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
            } if Config.DCA_MODE else None,
        }
