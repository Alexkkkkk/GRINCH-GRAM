"""Binance Spot API client."""
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Optional, Tuple
try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
except ImportError:
    raise ImportError("pip install python-binance")
from config import Config

log = logging.getLogger("exchange")

class BinanceExchangeClient:
    def __init__(self):
        self.client = Client(api_key=Config.BINANCE_API_KEY, api_secret=Config.BINANCE_API_SECRET, testnet=Config.USE_BINANCE_TESTNET)
        self.symbol = Config.GRID_SYMBOL; self._exchange_info = None; self._load_exchange_info()
    def _load_exchange_info(self):
        try:
            info = self.client.get_exchange_info(); self._exchange_info = {s["symbol"]: s for s in info["symbols"]}
        except Exception as e: log.error("Failed to load exchange info: %s", e)
    def get_symbol_info(self) -> Optional[Dict]: return self._exchange_info.get(self.symbol) if self._exchange_info else None
    def get_price(self) -> float:
        try: ticker = self.client.get_symbol_ticker(symbol=self.symbol); return float(ticker["price"])
        except Exception as e: log.error("Price fetch error: %s", e); return 0.0
    def get_balance(self, asset: str) -> float:
        try: acc = self.client.get_asset_balance(asset=asset); return float(acc["free"]) if acc else 0.0
        except Exception as e: log.error("Balance fetch error: %s", e); return 0.0
    def get_balances(self) -> Tuple[float, float]:
        base = self.symbol.replace("USDT", "").replace("BUSD", "").replace("USDC", "")
        quote = "USDT" if "USDT" in self.symbol else ("BUSD" if "BUSD" in self.symbol else "USDC")
        return self.get_balance(base), self.get_balance(quote)
    def get_filters(self) -> Dict:
        info = self.get_symbol_info()
        if not info: return {}
        return {f["filterType"]: f for f in info.get("filters", [])}
    def format_quantity(self, qty: float) -> str:
        filters = self.get_filters(); step = filters.get("LOT_SIZE", {}).get("stepSize", "0.0001")
        d = Decimal(str(qty)).quantize(Decimal(step), rounding=ROUND_DOWN); return str(d)
    def format_price(self, price: float) -> str:
        filters = self.get_filters(); step = filters.get("PRICE_FILTER", {}).get("tickSize", "0.0001")
        d = Decimal(str(price)).quantize(Decimal(step), rounding=ROUND_DOWN); return str(d)
    def place_limit_buy(self, quantity: float, price: float) -> Dict:
        try:
            order = self.client.order_limit_buy(symbol=self.symbol, quantity=self.format_quantity(quantity), price=self.format_price(price))
            log.info("BUY order placed: %s @ %s, id=%s", quantity, price, order["orderId"]); return {"ok": True, "order_id": order["orderId"], "raw": order}
        except BinanceAPIException as e: log.error("BUY order failed: %s", e); return {"ok": False, "error": str(e)}
    def place_limit_sell(self, quantity: float, price: float) -> Dict:
        try:
            order = self.client.order_limit_sell(symbol=self.symbol, quantity=self.format_quantity(quantity), price=self.format_price(price))
            log.info("SELL order placed: %s @ %s, id=%s", quantity, price, order["orderId"]); return {"ok": True, "order_id": order["orderId"], "raw": order}
        except BinanceAPIException as e: log.error("SELL order failed: %s", e); return {"ok": False, "error": str(e)}
    def get_order_status(self, order_id: int) -> Dict:
        try: order = self.client.get_order(symbol=self.symbol, orderId=order_id); return {"ok": True, "order": order}
        except Exception as e: return {"ok": False, "error": str(e)}
    def cancel_order(self, order_id: int) -> Dict:
        try: result = self.client.cancel_order(symbol=self.symbol, orderId=order_id); log.info("Order %s cancelled", order_id); return {"ok": True, "result": result}
        except Exception as e: log.error("Cancel failed: %s", e); return {"ok": False, "error": str(e)}
    def cancel_all_orders(self) -> Dict:
        try:
            open_orders = self.client.get_open_orders(symbol=self.symbol)
            for o in open_orders: self.cancel_order(o["orderId"])
            return {"ok": True, "cancelled": len(open_orders)}
        except Exception as e: return {"ok": False, "error": str(e)}
    def get_klines(self, interval: str = "1h", limit: int = 50) -> list:
        try: return self.client.get_klines(symbol=self.symbol, interval=interval, limit=limit)
        except Exception as e: log.error("Klines fetch error: %s", e); return []
