import threading
import time
from datetime import datetime
from config import Config
from exchange import ExchangeClient
from strategy import analyze
from ai_engine import AIEngine


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
        # Счётчик подтверждений BUY-сигнала (требуем 2 последовательных)
        self._buy_confirm_count = 0
        # Кеш баланса: не долбим блокчейн при каждом /api/status (TTL 30 сек)
        self._balance_cache     = {}
        self._balance_cache_ts  = 0
        self._balance_cache_ttl = 30  # секунд

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

        while self.running:
            try:
                self._tick()
            except Exception as e:
                self.log(f"Ошибка в цикле: {e}", "ERROR")
            time.sleep(30)

    def _emit_progress(self, progress_dict):
        if self.on_training_progress:
            try:
                self.on_training_progress(progress_dict)
            except Exception:
                pass

    # ──────────────────────────────────────────
    # Торговый тик
    # ──────────────────────────────────────────
    def _tick(self):
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

        # Если SL/TP закрыл все позиции и разослал SELL — завершаем тик,
        # чтобы в этом же тике не открыть BUY поверх ещё не сведённого
        # пользовательского состояния (гонка SELL→BUY в одном окне).
        if self._check_stop_loss_take_profit(price):
            return

        # ── Формируем итоговый сигнал ──────────────────────────────────
        final_signal = "HOLD"
        if signal == ai_signal and signal != "HOLD":
            final_signal = signal
        elif ai_signal != "HOLD" and conf >= Config.AI_OVERRIDE_CONFIDENCE:
            final_signal = ai_signal

        if anomaly:
            self.log(f"⚠️ АНОМАЛИЯ! Z-цена={ai['anomaly']['z_price']}", "WARN")

        # ── Счётчик подтверждений (требуем 2 последовательных BUY) ────
        if final_signal == "BUY":
            self._buy_confirm_count += 1
        else:
            self._buy_confirm_count = 0

        # ── Фильтры входа ──────────────────────────────────────────────
        blocked = None
        if final_signal == "BUY":
            hard_override     = conf >= Config.AI_HARD_OVERRIDE_CONFIDENCE
            mean_rev_override = (
                rsi <= Config.RSI_OVERSOLD_REVERSAL and
                conf >= Config.REVERSAL_AI_MIN
            )

            if conf < Config.MIN_AI_CONFIDENCE:
                blocked = f"низкая уверенность AI {conf}%"
            elif mean_rev_override:
                # RSI экстремально низкий + AI уверен → Mean Reversion вход даже в DOWNTREND
                self.log(
                    f"📈 Mean Reversion Override: RSI={rsi:.1f} + AI={conf}% "
                    f"→ вход несмотря на {regime_name}", "INFO"
                )
            elif Config.TREND_FILTER and regime_name == "DOWNTREND":
                blocked = "нисходящий тренд"
            elif regime_name == "VOLATILE" and not hard_override:
                # VOLATILE = хаос, без сильного AI-сигнала не входим
                blocked = f"хаотичный рынок (VOLATILE)"
            elif hard_override:
                if anomaly:
                    self.log(
                        f"🔥 Hard Override: AI={conf}% → вход несмотря на "
                        f"аномалию Z={ai['anomaly']['z_price']}", "INFO"
                    )
            elif rsi >= Config.RSI_OVERBOUGHT:
                blocked = f"перекупленность RSI={rsi:.1f}"
            elif anomaly:
                blocked = f"рыночная аномалия Z={ai['anomaly']['z_price']:.2f}"
            elif self._buy_confirm_count < 2 and not hard_override:
                # Нужно 2 последовательных сигнала для подтверждения
                blocked = f"ожидаем подтверждение ({self._buy_confirm_count}/2)"

        self.log(
            f"📊 RSI={rsi:.1f} | {regime_name} | "
            f"Сигнал={signal} | AI={ai_signal}({conf}%) | "
            f"Итог={'HOLD' if blocked else final_signal}",
            level="INFO"
        )

        if final_signal == "BUY" and blocked:
            self.log(f"⏸️ Вход отменён: {blocked}", "WARN")
        elif final_signal == "BUY" and len(self.open_trades) < Config.MAX_OPEN_TRADES:
            opened = self._open_trade("buy", price, result, ai)
            # Сигнал пользователям шлём ТОЛЬКО если реальный ордер исполнился —
            # иначе у юзеров спишется виртуальный TON и откроется позиция без
            # реальной сделки (рассинхрон балансов, блокировка вывода).
            if opened:
                self._buy_confirm_count = 0   # сбрасываем после входа
                self._emit_signal("BUY", price, ai)
        elif final_signal == "SELL" and self.open_trades:
            closed = self._close_all_trades(price, result)
            # Сигнал SELL юзерам шлём ТОЛЬКО если реальная продажа прошла —
            # иначе у юзеров обнулится grinch_held и разблокируется вывод TON,
            # которого на самом деле нет (реальный GRINCH не продан).
            if closed:
                self._emit_signal("SELL", price, ai)

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
    def _targets(self, price, ai):
        atr_pct = (ai.get("regime", {}) or {}).get("atr_pct", 0) / 100.0 if ai else 0.0
        if Config.USE_DYNAMIC_TARGETS and atr_pct > 0:
            sl_pct = max(atr_pct * Config.ATR_SL_MULT * 100, Config.STOP_LOSS_PCT)
            # ATR × 8 используется только если ОН ВЫШЕ минимального нетто-таргета
            tp_pct = max(atr_pct * Config.ATR_TP_MULT * 100, Config.TAKE_PROFIT_PCT)
        else:
            sl_pct, tp_pct = Config.STOP_LOSS_PCT, Config.TAKE_PROFIT_PCT

        # Жёсткий минимум TP = нетто 50% + обе стороны комиссии DeDust (0.6%)
        # Никогда не закрываемся дешевле чем на +50% нетто
        min_gross_tp = Config.TARGET_NET_PCT + Config.FEE_ROUND_TRIP
        tp_pct = max(tp_pct, min_gross_tp)
        return sl_pct, tp_pct

    def _open_trade(self, side, price, analysis, ai=None):
        ai_conf = ai.get("confidence", 0) if ai else 0

        # Ставка пропорциональна уверенности AI: 50%→100% капитала
        conf_factor = 0.5 + min(max((ai_conf - 50) / 50.0, 0.0), 1.0) * 0.5
        stake  = Config.TRADE_AMOUNT * conf_factor

        ton_stake = None
        if self.exchange.mode == "dedust":
            ton_stake = stake
            amount    = stake / price
        else:
            amount = stake / price

        # ── Опрос баланса перед сделкой ──────────────────────────────────
        # Перед каждой реальной покупкой запрашиваем баланс кошелька. Если TON
        # недостаточно (ставка + газ свопа) — сделку НЕ открываем вовсе, не
        # дёргаем биржу впустую. Если баланс не удалось прочитать — тоже не
        # торгуем (fail-closed: не торгуем вслепую).
        if self.exchange.mode == "dedust" and side == "buy":
            bal     = self.exchange.get_balance() or {}
            ton_bal = bal.get("TON", 0) or 0
            needed  = stake + 0.45   # газ свопа (0.4 TON) + запас на комиссии сети
            if bal.get("error") or ton_bal < needed:
                why = bal.get("error") or f"на кошельке {ton_bal:.3f} TON"
                self.log(
                    f"⛔ Недостаточно средств для BUY: {why}, "
                    f"нужно ≥ {needed:.2f} TON (ставка {stake:.3f} + газ). Сделка отменена.",
                    "WARN"
                )
                return False

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

        sl_pct, tp_pct = self._targets(price, ai)
        sl = self.exchange._round(price * (1 - sl_pct / 100))
        tp = self.exchange._round(price * (1 + tp_pct / 100))

        trade = {
            "id":            order["id"],
            "symbol":        Config.SYMBOL,
            "side":          side,
            "entry_price":   price,
            "amount":        round(amount, 6),
            "stake_ton":     round(stake, 4),
            "stop_loss":     sl,
            "take_profit":   tp,
            "trail_pct":     Config.TRAILING_STOP_PCT,
            "high_water":    price,
            "opened_at":     datetime.utcnow().isoformat(),
            "pnl":           0.0,
            "status":        "open",
            "ai_confidence": ai_conf,
        }
        self.open_trades.append(trade)
        self.trades.append(dict(trade))
        self.stats["total_trades"] += 1
        self.log(
            f"🟢 BUY @ {price} | {stake:.3f} TON | SL={sl}(-{sl_pct:.1f}%) | "
            f"TP={tp}(+{tp_pct:.1f}%) | AI={ai_conf}%", "BUY"
        )
        return True

    def _close_all_trades(self, price, analysis):
        relevant_before = self._relevant_open()
        for trade in list(relevant_before):
            self._close_trade(trade, price, "signal")
        # Сигнал SELL юзерам безопасен ТОЛЬКО когда были позиции и ВСЕ они
        # реально закрылись. При частичном закрытии (одна продажа прошла, другая
        # нет) grinch_held обнулять нельзя — часть реального GRINCH ещё не продана.
        return bool(relevant_before) and not self._relevant_open()

    def _check_stop_loss_take_profit(self, price):
        had_relevant = bool(self._relevant_open())
        closed_any   = False
        for trade in list(self.open_trades):
            if trade.get("symbol", Config.SYMBOL) != Config.SYMBOL:
                continue

            entry      = trade["entry_price"]
            profit_pct = (price - entry) / entry * 100

            # ── Прогрессивный трейлинг-стоп ────────────────────────────────
            # Этапы активируются по мере роста прибыли (цель +50%):
            #   >45%   → трейл 2%  (фиксируем ≥43% нетто)
            #   >37.5% → трейл 4%
            #   >25%   → трейл 6%
            #   >12.5% → стоп в безубыток (не теряем деньги)
            if profit_pct >= Config.TRAIL_STAGE4_AT:
                trail_pct = Config.TRAIL_STAGE4_PCT
            elif profit_pct >= Config.TRAIL_STAGE3_AT:
                trail_pct = Config.TRAIL_STAGE3_PCT
            elif profit_pct >= Config.TRAIL_STAGE2_AT:
                trail_pct = Config.TRAIL_STAGE2_PCT
            else:
                trail_pct = Config.TRAILING_STOP_PCT   # начальный (7%)

            # Обновляем high_water и стоп
            if price > trade.get("high_water", entry):
                trade["high_water"] = price

            high_water = trade.get("high_water", entry)
            new_sl     = self.exchange._round(high_water * (1 - trail_pct / 100))

            # Безубыток: если прибыль > TRAIL_BREAKEVEN_AT → стоп не ниже цены входа
            if profit_pct >= Config.TRAIL_BREAKEVEN_AT:
                breakeven_sl = self.exchange._round(entry * 1.002)  # чуть выше входа (покрывает комиссию)
                new_sl = max(new_sl, breakeven_sl)

            # Поднимаем стоп только вверх (никогда не опускаем)
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

            # ── Проверка условий закрытия ───────────────────────────────────
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

    def _close_trade(self, trade, price, reason):
        """
        Закрывает позицию:
        1. Исполняет реальную продажу GRINCH на блокчейне (DeDust режим)
        2. Рассчитывает виртуальный P&L
        3. Обновляет статистику и AI feedback
        """
        # ── 1. РЕАЛЬНАЯ продажа GRINCH через DeDust ─────────────────────
        if self.exchange.mode == "dedust":
            grinch_amount = trade.get("amount", 0)
            if grinch_amount > 0:
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
        # Обе стороны DeDust: 0.3% вход + 0.3% выход
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

        # ── 3. AI feedback: самообучение ─────────────────────────────────
        try:
            outcome = "win" if pnl > 0 else "loss"
            self.ai.feedback(outcome=outcome, pnl=float(pnl))
            self.log(f"🧠 AI feedback: {outcome} PNL={pnl:+.6f} TON", "INFO")
        except Exception as e:
            self.log(f"AI feedback ошибка: {e}", "WARN")

        emoji = "🟩" if pnl >= 0 else "🟥"
        self.log(
            f"{emoji} Закрыто @ {price} | PNL={pnl:+.6f} TON | {reason}", 
            "SELL" if pnl >= 0 else "ERROR"
        )
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
        if bal and not bal.get("error"):
            self._balance_cache    = bal
            self._balance_cache_ts = now
        return bal

    def get_status(self):
        ohlcv    = self.exchange.get_ohlcv(limit=100)
        analysis = analyze(ohlcv)
        ai       = self.last_ai if self.last_ai else self.ai.analyze(ohlcv)
        balance  = self._get_balance_cached()
        winrate  = 0
        if self.stats["total_trades"] > 0:
            winrate = round(self.stats["winning_trades"] / self.stats["total_trades"] * 100, 1)
        return {
            "running":       self.running,
            "training":      self.training,
            "demo_mode":     self.exchange.demo_mode,
            "symbol":        Config.SYMBOL,
            "balance":       balance,
            "analysis":      analysis,
            "ai":            ai,
            "open_trades":   self.open_trades,
            "recent_trades": self.trades[-20:],
            "logs":          self.logs[-50:],
            "stats":         {**self.stats, "winrate": winrate},
            "training_progress": self.ai.training_progress,
        }
