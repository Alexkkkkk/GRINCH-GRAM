"""Core grid trading engine."""
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict

from config import Config
from database import GridDatabase
from exchange_client import BinanceExchangeClient

log = logging.getLogger("grid_engine")
STATE_FILE = "grid_state.json"


@dataclass
class GridLevel:
    id: int
    side: str
    price: float
    quantity: float = 0.0
    order_id: Optional[int] = None
    status: str = "waiting"
    filled_at: Optional[float] = None
    profit_usdt: float = 0.0
    paired_level_id: Optional[int] = None


@dataclass
class GridStats:
    total_profit_usdt: float = 0.0
    total_profit_pct: float = 0.0
    roi_pct: float = 0.0
    total_trades: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    avg_profit_per_grid: float = 0.0
    running_time_sec: float = 0.0
    start_time: Optional[float] = None
    current_price: float = 0.0
    upper_price: float = 0.0
    lower_price: float = 0.0
    active_orders: int = 0
    grid_count: int = 0
    step_pct: float = 0.0


class GridTradingEngine:
    def __init__(self):
        self.client = BinanceExchangeClient()
        self.db = GridDatabase()
        self.symbol = Config.SYMBOL
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.levels: List[GridLevel] = []
        self.stats = GridStats()
        self.price_history: List[float] = []
        self.last_rebuild: float = 0
        self.step_ratio: float = 1.0
        self._load_state()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        if not self.stats.start_time:
            self.stats.start_time = time.time()
        log.info("[GridEngine] Started")

    def stop(self):
        self._stop.set()
        self.client.cancel_all_orders()
        self._save_state()
        log.info("[GridEngine] Stopped")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_status(self) -> Dict:
        with self._lock:
            if self.stats.start_time:
                self.stats.running_time_sec = time.time() - self.stats.start_time
            return {
                "active": self.is_running(),
                "symbol": self.symbol,
                "stats": asdict(self.stats),
                "levels": [asdict(l) for l in self.levels],
                "step_pct": self.stats.step_pct,
            }

    def build_grid(self, upper: float = None, lower: float = None,
                   grid_count: int = None, investment: float = None) -> Dict:
        with self._lock:
            self.client.cancel_all_orders()
            self.db.clear_levels(self.symbol)
            price = self.client.get_price()
            if not price:
                return {"ok": False, "error": "Cannot get price"}
            grid_count = grid_count or Config.GRID_COUNT
            investment = investment or Config.TOTAL_INVESTMENT
            upper = upper or Config.UPPER_PRICE
            lower = lower or Config.LOWER_PRICE
            if not upper or not lower:
                atr_pct = self._calc_atr_pct()
                upper = price * (1 + atr_pct * 3 / 100)
                lower = price * (1 - atr_pct * 3 / 100)
            upper = max(upper, price * 1.005)
            lower = min(lower, price * 0.995)
            self.step_ratio = (upper / lower) ** (1.0 / grid_count)
            self.stats.step_pct = (self.step_ratio - 1.0) * 100
            self.last_rebuild = time.time()
            self.levels = []
            current = lower
            lvl_id = 0
            all_prices = []
            while current <= upper * 1.001:
                lvl_id += 1
                all_prices.append(round(current, 8))
                current *= self.step_ratio
            base_bal, quote_bal = self.client.get_balances()
            buy_prices = [p for p in all_prices if p < price]
            sell_prices = [p for p in all_prices if p > price]
            usdt_per_buy = (investment * 0.5 / len(buy_prices)) if buy_prices else 0
            base_per_sell = (base_bal / len(sell_prices)) if sell_prices else 0
            lvl_id = 0
            for p in all_prices:
                lvl_id += 1
                side = "buy" if p < price else ("sell" if p > price else "neutral")
                qty = 0
                if side == "buy" and usdt_per_buy >= Config.MIN_ORDER_USDT:
                    qty = usdt_per_buy / p
                elif side == "sell" and base_per_sell > 0:
                    qty = base_per_sell
                lvl = GridLevel(id=lvl_id, side=side, price=p, quantity=round(qty, 6))
                self.levels.append(lvl)
                if side == "buy" and qty > 0:
                    res = self.client.place_limit_buy(qty, p)
                    if res["ok"]:
                        lvl.order_id = res["order_id"]
                        lvl.status = "open"
                elif side == "sell" and qty > 0:
                    res = self.client.place_limit_sell(qty, p)
                    if res["ok"]:
                        lvl.order_id = res["order_id"]
                        lvl.status = "open"
                self.db.save_level(self.symbol, lvl.id, lvl.side, lvl.price,
                                   lvl.quantity, lvl.status, lvl.order_id)
            self.stats.upper_price = upper
            self.stats.lower_price = lower
            self.stats.grid_count = len(self.levels)
            self.stats.current_price = price
            self._save_state()
            self.db.save_grid_config(self.symbol, upper, lower, grid_count,
                                     investment, self.stats.step_pct)
            log.info("[GridEngine] Built %d levels %.4f-%.4f step %.2f%%",
                     len(self.levels), lower, upper, self.stats.step_pct)
            return {
                "ok": True,
                "levels_count": len(self.levels),
                "upper": round(upper, 6),
                "lower": round(lower, 6),
                "step_pct": round(self.stats.step_pct, 2),
                "center": price,
            }

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log.error("[GridEngine] Tick error: %s", e)
            time.sleep(Config.TICK_INTERVAL)

    def _tick(self):
        price = self.client.get_price()
        if not price:
            return
        self.stats.current_price = price
        self.price_history.append(price)
        if len(self.price_history) > 200:
            self.price_history = self.price_history[-200:]
        with self._lock:
            if not self.levels:
                return
            if self._should_recenter(price):
                log.info("[GridEngine] Recenter triggered")
                self.build_grid()
                return
            self._check_orders()
            self._update_stats()
            self._save_state()

    def _should_recenter(self, price: float) -> bool:
        if not self.levels or not self.stats.step_pct:
            return False
        steps = abs(math.log(price / self.stats.current_price)) / math.log(1 + self.stats.step_pct / 100)
        return steps >= Config.RECENTER_THRESHOLD and (time.time() - self.last_rebuild) >= Config.RECENTER_COOLDOWN

    def _check_orders(self):
        for lvl in self.levels:
            if lvl.status != "open" or not lvl.order_id:
                continue
            res = self.client.get_order_status(lvl.order_id)
            if not res["ok"]:
                continue
            order = res["order"]
            if order["status"] == "FILLED":
                self._handle_fill(lvl, order)
            elif order["status"] in ("CANCELED", "REJECTED", "EXPIRED"):
                lvl.status = "cancelled"

    def _handle_fill(self, lvl: GridLevel, order: Dict):
        executed_qty = float(order.get("executedQty", 0))
        executed_price = float(order.get("price", lvl.price))
        lvl.status = "filled"
        lvl.filled_at = time.time()
        if lvl.side == "buy":
            self.stats.buy_trades += 1
            sell_price = lvl.price * self.step_ratio
            sell_lvl = self._find_level_by_price(sell_price)
            if sell_lvl:
                sell_qty = executed_qty * (1 - Config.FEE_PCT / 100)
                sell_lvl.quantity = round(sell_qty, 6)
                sell_lvl.side = "sell"
                sell_lvl.paired_level_id = lvl.id
                res = self.client.place_limit_sell(sell_qty, sell_price)
                if res["ok"]:
                    sell_lvl.order_id = res["order_id"]
                    sell_lvl.status = "open"
        elif lvl.side == "sell":
            self.stats.sell_trades += 1
            if lvl.paired_level_id:
                buy_lvl = next((l for l in self.levels if l.id == lvl.paired_level_id), None)
                if buy_lvl:
                    buy_cost = buy_lvl.quantity * buy_lvl.price
                    sell_rev = executed_qty * executed_price * (1 - Config.FEE_PCT / 100)
                    profit = sell_rev - buy_cost
                    lvl.profit_usdt = round(profit, 4)
                    self.stats.total_profit_usdt += profit
                    if profit > 0:
                        self.stats.win_trades += 1
                    else:
                        self.stats.loss_trades += 1
            buy_price = lvl.price / self.step_ratio
            buy_lvl = self._find_level_by_price(buy_price)
            if buy_lvl:
                quote_qty = lvl.quantity * buy_price
                if quote_qty >= Config.MIN_ORDER_USDT:
                    buy_qty = quote_qty / buy_price
                    buy_lvl.quantity = round(buy_qty, 6)
                    buy_lvl.side = "buy"
                    buy_lvl.paired_level_id = lvl.id
                    res = self.client.place_limit_buy(buy_qty, buy_price)
                    if res["ok"]:
                        buy_lvl.order_id = res["order_id"]
                        buy_lvl.status = "open"
        self.stats.total_trades = self.stats.buy_trades + self.stats.sell_trades
        self._update_stats()
        amount_usdt = executed_qty * executed_price
        fee = amount_usdt * Config.FEE_PCT / 100
        self.db.save_trade(self.symbol, lvl.side, lvl.id, executed_price,
                           executed_qty, amount_usdt, lvl.profit_usdt, fee,
                           str(lvl.order_id))

    def _find_level_by_price(self, price: float) -> Optional[GridLevel]:
        for lvl in self.levels:
            if abs(lvl.price - price) / price < 0.001:
                return lvl
        return None

    def _update_stats(self):
        invested = Config.TOTAL_INVESTMENT
        if invested > 0:
            self.stats.roi_pct = (self.stats.total_profit_usdt / invested) * 100
        cycles = self.stats.sell_trades
        if cycles > 0:
            self.stats.avg_profit_per_grid = self.stats.total_profit_usdt / cycles
        self.stats.active_orders = sum(1 for l in self.levels if l.status == "open")
        self.db.save_pnl_snapshot(self.symbol, self.stats.total_profit_usdt,
                                  self.stats.roi_pct, self.stats.current_price)

    def _calc_atr_pct(self) -> float:
        if len(self.price_history) < 14:
            return 2.0
        recent = self.price_history[-20:]
        trs = [abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))]
        atr = sum(trs) / len(trs)
        current = recent[-1]
        return (atr / current * 100) if current > 0 else 2.0

    def _save_state(self):
        try:
            state = {
                "levels": [asdict(l) for l in self.levels],
                "stats": asdict(self.stats),
                "last_rebuild": self.last_rebuild,
                "step_ratio": self.step_ratio,
            }
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, default=str)
        except Exception as e:
            log.error("Save state error: %s", e)

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            self.last_rebuild = state.get("last_rebuild", 0)
            self.step_ratio = state.get("step_ratio", 1.0)
            stats = state.get("stats", {})
            for k, v in stats.items():
                if hasattr(self.stats, k):
                    setattr(self.stats, k, v)
        except Exception as e:
            log.warning("Load state error: %s", e)
