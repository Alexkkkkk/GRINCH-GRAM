import ccxt
import random
import time
from config import Config
from datetime import datetime
from price_feed import price_feed

# Базовые ориентировочные цены экосистемы TON для демо-режима (USDT).
BASE_PRICES = {
    "GRINCH": 0.00027,
    "TON":    1.55,
}
DEFAULT_BASE_PRICE = 1.0


class ExchangeClient:
    def __init__(self):
        self.demo_mode = Config.DEMO_MODE
        self._exchange    = None
        self._live_price  = None
        self._live_symbol = None
        self._dedust      = None

        # ── Режим DeDust (реальный DEX на TON) ──────────────────────────
        if Config.TRADE_MODE == "dedust":
            from dedust_client import dedust_client
            self._dedust   = dedust_client
            self.demo_mode = False
            if not dedust_client.ready:
                print(f"[Exchange] DeDust недоступен: {dedust_client.error}. Переходим в демо-режим.")
                self._dedust   = None
                self.demo_mode = True
            else:
                print("[Exchange] DeDust-режим активен ✓")
            return

        # ── Режим реального CEX через CCXT ──────────────────────────────
        if not self.demo_mode and Config.API_KEY:
            try:
                exchange_class = getattr(ccxt, Config.EXCHANGE)
                self._exchange = exchange_class({
                    "apiKey":         Config.API_KEY,
                    "secret":         Config.API_SECRET,
                    "enableRateLimit": True,
                })
            except Exception as e:
                print(f"[Exchange] Ошибка подключения: {e}. Переходим в демо-режим.")
                self.demo_mode = True

    @property
    def mode(self) -> str:
        if self._dedust:
            return "dedust"
        if self._exchange:
            return "cex"
        return "demo"

    @property
    def symbol(self):
        return Config.SYMBOL

    @property
    def base_currency(self):
        return self.symbol.split("/")[0].upper()

    # ──────────────────────────── price helpers ──────────────────────────

    def _base_price(self):
        # DexScreener/CoinGecko — быстрый кэшированный источник
        real = price_feed.get(self.base_currency)
        if real and real > 0:
            return real
        return BASE_PRICES.get(self.base_currency, DEFAULT_BASE_PRICE)

    def _round(self, p):
        bp = self._base_price()
        if   bp >= 100:  digits = 2
        elif bp >= 1:    digits = 4
        elif bp >= 0.01: digits = 6
        else:            digits = 8
        return round(p, digits)

    # ──────────────────────────── public API ────────────────────────────

    def get_live_price(self):
        """Текущая цена актива (реальная или симулированная)."""
        # DeDust-режим: цена из DexScreener (быстро), DeDust-пул только для ордеров
        if self._dedust:
            p = self._base_price()
            if p and p > 0:
                return self._round(p)

        # CEX через CCXT
        if not self.demo_mode and self._exchange:
            try:
                return self.get_ticker()["price"]
            except Exception:
                pass

        # Демо: плавный random-walk вокруг реальной цены
        bp = self._base_price()
        if self._live_price is None or self._live_symbol != self.symbol:
            self._live_price  = bp
            self._live_symbol = self.symbol
        step = self._live_price * random.uniform(-0.0035, 0.0035)
        pull = (bp - self._live_price) * 0.02
        self._live_price = max(self._live_price + step + pull, bp * 0.3)
        return self._round(self._live_price)

    def get_ticker(self):
        if self._dedust:
            p = self.get_live_price()
            sp = p * 0.0002
            return {
                "price":  p,
                "bid":    self._round(p - sp),
                "ask":    self._round(p + sp),
                "volume": 0.0,
            }
        if self.demo_mode:
            return self._fake_ticker()
        try:
            t = self._exchange.fetch_ticker(self.symbol)
            return {"price": t["last"], "bid": t["bid"], "ask": t["ask"], "volume": t["baseVolume"]}
        except Exception as e:
            print(f"[Exchange] get_ticker error: {e}")
            return self._fake_ticker()

    def get_ohlcv(self, timeframe=None, limit=100):
        # DeDust не предоставляет OHLCV — используем симуляцию с реальной ценой
        if self._dedust:
            return self._fake_ohlcv(limit)
        if self.demo_mode:
            return self._fake_ohlcv(limit)
        try:
            tf   = timeframe or Config.TIMEFRAME
            bars = self._exchange.fetch_ohlcv(self.symbol, tf, limit=limit)
            return bars
        except Exception as e:
            print(f"[Exchange] get_ohlcv error: {e}")
            return self._fake_ohlcv(limit)

    def get_balance(self):
        if self._dedust:
            try:
                return self._dedust.get_balance()
            except Exception as e:
                print(f"[Exchange] dedust balance error: {e}")
                return {"TON": 0.0, "GRINCH": 0.0}
        if self.demo_mode:
            base    = self.base_currency
            holding = round(500.0 / self._base_price(), 6)
            return {"USDT": 10000.0, base: holding}
        try:
            bal = self._exchange.fetch_balance()
            return {k: v["free"] for k, v in bal["total"].items() if v > 0}
        except Exception as e:
            print(f"[Exchange] get_balance error: {e}")
            return {"USDT": 0.0}

    def place_order(self, side, amount, price=None, ton_stake=None):
        """
        side: "buy" | "sell"
        amount: количество базового актива (GRINCH)
        ton_stake: для DeDust-режима — сколько TON тратим на покупку (опционально)
        """
        if self._dedust:
            return self._dedust_order(side, amount, price, ton_stake=ton_stake)
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

    # ──────────────────────────── DeDust order ──────────────────────────

    def _dedust_order(self, side, amount, price=None, ton_stake=None):
        """Реальный своп через DeDust DEX."""
        fill_price = price or self.get_live_price()
        try:
            if side == "buy":
                # ton_stake передаётся из trader напрямую (TON), иначе конвертируем
                ton_amount = ton_stake if ton_stake is not None else amount * fill_price
                result = self._dedust.buy(ton_amount)
            else:
                result = self._dedust.sell(amount)

            if not result.get("ok"):
                print(f"[DeDust] Ошибка ордера: {result.get('error')}")
                return None

            return {
                "id":       f"dedust_{int(time.time())}",
                "side":     side,
                "amount":   amount,
                "price":    fill_price,
                "status":   "closed",
                "datetime": datetime.utcnow().isoformat(),
                "info":     result,
            }
        except Exception as e:
            print(f"[Exchange] _dedust_order error: {e}")
            return None

    # ──────────────────────────── demo helpers ──────────────────────────

    def _fake_ticker(self):
        bp     = self._base_price()
        base   = bp + random.uniform(-bp * 0.008, bp * 0.008)
        spread = bp * 0.0002
        return {
            "price":  self._round(base),
            "bid":    self._round(base - spread),
            "ask":    self._round(base + spread),
            "volume": round(random.uniform(1000, 5000), 2),
        }

    def _fake_ohlcv(self, limit=100):
        bars     = []
        now      = int(time.time() * 1000)
        interval = 3600 * 1000
        bp       = self._base_price()
        price    = bp
        vol      = bp * 0.005
        for i in range(limit):
            ts = now - (limit - i) * interval
            o  = price
            h  = o + random.uniform(0, vol)
            l  = o - random.uniform(0, vol)
            c  = l + random.uniform(0, h - l)
            v  = random.uniform(100, 500)
            bars.append([ts, self._round(o), self._round(h), self._round(l), self._round(c), round(v, 2)])
            price = c
        return bars

    def _fake_order(self, side, amount, price=None):
        ticker     = self._fake_ticker()
        fill_price = price or ticker["price"]
        return {
            "id":       f"demo_{int(time.time())}",
            "side":     side,
            "amount":   amount,
            "price":    fill_price,
            "status":   "closed",
            "datetime": datetime.utcnow().isoformat(),
        }
