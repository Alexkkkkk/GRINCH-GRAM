import ccxt
import random
import time
from config import Config
from datetime import datetime, timedelta

# Базовые ориентировочные цены монет для демо-режима (USDT)
BASE_PRICES = {
    "GRINCH": 0.025,
    "BTC": 67000.0,
    "ETH": 3500.0,
    "TON": 5.5,
    "SOL": 150.0,
    "BNB": 600.0,
    "XRP": 0.52,
    "DOGE": 0.16,
    "ADA": 0.45,
    "AVAX": 35.0,
    "MATIC": 0.72,
}
DEFAULT_BASE_PRICE = 100.0


class ExchangeClient:
    def __init__(self):
        self.demo_mode = Config.DEMO_MODE
        self._exchange = None
        self._live_price = None
        self._live_symbol = None
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

    @property
    def symbol(self):
        # Читаем динамически — пара может меняться через настройки
        return Config.SYMBOL

    @property
    def base_currency(self):
        return self.symbol.split("/")[0].upper()

    def _base_price(self):
        return BASE_PRICES.get(self.base_currency, DEFAULT_BASE_PRICE)

    def get_live_price(self):
        """Живая цена в реальном времени (плавный random walk вокруг базовой)."""
        if not self.demo_mode:
            try:
                return self.get_ticker()["price"]
            except Exception:
                pass
        bp = self._base_price()
        # Сброс при смене пары или первом запуске
        if self._live_price is None or self._live_symbol != self.symbol:
            self._live_price = bp
            self._live_symbol = self.symbol
        # Небольшой случайный шаг + лёгкий возврат к базовой цене
        step = self._live_price * random.uniform(-0.0035, 0.0035)
        pull = (bp - self._live_price) * 0.02
        self._live_price = max(self._live_price + step + pull, bp * 0.3)
        return self._round(self._live_price)

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
            base = self.base_currency
            holding = round(500.0 / self._base_price(), 6)
            return {"USDT": 10000.0, base: holding}
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

    def _round(self, p):
        # Меньше цена — больше знаков после запятой
        bp = self._base_price()
        digits = 2 if bp >= 100 else (4 if bp >= 1 else 6)
        return round(p, digits)

    def _fake_ticker(self):
        bp = self._base_price()
        base = bp + random.uniform(-bp * 0.008, bp * 0.008)
        spread = bp * 0.0002
        return {
            "price": self._round(base),
            "bid": self._round(base - spread),
            "ask": self._round(base + spread),
            "volume": round(random.uniform(1000, 5000), 2),
        }

    def _fake_ohlcv(self, limit=100):
        bars = []
        now = int(time.time() * 1000)
        interval = 3600 * 1000
        bp = self._base_price()
        price = bp
        vol = bp * 0.005  # масштаб волатильности относительно цены
        for i in range(limit):
            ts = now - (limit - i) * interval
            o = price
            h = o + random.uniform(0, vol)
            l = o - random.uniform(0, vol)
            c = l + random.uniform(0, h - l)
            v = random.uniform(100, 500)
            bars.append([ts, self._round(o), self._round(h), self._round(l), self._round(c), round(v, 2)])
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
