import time
import threading
import requests
from http_client import SESSION as _HTTP
from config import Config


def _tc_headers() -> dict:
    """Заголовки для TonCenter API (X-API-Key, если задан в TONCENTER_API_KEY)."""
    key = Config.TONCENTER_API_KEY
    return {"X-API-Key": key} if key else {}

# Соответствие тикера → ID в CoinGecko (бесплатный API без ключа)
COINGECKO_IDS = {
    "TON": "the-open-network",
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "AVAX": "avalanche-2",
    "MATIC": "matic-network",
}


class PriceFeed:
    """Реальные цены через бесплатные API (CoinGecko + DexScreener). С кэшем по TTL."""

    def __init__(self, ttl=10):
        self.ttl = ttl
        self._cache = {}   # base -> (price, ts)
        self._lock = threading.Lock()

    def get(self, base, max_stale=None):
        """Цена базового актива в USD.

        max_stale: если задан (сек), при невозможности получить свежую цену
        НЕ возвращаем бесконечно устаревший кэш — только если он не старше
        max_stale. Используется для исполнения свопов (защита от устаревшей
        цены): передавайте небольшой max_stale, чтобы не торговать по протухшей
        котировке. Если max_stale=None — поведение прежнее (отдаём последнюю
        известную цену любой давности, годится для отображения в UI).
        """
        base = (base or "").upper()
        now = time.time()
        with self._lock:
            entry = self._cache.get(base)
            if entry and now - entry[1] < self.ttl:
                return entry[0]
        price = self._fetch(base)
        if price and price > 0:
            with self._lock:
                self._cache[base] = (price, now)
            return price
        # Свежую цену получить не удалось — отдаём последнюю известную.
        with self._lock:
            entry = self._cache.get(base)
            if not entry:
                return None
            if max_stale is not None and (now - entry[1]) > max_stale:
                return None
            return entry[0]

    def _fetch(self, base):
        cid = COINGECKO_IDS.get(base)
        if cid:
            return self._fetch_coingecko(cid)
        # GRINCH (TON-джеттон) — реальная цена через DexScreener по адресу контракта токена
        if base == "GRINCH":
            return self._fetch_dexscreener(Config.GRINCH_TOKEN_ADDRESS)
        # Неизвестная монета — нет реальной цены, exchange.py возьмёт демо-цену
        return None

    def _fetch_coingecko(self, coin_id):
        try:
            r = _HTTP.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
                timeout=10,
            )
            r.raise_for_status()
            return float(r.json()[coin_id]["usd"])
        except Exception:
            return None

    def _fetch_dexscreener(self, token_address):
        try:
            r = _HTTP.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
                timeout=10,
            )
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
            if pairs:
                # Берём пару с наибольшей ликвидностью
                pairs.sort(
                    key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0),
                    reverse=True,
                )
                return float(pairs[0]["priceUsd"])
        except Exception:
            pass
        return None

    # ───────────── курс TON↔GRINCH напрямую из пула (priceNative) ────────────

    def get_grinch_ton_price(self, max_stale=None):
        """Цена 1 GRINCH в TON напрямую из пула.

        Источник истины — РЕАЛЬНЫЕ резервы пула в блокчейне (get_pool_data, см.
        _fetch_grinch_ton_onchain); при недоступности — priceNative закреплённого
        пула с DexScreener. Это РЕАЛЬНЫЙ курс пула, а не перекрёстный USD-курс.
        min-out для свопов нужно считать ОТСЮДА: кросс-курс grinch_usd/ton_usd
        берёт цены из разных источников (DexScreener + CoinGecko) и систематически
        расходится с курсом нашего 1%-пула (на ~6%). Из-за этого min-out оказывался
        завышен, и пул DeDust отклонял каждую покупку (exit 65535, bounce). С курсом
        пула буфер SLIPPAGE_PCT уверенно перекрывает комиссию пула и проскальзывание.
        """
        key = "GRINCH_TON"
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry and now - entry[1] < self.ttl:
                return entry[0]
        price = self._fetch_grinch_ton_native()
        if price and price > 0:
            with self._lock:
                self._cache[key] = (price, now)
            return price
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            if max_stale is not None and (now - entry[1]) > max_stale:
                return None
            return entry[0]

    def _fetch_grinch_ton_native(self):
        # 1) Источник истины — РЕАЛЬНЫЕ резервы пула в блокчейне (без задержки и
        #    без лимитов внешних API). Цена = резерв TON / резерв GRINCH.
        onchain = self._fetch_grinch_ton_onchain()
        if onchain and onchain > 0:
            return onchain
        # 2) Резерв — priceNative закреплённого пула с DexScreener
        try:
            r = _HTTP.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{Config.GRINCH_TOKEN_ADDRESS}",
                timeout=10,
            )
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
            if not pairs:
                return None
            pinned = (getattr(Config, "GRINCH_POOL_ADDRESS", "") or "").lower()
            # 1) предпочитаем ЗАКРЕПЛЁННЫЙ пул — именно через него идут свопы
            if pinned:
                for p in pairs:
                    if (p.get("pairAddress", "") or "").lower() == pinned:
                        pn = p.get("priceNative")
                        if pn:
                            return float(pn)
            # 2) иначе — самый ликвидный пул, НО ТОЛЬКО с котировкой в TON:
            # priceNative имеет смысл лишь для TON-пары. Если котировка не в TON,
            # курс будет несопоставим — лучше вернуть None и отклонить своп, чем
            # считать min-out по чужому рынку.
            ton_pairs = [
                p for p in pairs
                if ((p.get("quoteToken", {}) or {}).get("symbol", "") or "").upper() == "TON"
                and p.get("priceNative")
            ]
            if not ton_pairs:
                return None
            ton_pairs.sort(
                key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0),
                reverse=True,
            )
            return float(ton_pairs[0]["priceNative"])
        except Exception:
            return None

    def _fetch_grinch_ton_onchain(self):
        """Цена 1 GRINCH в TON из РЕАЛЬНЫХ резервов пула в блокчейне.

        Самый точный источник — get-метод `get_pool_data` самого контракта пула
        (через TonCenter runGetMethod). Он отдаёт ФАКТИЧЕСКИЕ резервы пула — ровно
        те, по которым DeDust считает цену, — без задержки и без лимитов внешних
        агрегаторов. Важно: брать именно резервы из get_pool_data, а НЕ баланс
        TON-аккаунта пула (в балансе ~190 TON газа/ренты, что завышает цену на ~3%).

        У DeDust CPMM-v2 get_pool_data возвращает в стеке: позиция 9 — резерв TON
        (нано), позиция 10 — резерв GRINCH (нано). Оба актива 9-знаковые.
        """
        try:
            pool = Config.GRINCH_POOL_ADDRESS
            r = _HTTP.post(
                "https://toncenter.com/api/v2/runGetMethod",
                json={"address": pool, "method": "get_pool_data", "stack": []},
                headers={"Accept": "application/json", **_tc_headers()}, timeout=8,
            )
            d = r.json()
            res = d.get("result") or {}
            if not d.get("ok") or res.get("exit_code") not in (0, None):
                return None
            stack = res.get("stack") or []
            if len(stack) < 11 or stack[9][0] != "num" or stack[10][0] != "num":
                return None
            ton_reserve    = int(stack[9][1], 16) / 1e9
            grinch_reserve = int(stack[10][1], 16) / 1e9
            if ton_reserve > 0 and grinch_reserve > 0:
                price = ton_reserve / grinch_reserve
                # Защита от неверного разбора стека: курс должен быть в разумных пределах
                if 1e-6 < price < 1e-1:
                    return price
        except Exception:
            return None
        return None


price_feed = PriceFeed()
