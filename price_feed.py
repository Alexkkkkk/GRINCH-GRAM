import time
import threading
import requests
from config import Config

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

    def __init__(self, ttl=30):
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
            r = requests.get(
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
            r = requests.get(
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


price_feed = PriceFeed()
