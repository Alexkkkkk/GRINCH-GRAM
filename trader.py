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

        self._check_stop_loss_take_profit(price)

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
            self._open_trade("buy", price, result, ai)
            self._buy_confirm_count = 0   # сбрасываем после входа
            for cb in self.signal_callbacks:
                try:   cb("BUY", price, ai)
                except Exception as e: self.log(f"Signal cb ошибка: {e}", "WARN")
        elif final_signal == "SELL" and self.open_trades:
            self._close_all_trades(price, result)
            for cb in self.signal_callbacks:
                try:   cb("SELL", price, ai)
                except Exception as e: self.log(f"Signal cb ошибка: {e}", "WARN")

    # ──────────────────────────────────────────
    # Торговые операции
    # ──────────────────────────────────────────
    def _targets(self, price, ai):
        atr_pct = (ai.get("regime", {}) or {}).get("atr_pct", 0) / 100.0 if ai else 0.0
        if Config.USE_DYNAMIC_TARGETS and atr_pct > 0:
            sl_pct = max(atr_pct * Config.ATR_SL_MULT * 100, Config.STOP_LOSS_PCT)
            tp_pct = max(atr_pct * Config.ATR_TP_MULT * 100, Config.TAKE_PROFIT_PCT)
        else:
            sl_pct, tp_pct = Config.STOP_LOSS_PCT, Config.TAKE_PROFIT_PCT
        # TP должен перекрывать: DeDust buy fee + sell fee + запас 0.5%
        min_tp = Config.FEE_PCT * 2 + 0.5
        tp_pct = max(tp_pct, min_tp)
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

        order = self.exchange.place_order(side, amount, ton_stake=ton_stake)
        if not order:
            self.log("⚠️ BUY ордер не исполнен — пропускаем", "WARN")
            return

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

    def _close_all_trades(self, price, analysis):
        for trade in list(self.open_trades):
            if trade.get("symbol", Config.SYMBOL) != Config.SYMBOL:
                continue
            self._close_trade(trade, price, "signal")

    def _check_stop_loss_take_profit(self, price):
        for trade in list(self.open_trades):
            if trade.get("symbol", Config.SYMBOL) != Config.SYMBOL:
                continue

            # Трейлинг-стоп: поднимаем стоп за ценой
            trail = trade.get("trail_pct", 0)
            if trail and price > trade.get("high_water", trade["entry_price"]):
                trade["high_water"] = price
                new_sl = self.exchange._round(price * (1 - trail / 100))
                if new_sl > trade["stop_loss"] and new_sl > trade["entry_price"]:
                    trade["stop_loss"] = new_sl
                    self.log(f"🔼 Трейлинг-стоп → {new_sl}", "INFO")

            if price <= trade["stop_loss"]:
                reason = "trailing_stop" if trade["stop_loss"] > trade["entry_price"] else "stop_loss"
                self._close_trade(trade, price, reason)
            elif price >= trade["take_profit"]:
                self._close_trade(trade, price, "take_profit")

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
                try:
                    sell_result = self.exchange.place_order("sell", grinch_amount)
                    if sell_result and not sell_result.get("error"):
                        self.log(
                            f"✅ Продажа GRINCH → TON исполнена | "
                            f"id={sell_result.get('id', '—')}", "INFO"
                        )
                    else:
                        err = sell_result.get("error", "нет ответа") if sell_result else "нет ответа"
                        self.log(f"⚠️ Продажа не исполнена: {err}", "WARN")
                except Exception as e:
                    self.log(f"⚠️ Ошибка продажи GRINCH: {e}", "WARN")

        # ── 2. Виртуальный P&L ───────────────────────────────────────────
        gross = (price - trade["entry_price"]) * trade["amount"]
        # Обе стороны DeDust: 0.3% вход + 0.3% выход
        fee   = (trade["entry_price"] + price) * trade["amount"] * Config.FEE_PCT / 100
        pnl   = round(gross - fee, 6)

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

    # ──────────────────────────────────────────
    # Статус
    # ──────────────────────────────────────────
    def get_status(self):
        ohlcv    = self.exchange.get_ohlcv(limit=100)
        analysis = analyze(ohlcv)
        ai       = self.last_ai if self.last_ai else self.ai.analyze(ohlcv)
        balance  = self.exchange.get_balance()
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
