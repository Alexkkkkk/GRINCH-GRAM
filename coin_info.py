import time
import threading
import requests
from http_client import SESSION as _HTTP
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

    def __init__(self, ttl_market=12, ttl_trades=8):
        self.ttl_market = ttl_market
        self.ttl_trades = ttl_trades
        self._lock = threading.Lock()
        self._market_cache = {}   # base -> (data, ts)
        self._trades_cache = {}   # base -> (data, ts)
        self._pool_cache = {}     # base -> (pool_addr, ts)
        self._exch_cache = {}     # base -> (data, ts)

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
            r = _HTTP.get(
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
            r = _HTTP.get(
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
            all_txns = p.get("txns", {}) or {}
            txns_h24 = all_txns.get("h24", {}) or {}
            txns_h6  = all_txns.get("h6",  {}) or {}
            txns_h1  = all_txns.get("h1",  {}) or {}
            pool = p.get("pairAddress")
            if pool:
                with self._lock:
                    self._pool_cache[base] = (pool, time.time())
            # Вычисляем buy/sell ratios по всем таймфреймам
            def _ratio(t):
                b, s = t.get("buys", 0) or 0, t.get("sells", 0) or 0
                return round(b / s, 3) if s else None
            return {
                "name": p["baseToken"].get("name"),
                "symbol": p["baseToken"].get("symbol"),
                "image": info.get("imageUrl"),
                "price_usd": _f(p.get("priceUsd")),
                "price_native": _f(p.get("priceNative")),
                "change_m5":  _f(pc.get("m5")),
                "change_h1":  _f(pc.get("h1")),
                "change_h6":  _f(pc.get("h6")),
                "change_h24": _f(pc.get("h24")),
                "volume_h24": _f(vol.get("h24")),
                "volume_h6":  _f(vol.get("h6")),
                "volume_h1":  _f(vol.get("h1")),
                "liquidity": _f((p.get("liquidity", {}) or {}).get("usd")),
                "market_cap": _f(p.get("marketCap")),
                "fdv": _f(p.get("fdv")),
                "buys_h24":  txns_h24.get("buys"),
                "sells_h24": txns_h24.get("sells"),
                "ratio_h24": _ratio(txns_h24),
                "buys_h6":   txns_h6.get("buys"),
                "sells_h6":  txns_h6.get("sells"),
                "ratio_h6":  _ratio(txns_h6),
                "buys_h1":   txns_h1.get("buys"),
                "sells_h1":  txns_h1.get("sells"),
                "ratio_h1":  _ratio(txns_h1),
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
            r = _HTTP.get(
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
                    "addr": a.get("tx_from_address") or "",
                })
            return out
        except Exception:
            return []

    # ---------------- Цены на всех биржах TON ----------------
    def exchanges(self, base):
        base = (base or "").upper()
        data = self._cached(self._exch_cache, base, self._fetch_exchanges, self.ttl_market)
        return data or {"exchanges": [], "agg": None}

    def _fetch_exchanges(self, base):
        if base == "GRINCH":
            rows = self._exchanges_dexscreener(Config.GRINCH_TOKEN_ADDRESS)
        else:
            cid = COINGECKO_IDS.get(base)
            rows = self._exchanges_coingecko(cid) if cid else []
        if not rows:
            return None
        return {"exchanges": rows, "agg": self._aggregate(rows)}

    def _exchanges_dexscreener(self, addr):
        try:
            r = _HTTP.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                timeout=10,
            )
            r.raise_for_status()
            want = (addr or "").lower()
            rows = []
            for p in (r.json().get("pairs") or []):
                if (p.get("baseToken", {}) or {}).get("address", "").lower() != want:
                    continue
                price = _f(p.get("priceUsd"))
                if not price:
                    continue
                rows.append({
                    "name": (p.get("dexId") or "DEX").title(),
                    "pair": f"{p['baseToken'].get('symbol')}/{p['quoteToken'].get('symbol')}",
                    "kind": "DEX",
                    "price": price,
                    "liquidity": _f((p.get("liquidity", {}) or {}).get("usd")),
                    "volume24h": _f((p.get("volume", {}) or {}).get("h24")),
                    "change24h": _f((p.get("priceChange", {}) or {}).get("h24")),
                    "url": p.get("url"),
                })
            rows.sort(key=lambda x: x.get("liquidity") or 0, reverse=True)
            return rows
        except Exception:
            return []

    def _exchanges_coingecko(self, cid):
        try:
            r = _HTTP.get(
                f"https://api.coingecko.com/api/v3/coins/{cid}/tickers",
                timeout=12,
            )
            r.raise_for_status()
            # Одна строка на биржу — выбираем тикер с наибольшим объёмом
            best = {}
            for t in (r.json().get("tickers") or []):
                price = _f((t.get("converted_last") or {}).get("usd"))
                if not price:
                    continue
                name = (t.get("market") or {}).get("name") or "Биржа"
                vol = _f((t.get("converted_volume") or {}).get("usd")) or 0.0
                row = {
                    "name": name,
                    "pair": f"{t.get('base')}/{t.get('target')}",
                    "kind": "CEX",
                    "price": price,
                    "liquidity": None,
                    "volume24h": vol,
                    "change24h": None,
                    "url": t.get("trade_url"),
                }
                prev = best.get(name)
                if prev is None or vol > (prev.get("volume24h") or 0):
                    best[name] = row
            rows = list(best.values())
            rows.sort(key=lambda x: x.get("volume24h") or 0, reverse=True)
            return rows[:15]
        except Exception:
            return []

    def _aggregate(self, rows):
        prices = [r["price"] for r in rows if r.get("price")]
        if not prices:
            return None
        # Вес — по ликвидности (DEX) или объёму (CEX); если нет, равный вес
        wsum = num = 0.0
        for r in rows:
            p = r.get("price")
            if not p:
                continue
            w = r.get("liquidity") or r.get("volume24h") or 1.0
            wsum += w
            num += p * w
        avg = num / wsum if wsum else sum(prices) / len(prices)
        pmin, pmax = min(prices), max(prices)
        spread = (pmax - pmin) / pmin * 100 if pmin else 0.0
        total_liq = sum((r.get("liquidity") or 0) for r in rows)
        total_vol = sum((r.get("volume24h") or 0) for r in rows)
        # Кросс-биржевой AI-сигнал
        if spread >= 1.5:
            signal, note = "АРБИТРАЖ", "Расхождение цен между биржами — возможен арбитраж"
        elif spread >= 0.4:
            signal, note = "РАСХОЖДЕНИЕ", "Небольшое расхождение цен между биржами"
        else:
            signal, note = "КОНСЕНСУС", "Цены на биржах согласованы"
        best_buy = min(rows, key=lambda r: r.get("price") or 9e18)
        best_sell = max(rows, key=lambda r: r.get("price") or 0)
        return {
            "avg_price": avg,
            "min_price": pmin,
            "max_price": pmax,
            "spread_pct": round(spread, 3),
            "total_liquidity": total_liq,
            "total_volume": total_vol,
            "count": len(prices),
            "signal": signal,
            "note": note,
            "best_buy": {"name": best_buy.get("name"), "price": best_buy.get("price")},
            "best_sell": {"name": best_sell.get("name"), "price": best_sell.get("price")},
        }

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
