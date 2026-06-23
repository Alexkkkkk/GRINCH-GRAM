import threading
import time
from datetime import datetime
from config import Config
from exchange import ExchangeClient
from strategy import analyze

class Trader:
    def __init__(self):
        self.exchange = ExchangeClient()
        self.running = False
        self.trades = []
        self.open_trades = []
        self.logs = []
        self.stats = {
            "total_trades": 0,
            "winning_trades": 0,
            "total_pnl": 0.0,
            "start_balance": 10000.0,
        }
        self._thread = None

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
        ohlcv = self.exchange.get_ohlcv(limit=60)
        result = analyze(ohlcv)
        signal = result["signal"]
        price = result["price"]

        self.log(
            f"Анализ: цена={price} | RSI={result['rsi']} | "
            f"MACD={result['macd']} | Сигнал={signal} ({result['strength']}%)"
        )

        self._check_stop_loss_take_profit(price)

        if signal == "BUY" and len(self.open_trades) < Config.MAX_OPEN_TRADES:
            self._open_trade("buy", price, result)
        elif signal == "SELL" and self.open_trades:
            self._close_all_trades(price, result)

    def _open_trade(self, side, price, analysis):
        amount = Config.TRADE_AMOUNT / price
        order = self.exchange.place_order(side, amount)
        if not order:
            return

        sl = round(price * (1 - Config.STOP_LOSS_PCT / 100), 2)
        tp = round(price * (1 + Config.TAKE_PROFIT_PCT / 100), 2)

        trade = {
            "id": order["id"],
            "side": side,
            "entry_price": price,
            "amount": round(amount, 6),
            "stop_loss": sl,
            "take_profit": tp,
            "opened_at": datetime.utcnow().isoformat(),
            "pnl": 0.0,
            "status": "open",
        }
        self.open_trades.append(trade)
        self.trades.append(dict(trade))
        self.stats["total_trades"] += 1
        self.log(f"Открыта сделка BUY @ {price} | SL={sl} | TP={tp}", "BUY")

    def _close_all_trades(self, price, analysis):
        for trade in list(self.open_trades):
            self._close_trade(trade, price, "signal")

    def _check_stop_loss_take_profit(self, price):
        for trade in list(self.open_trades):
            if price <= trade["stop_loss"]:
                self._close_trade(trade, price, "stop_loss")
            elif price >= trade["take_profit"]:
                self._close_trade(trade, price, "take_profit")

    def _close_trade(self, trade, price, reason):
        pnl = round((price - trade["entry_price"]) * trade["amount"], 2)
        trade["pnl"] = pnl
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
        self.log(f"Закрыта сделка @ {price} | PNL={pnl} USDT | Причина: {reason}", level)

    def get_status(self):
        ohlcv = self.exchange.get_ohlcv(limit=60)
        analysis = analyze(ohlcv)
        balance = self.exchange.get_balance()
        winrate = 0
        if self.stats["total_trades"] > 0:
            winrate = round(self.stats["winning_trades"] / self.stats["total_trades"] * 100, 1)
        return {
            "running": self.running,
            "demo_mode": self.exchange.demo_mode,
            "symbol": Config.SYMBOL,
            "balance": balance,
            "analysis": analysis,
            "open_trades": self.open_trades,
            "recent_trades": self.trades[-20:],
            "logs": self.logs[-50:],
            "stats": {**self.stats, "winrate": winrate},
        }
