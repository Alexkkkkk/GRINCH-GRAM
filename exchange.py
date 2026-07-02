import json
import os
import random
import threading
import time
import urllib.request
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
                import ccxt
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

    # Кэш реальных свечей GeckoTerminal: ключ (currency, token, tf, aggregate) ->
    #   {"ts": время успеха, "bars": свечи, "fail_ts": время последней ошибки}
    _ohlcv_cache = {}
    _ohlcv_lock     = threading.Lock()
    _OHLCV_TTL      = 60   # сек — обновлять свечи раз в минуту (быстрый сигнал)
    _OHLCV_BACKOFF  = 45   # сек — после ошибки ждём меньше, чтобы не пропустить сигнал

    def get_real_ohlcv(self, limit=100, currency="usd", token="base", tf="hour", aggregate=1):
        """Реальные свечи пула GRINCH/GRAM (Toncoin) с GeckoTerminal.
        currency="usd" — цена в USD; currency="token", token="base" — цена GRINCH в GRAM (TON).
        tf="minute"+aggregate=15 — 15-минутные свечи (как на DeDust); tf="hour"+aggregate=1 — часовые.
        Возвращает [[ts_ms, o, h, l, c, v], ...] от старых к новым, либо None при ошибке.
        Кэширует успех на _OHLCV_TTL и при ошибке отдаёт устаревшие данные (backoff),
        чтобы не упереться в rate limit бесплатного GeckoTerminal."""
        pool = getattr(Config, "GRINCH_POOL_ADDRESS", None)
        if not pool:
            return None
        key = (currency, token, tf, aggregate)

        # Быстрый путь без блокировки: свежий кэш в памяти
        entry = ExchangeClient._ohlcv_cache.get(key)
        if entry and entry.get("bars") and (time.time() - entry.get("ts", 0)) < self._OHLCV_TTL:
            return entry["bars"][-limit:]

        # Медленный путь: одна загрузка за раз (singleflight) под блокировкой
        with ExchangeClient._ohlcv_lock:
            now   = time.time()
            entry = ExchangeClient._ohlcv_cache.get(key, {})
            # После перезапуска память пуста — пробуем дисковый кэш
            if not entry.get("bars"):
                disk = self._load_disk_ohlcv(key)
                if disk:
                    entry = disk
                    ExchangeClient._ohlcv_cache[key] = entry
            bars = entry.get("bars")

            # Другой поток мог уже обновить кэш, пока мы ждали блокировку
            if bars and (now - entry.get("ts", 0)) < self._OHLCV_TTL:
                return bars[-limit:]
            # Недавняя ошибка — не штурмуем API, отдаём устаревшие данные (если есть)
            if (now - entry.get("fail_ts", 0)) < self._OHLCV_BACKOFF:
                return bars[-limit:] if bars else None

            try:
                url = (
                    f"https://api.geckoterminal.com/api/v2/networks/ton/pools/{pool}"
                    f"/ohlcv/{tf}?aggregate={aggregate}&limit={max(limit, 100)}"
                    f"&currency={currency}&token={token}"
                )
                req = urllib.request.Request(url, headers={
                    "Accept": "application/json",
                    # GeckoTerminal/Cloudflare блокирует дефолтный UA urllib (403)
                    "User-Agent": "Mozilla/5.0 (compatible; GrinchGram/1.0)",
                })
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read())
                raw = data["data"]["attributes"]["ohlcv_list"]  # newest-first, ts в секундах
                fresh = [
                    [int(ts) * 1000, float(o), float(h), float(l), float(c), float(v)]
                    for ts, o, h, l, c, v in reversed(raw)
                ]
                if not fresh:
                    return bars[-limit:] if bars else None
                new_entry = {"ts": now, "bars": fresh, "fail_ts": 0}
                ExchangeClient._ohlcv_cache[key] = new_entry
                self._save_disk_ohlcv(key, new_entry)
                return fresh[-limit:]
            except Exception as e:
                print(f"[Exchange] get_real_ohlcv error: {e}")
                entry["fail_ts"] = now
                ExchangeClient._ohlcv_cache[key] = entry
                return bars[-limit:] if bars else None

    @staticmethod
    def _disk_ohlcv_path(key):
        return "/tmp/grinch_ohlcv_" + "_".join(str(k) for k in key) + ".json"

    def _load_disk_ohlcv(self, key):
        try:
            path = self._disk_ohlcv_path(key)
            if not os.path.exists(path):
                return None
            with open(path) as f:
                d = json.load(f)
            if d.get("bars"):
                return {"ts": d.get("ts", 0), "bars": d["bars"], "fail_ts": 0}
        except Exception:
            pass
        return None

    def _save_disk_ohlcv(self, key, entry):
        try:
            path = self._disk_ohlcv_path(key)
            tmp  = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump({"ts": entry["ts"], "bars": entry["bars"]}, f)
            os.replace(tmp, path)   # атомарная замена — без частичных записей
        except Exception:
            pass

    def get_ohlcv(self, timeframe=None, limit=100):
        # DeDust не предоставляет OHLCV — для торгового движка используем симуляцию
        # (реальные свечи GeckoTerminal идут только в график через get_real_ohlcv,
        #  чтобы не упираться в rate limit бесплатного API).
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
