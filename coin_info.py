import time
import threading
import requests
from config import Config
from price_feed import COINGECKO_IDS


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class CoinInfo:
    """Рыночная статистика монеты и лента последних сделок через бесплатные API
    (DexScreener / GeckoTerminal для GRINCH-джеттона, CoinGecko для крупных монет)."""

    def __init__(self, ttl_market=30, ttl_trades=15):
        self.ttl_market = ttl_market
        self.ttl_trades = ttl_trades
        self._lock = threading.Lock()
        self._market_cache = {}   # base -> (data, ts)
        self._trades_cache = {}   # base -> (data, ts)
        self._pool_cache = {}     # base -> (pool_addr, ts)

    # ---------------- Рыночная статистика ----------------
    def market(self, base):
        base = (base or "").upper()
        return self._cached(self._market_cache, base, self._fetch_market, self.ttl_market)

    def _fetch_market(self, base):
        cid = COINGECKO_IDS.get(base)
        if cid:
            return self._market_coingecko(cid)
        if base == "GRINCH":
            return self._market_dexscreener(Config.GRINCH_TOKEN_ADDRESS, base)
        return None

    def _market_coingecko(self, cid):
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "ids": cid},
                timeout=10,
            )
            r.raise_for_status()
            arr = r.json()
            if not arr:
                return None
            c = arr[0]
            return {
                "name": c.get("name"),
                "symbol": (c.get("symbol") or "").upper(),
                "image": c.get("image"),
                "price_usd": _f(c.get("current_price")),
                "change_h24": _f(c.get("price_change_percentage_24h")),
                "change_h1": None,
                "volume_h24": _f(c.get("total_volume")),
                "liquidity": None,
                "market_cap": _f(c.get("market_cap")),
                "fdv": _f(c.get("fully_diluted_valuation")),
                "buys_h24": None,
                "sells_h24": None,
                "url": None,
                "pool": None,
                "source": "CoinGecko",
            }
        except Exception:
            return None

    def _market_dexscreener(self, addr, base):
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                timeout=10,
            )
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
            # Берём только пары, где GRINCH — именно базовый токен (по адресу контракта)
            want = (addr or "").lower()
            grinch_pairs = [
                p for p in pairs
                if (p.get("baseToken", {}) or {}).get("address", "").lower() == want
            ]
            pairs = grinch_pairs or pairs
            if not pairs:
                return None
            pairs.sort(key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0), reverse=True)
            p = pairs[0]
            info = p.get("info", {}) or {}
            pc = p.get("priceChange", {}) or {}
            vol = p.get("volume", {}) or {}
            txns = (p.get("txns", {}) or {}).get("h24", {}) or {}
            pool = p.get("pairAddress")
            if pool:
                with self._lock:
                    self._pool_cache[base] = (pool, time.time())
            return {
                "name": p["baseToken"].get("name"),
                "symbol": p["baseToken"].get("symbol"),
                "image": info.get("imageUrl"),
                "price_usd": _f(p.get("priceUsd")),
                "change_h24": _f(pc.get("h24")),
                "change_h1": _f(pc.get("h1")),
                "volume_h24": _f(vol.get("h24")),
                "liquidity": _f((p.get("liquidity", {}) or {}).get("usd")),
                "market_cap": _f(p.get("marketCap")),
                "fdv": _f(p.get("fdv")),
                "buys_h24": txns.get("buys"),
                "sells_h24": txns.get("sells"),
                "url": p.get("url"),
                "pool": pool,
                "source": "DexScreener",
            }
        except Exception:
            return None

    # ---------------- Лента сделок ----------------
    def trades(self, base, limit=25):
        base = (base or "").upper()
        if base != "GRINCH":
            return []   # лента отдельных сделок доступна только для GRINCH-джеттона
        data = self._cached(
            self._trades_cache, base,
            lambda b: self._fetch_trades(b, limit), self.ttl_trades,
        )
        return data or []

    def _pool(self, base):
        with self._lock:
            entry = self._pool_cache.get(base)
            if entry and time.time() - entry[1] < 600:
                return entry[0]
        self._fetch_market(base)   # подтянет адрес пула в кэш
        with self._lock:
            entry = self._pool_cache.get(base)
            return entry[0] if entry else None

    def _fetch_trades(self, base, limit):
        pool = self._pool(base)
        if not pool:
            return []
        try:
            r = requests.get(
                f"https://api.geckoterminal.com/api/v2/networks/ton/pools/{pool}/trades",
                timeout=12,
            )
            r.raise_for_status()
            grinch = (Config.GRINCH_TOKEN_ADDRESS or "").lower()
            out = []
            for t in (r.json().get("data") or [])[:limit]:
                a = t.get("attributes", {}) or {}
                to_addr = (a.get("to_token_address") or "").lower()
                from_addr = (a.get("from_token_address") or "").lower()
                # Определяем сторону по адресу токена GRINCH (надёжнее, чем поле kind)
                if to_addr == grinch:
                    kind = "buy"      # GRINCH получен
                    token_amount = _f(a.get("to_token_amount"))
                    ton_amount = _f(a.get("from_token_amount"))
                    price = _f(a.get("price_to_in_usd"))
                elif from_addr == grinch:
                    kind = "sell"     # GRINCH продан
                    token_amount = _f(a.get("from_token_amount"))
                    ton_amount = _f(a.get("to_token_amount"))
                    price = _f(a.get("price_from_in_usd"))
                else:
                    # фолбэк, если адреса не совпали
                    kind = a.get("kind")
                    if kind == "buy":
                        token_amount = _f(a.get("to_token_amount"))
                        ton_amount = _f(a.get("from_token_amount"))
                        price = _f(a.get("price_to_in_usd"))
                    else:
                        token_amount = _f(a.get("from_token_amount"))
                        ton_amount = _f(a.get("to_token_amount"))
                        price = _f(a.get("price_from_in_usd"))
                out.append({
                    "kind": kind,
                    "amount_usd": _f(a.get("volume_in_usd")),
                    "price_usd": price,
                    "token_amount": token_amount,
                    "ton_amount": ton_amount,
                    "ts": a.get("block_timestamp"),
                })
            return out
        except Exception:
            return []

    # ---------------- Общий кэш ----------------
    def _cached(self, cache, key, fetch, ttl):
        now = time.time()
        with self._lock:
            e = cache.get(key)
            if e and now - e[1] < ttl:
                return e[0]
        val = fetch(key)
        if val is not None:
            with self._lock:
                cache[key] = (val, now)
            return val
        with self._lock:
            e = cache.get(key)
            return e[0] if e else None


coin_info = CoinInfo()
