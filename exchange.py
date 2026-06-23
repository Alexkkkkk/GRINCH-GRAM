import ccxt
import random
import time
from config import Config
from datetime import datetime, timedelta

class ExchangeClient:
    def __init__(self):
        self.demo_mode = Config.DEMO_MODE
        self.symbol = Config.SYMBOL
        self._exchange = None
        if not self.demo_mode and Config.API_KEY:
            try:
                exchange_class = getattr(ccxt, Config.EXCHANGE)
                self._exchange = exchange_class({
                    "apiKey": Config.API_KEY,
                    "secret": Config.API_SECRET,
                    "enableRateLimit": True,
                })
            except Exception as e:
                print(f"[Exchange] Ошибка подключения: {e}. Переходим в демо-режим.")
                self.demo_mode = True

    def get_ticker(self):
        if self.demo_mode:
            return self._fake_ticker()
        try:
            t = self._exchange.fetch_ticker(self.symbol)
            return {"price": t["last"], "bid": t["bid"], "ask": t["ask"], "volume": t["baseVolume"]}
        except Exception as e:
            print(f"[Exchange] get_ticker error: {e}")
            return self._fake_ticker()

    def get_ohlcv(self, timeframe=None, limit=100):
        if self.demo_mode:
            return self._fake_ohlcv(limit)
        try:
            tf = timeframe or Config.TIMEFRAME
            bars = self._exchange.fetch_ohlcv(self.symbol, tf, limit=limit)
            return bars
        except Exception as e:
            print(f"[Exchange] get_ohlcv error: {e}")
            return self._fake_ohlcv(limit)

    def get_balance(self):
        if self.demo_mode:
            return {"USDT": 10000.0, "BTC": 0.05}
        try:
            bal = self._exchange.fetch_balance()
            return {k: v["free"] for k, v in bal["total"].items() if v > 0}
        except Exception as e:
            print(f"[Exchange] get_balance error: {e}")
            return {"USDT": 0.0}

    def place_order(self, side, amount, price=None):
        if self.demo_mode:
            return self._fake_order(side, amount, price)
        try:
            if price:
                order = self._exchange.create_limit_order(self.symbol, side, amount, price)
            else:
                order = self._exchange.create_market_order(self.symbol, side, amount)
            return order
        except Exception as e:
            print(f"[Exchange] place_order error: {e}")
            return None

    def _fake_ticker(self):
        base = 67000 + random.uniform(-500, 500)
        return {
            "price": round(base, 2),
            "bid": round(base - 10, 2),
            "ask": round(base + 10, 2),
            "volume": round(random.uniform(1000, 5000), 2),
        }

    def _fake_ohlcv(self, limit=100):
        bars = []
        now = int(time.time() * 1000)
        interval = 3600 * 1000
        price = 67000.0
        for i in range(limit):
            ts = now - (limit - i) * interval
            o = price
            h = o + random.uniform(0, 300)
            l = o - random.uniform(0, 300)
            c = l + random.uniform(0, h - l)
            v = random.uniform(100, 500)
            bars.append([ts, round(o, 2), round(h, 2), round(l, 2), round(c, 2), round(v, 2)])
            price = c
        return bars

    def _fake_order(self, side, amount, price=None):
        ticker = self._fake_ticker()
        fill_price = price or ticker["price"]
        return {
            "id": f"demo_{int(time.time())}",
            "side": side,
            "amount": amount,
            "price": fill_price,
            "status": "closed",
            "datetime": datetime.utcnow().isoformat(),
        }
