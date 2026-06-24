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
        self.ai = AIEngine()
        self.running = False
        self.trades = []
        self.open_trades = []
        self.logs = []
        self.last_ai = {}
        self.stats = {
            "total_trades": 0,
            "winning_trades": 0,
            "total_pnl": 0.0,
            "start_balance": 10000.0,
        }
        self._thread = None
        # Колбэки для уведомления пользовательских трейдеров о сигналах
        self.signal_callbacks = []

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
        self.log("Торговый агент остановлен", "WARN")

    def _loop(self):
        while self.running:
            try:
                self._tick()
            except Exception as e:
                self.log(f"Ошибка в цикле: {e}", "ERROR")
            time.sleep(30)

    def _tick(self):
        ohlcv  = self.exchange.get_ohlcv(limit=100)
        result = analyze(ohlcv)
        ai     = self.ai.analyze(ohlcv)
        self.last_ai = ai

        signal     = result["signal"]
        ai_signal  = ai.get("ai_signal", "HOLD")
        price      = result["price"]
        conf       = ai.get("confidence", 0)
        rsi        = result.get("rsi", 50)
        regime     = ai.get("regime", {}) or {}
        regime_name = regime.get("name", "?")
        anomaly    = ai.get("anomaly", {}).get("detected", False)

        # Сначала сопровождаем открытые позиции (трейлинг-стоп, SL/TP)
        self._check_stop_loss_take_profit(price)

        # Ансамбль: стратегия + AI должны совпасть, либо AI с высокой уверенностью
        final_signal = "HOLD"
        if signal == ai_signal and signal != "HOLD":
            final_signal = signal
        elif ai_signal != "HOLD" and conf >= Config.AI_OVERRIDE_CONFIDENCE:
            final_signal = ai_signal

        if anomaly:
            self.log(f"⚠️ АНОМАЛИЯ обнаружена! Z-score цены={ai['anomaly']['z_price']}", "WARN")

        # Профит-фильтры качества входа (только для покупки)
        blocked = None
        if final_signal == "BUY":
            if Config.TREND_FILTER and regime_name == "DOWNTREND":
                blocked = "нисходящий тренд"
            elif rsi >= Config.RSI_OVERBOUGHT:
                blocked = f"перекупленность RSI={rsi}"
            elif conf < Config.MIN_AI_CONFIDENCE:
                blocked = f"низкая уверенность AI {conf}%"
            elif anomaly:
                blocked = "рыночная аномалия"

        self.log(
            f"📊 RSI={rsi} | {regime_name} | "
            f"Сигнал={signal} | AI={ai_signal}({conf}%) | Итог={'HOLD' if blocked else final_signal}",
            level="INFO"
        )

        if final_signal == "BUY" and blocked:
            self.log(f"⏸️ Вход отменён: {blocked}", "WARN")
        elif final_signal == "BUY" and len(self.open_trades) < Config.MAX_OPEN_TRADES:
            self._open_trade("buy", price, result, ai)
            for cb in self.signal_callbacks:
                try:
                    cb("BUY", price, ai)
                except Exception as e:
                    self.log(f"Signal cb ошибка: {e}", "WARN")
        elif final_signal == "SELL" and self.open_trades:
            self._close_all_trades(price, result)
            for cb in self.signal_callbacks:
                try:
                    cb("SELL", price, ai)
                except Exception as e:
                    self.log(f"Signal cb ошибка: {e}", "WARN")

    def _targets(self, price, ai):
        """Динамические стоп-лосс/тейк-профит по волатильности (ATR), с учётом комиссии."""
        atr_pct = (ai.get("regime", {}) or {}).get("atr_pct", 0) / 100.0 if ai else 0.0
        if Config.USE_DYNAMIC_TARGETS and atr_pct > 0:
            sl_pct = max(atr_pct * Config.ATR_SL_MULT * 100, Config.STOP_LOSS_PCT)
            tp_pct = max(atr_pct * Config.ATR_TP_MULT * 100, Config.TAKE_PROFIT_PCT)
        else:
            sl_pct, tp_pct = Config.STOP_LOSS_PCT, Config.TAKE_PROFIT_PCT
        # Тейк обязан перекрывать комиссию обоих сделок с запасом
        tp_pct = max(tp_pct, Config.FEE_PCT * 2 + 0.5)
        return sl_pct, tp_pct

    def _open_trade(self, side, price, analysis, ai=None):
        ai_conf = ai.get("confidence", 0) if ai else 0

        # Размер позиции масштабируется уверенностью AI (0.5×…1.0× от суммы)
        conf_factor = 0.5 + min(max((ai_conf - 50) / 50.0, 0.0), 1.0) * 0.5
        stake  = Config.TRADE_AMOUNT * conf_factor

        # В DeDust-режиме TRADE_AMOUNT = TON. Для записи переводим в кол-во GRINCH.
        ton_stake = None
        if self.exchange.mode == "dedust":
            ton_stake = stake                # сколько TON тратим
            amount    = stake / price        # ~кол-во GRINCH (приблизительно, для журнала)
        else:
            amount = stake / price

        order = self.exchange.place_order(side, amount, ton_stake=ton_stake)
        if not order:
            return

        sl_pct, tp_pct = self._targets(price, ai)
        sl = self.exchange._round(price * (1 - sl_pct / 100))
        tp = self.exchange._round(price * (1 + tp_pct / 100))

        trade = {
            "id": order["id"],
            "symbol": Config.SYMBOL,
            "side": side,
            "entry_price": price,
            "amount": round(amount, 6),
            "stop_loss": sl,
            "take_profit": tp,
            "trail_pct": Config.TRAILING_STOP_PCT,
            "high_water": price,
            "opened_at": datetime.utcnow().isoformat(),
            "pnl": 0.0,
            "status": "open",
            "ai_confidence": ai_conf,
        }
        self.open_trades.append(trade)
        self.trades.append(dict(trade))
        self.stats["total_trades"] += 1
        self.log(
            f"🟢 BUY @ {price} | {stake:.0f} USDT | SL={sl}(-{sl_pct:.1f}%) | "
            f"TP={tp}(+{tp_pct:.1f}%) | AI={ai_conf}%", "BUY"
        )

    def _close_all_trades(self, price, analysis):
        for trade in list(self.open_trades):
            if trade.get("symbol", Config.SYMBOL) != Config.SYMBOL:
                continue
            self._close_trade(trade, price, "signal")

    def _check_stop_loss_take_profit(self, price):
        # Оцениваем только позиции по текущей паре
        for trade in list(self.open_trades):
            if trade.get("symbol", Config.SYMBOL) != Config.SYMBOL:
                continue

            # Трейлинг-стоп: подтягиваем стоп вверх вслед за ценой, фиксируя прибыль
            trail = trade.get("trail_pct", 0)
            if trail and price > trade.get("high_water", trade["entry_price"]):
                trade["high_water"] = price
                new_sl = self.exchange._round(price * (1 - trail / 100))
                # Поднимаем стоп только если он выше текущего и уже в зоне прибыли
                if new_sl > trade["stop_loss"] and new_sl > trade["entry_price"]:
                    trade["stop_loss"] = new_sl
                    self.log(f"🔼 Трейлинг-стоп поднят до {new_sl}", "INFO")

            if price <= trade["stop_loss"]:
                reason = "trailing" if trade["stop_loss"] > trade["entry_price"] else "stop_loss"
                self._close_trade(trade, price, reason)
            elif price >= trade["take_profit"]:
                self._close_trade(trade, price, "take_profit")

    def _close_trade(self, trade, price, reason):
        gross = (price - trade["entry_price"]) * trade["amount"]
        # Комиссия берётся на входе и на выходе (FEE_PCT — за одну сделку)
        fee = (trade["entry_price"] + price) * trade["amount"] * Config.FEE_PCT / 100
        pnl = round(gross - fee, 2)
        trade["pnl"] = pnl
        trade["fee"] = round(fee, 2)
        trade["exit_price"] = price
        trade["closed_at"] = datetime.utcnow().isoformat()
        trade["close_reason"] = reason
        trade["status"] = "closed"

        self.stats["total_pnl"] = round(self.stats["total_pnl"] + pnl, 2)
        if pnl > 0:
            self.stats["winning_trades"] += 1

        self.open_trades = [t for t in self.open_trades if t["id"] != trade["id"]]
        for t in self.trades:
            if t["id"] == trade["id"]:
                t.update(trade)
                break

        level = "SELL" if pnl >= 0 else "ERROR"
        self.log(f"🔴 Закрыто @ {price} | PNL={pnl} USDT | {reason}", level)

    def get_status(self):
        ohlcv    = self.exchange.get_ohlcv(limit=100)
        analysis = analyze(ohlcv)
        ai       = self.last_ai if self.last_ai else self.ai.analyze(ohlcv)
        balance  = self.exchange.get_balance()
        winrate  = 0
        if self.stats["total_trades"] > 0:
            winrate = round(self.stats["winning_trades"] / self.stats["total_trades"] * 100, 1)
        return {
            "running":      self.running,
            "demo_mode":    self.exchange.demo_mode,
            "symbol":       Config.SYMBOL,
            "balance":      balance,
            "analysis":     analysis,
            "ai":           ai,
            "open_trades":  self.open_trades,
            "recent_trades": self.trades[-20:],
            "logs":         self.logs[-50:],
            "stats":        {**self.stats, "winrate": winrate},
        }
