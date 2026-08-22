"""
grid_trader.py — чистая классическая Spot Grid (Binance-style)
Без DCA. Только: BUY ниже → SELL выше. Прибыль с каждой сетки.
"""

import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

log = logging.getLogger("grid")

DATA_DIR = os.getenv("DATA_DIR", ".")
STATE_FILE = os.path.join(DATA_DIR, "grid_state.json")
TRADE_HISTORY_FILE = os.path.join(DATA_DIR, "grid_trade_history.json")


class GridConfig:
    """Конфигурация классической сетки (значения берутся из Config.py)."""

    DEFAULT_STEP_PCT = 3.5
    MIN_STEP_PCT = 3.0
    MAX_STEP_PCT = 8.0
    SELL_LEVELS_COUNT = 20
    BUY_LEVELS_COUNT = 20
    ADAPTIVE_STEP = True
    MIN_ORDER_TON = 15.0
    GAS_RESERVE_TON = 5.0
    FEE_PCT = 0.01
    GAS_PER_TRADE_TON = 0.30
    TICK_INTERVAL_SEC = 15
    RECENTER_STEPS = 1.8
    RECENTER_COOLDOWN = 1800

    @classmethod
    def min_profitable_order_ton(cls, step_pct=None):
        try:
            step = float(step_pct)
        except (TypeError, ValueError):
            step = cls.DEFAULT_STEP_PCT
        step = max(step, 0.0)
        cycle_factor = (1.0 + step / 100.0) * (1.0 - cls.FEE_PCT) ** 2 - 1.0
        if cycle_factor <= 0:
            return float("inf")
        gas_min = (cls.GAS_PER_TRADE_TON * 2.0) / cycle_factor
        return max(float(cls.MIN_ORDER_TON), math.ceil(gas_min * 10.0) / 10.0)

    @classmethod
    def adaptive_step(cls, atr_pct):
        """Подбираем шаг по ATR: чем выше волатильность — тем шире сетка."""
        if not cls.ADAPTIVE_STEP:
            return cls.DEFAULT_STEP_PCT
        if atr_pct >= 8.0:
            return min(cls.MAX_STEP_PCT, 7.0)
        elif atr_pct >= 5.0:
            return min(cls.MAX_STEP_PCT, 5.0)
        elif atr_pct >= 3.0:
            return 4.0
        else:
            return max(cls.MIN_STEP_PCT, 3.0)


@dataclass
class GridLevel:
    id: int
    side: str
    price_ton: float
    amount_grinch: float = 0.0
    amount_ton: float = 0.0
    status: str = "waiting"
    filled_at: Optional[float] = None
    fill_price_ton: float = 0.0
    profit_ton: float = 0.0
    tx_hash: str = ""
    note: str = ""


@dataclass
class GridState:
    active: bool = False
    center_price: float = 0.0
    step_pct: float = 0.0
    upper_price: float = 0.0
    lower_price: float = 0.0
    sell_levels: List[GridLevel] = field(default_factory=list)
    buy_levels: List[GridLevel] = field(default_factory=list)
    completed_fills: List[GridLevel] = field(default_factory=list)
    total_profit_ton: float = 0.0
    total_sell_cycles: int = 0
    grid_reserved_grinch: float = 0.0
    last_action: str = ""
    last_rebuild: float = 0.0
    last_tick: float = 0.0
    win_streak: int = 0
    loss_streak: int = 0

    def to_dict(self):
        d = asdict(self)
        d["sell_levels"] = [asdict(l) for l in self.sell_levels]
        d["buy_levels"] = [asdict(l) for l in self.buy_levels]
        d["completed_fills"] = [asdict(l) for l in self.completed_fills]
        return d

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for k, v in d.items():
            if k in ("sell_levels", "buy_levels", "completed_fills"):
                setattr(s, k, [GridLevel(**l) for l in (v or [])])
            elif hasattr(s, k):
                setattr(s, k, v)
        return s


class GridTrader:
    def __init__(self):
        self._dc = None
        self._ai = None
        self._trader = None
        self._state = GridState()
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._load_state()
        self._trade_history: List[dict] = []
        self._load_trade_history()
        self._price_history: List[float] = []

    def inject(self, dedust_client=None, ai_engine=None, trader_ref=None):
        self._dc = dedust_client
        self._ai = ai_engine
        self._trader = trader_ref

    def start_poller(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("[Grid] Poller started")

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log.error("[Grid] tick error: %s", e)
            time.sleep(GridConfig.TICK_INTERVAL_SEC)

    def _get_price(self):
        try:
            if self._dc and hasattr(self._dc, "get_grinch_ton_price"):
                return self._dc.get_grinch_ton_price()
            if self._trader and hasattr(self._trader, "get_price"):
                return self._trader.get_price()
        except Exception as e:
            log.debug("[Grid] price error: %s", e)
        return 0.0

    def _get_balances(self):
        try:
            if self._trader and hasattr(self._trader, "get_balances"):
                return self._trader.get_balances()
            if self._dc and hasattr(self._dc, "get_balances"):
                return self._dc.get_balances()
        except Exception as e:
            log.debug("[Grid] balance error: %s", e)
        return None, None

    def _calc_atr_pct(self):
        """ATR как % от текущей цены (по последним 20 ценам)."""
        if len(self._price_history) < 5:
            return GridConfig.DEFAULT_STEP_PCT
        recent = self._price_history[-20:]
        if len(recent) < 2:
            return GridConfig.DEFAULT_STEP_PCT
        trs = []
        for i in range(1, len(recent)):
            trs.append(abs(recent[i] - recent[i - 1]))
        atr = sum(trs) / len(trs)
        current = recent[-1]
        return (atr / current * 100) if current > 0 else GridConfig.DEFAULT_STEP_PCT

    def _tick(self):
        with self._lock:
            price_ton = self._get_price()
            if not price_ton:
                return
            self._state.last_tick = time.time()
            self._price_history.append(price_ton)
            if len(self._price_history) > 100:
                self._price_history = self._price_history[-100:]

            # Если сетка не активна — строим
            if not self._state.active or not self._state.sell_levels:
                self._maybe_build_grid(price_ton)
                return

            # Перецентровка при сильном уходе цены
            if self._should_recenter(price_ton):
                log.info("[Grid] Recenter triggered @ %.6f", price_ton)
                self._maybe_build_grid(price_ton)
                return

            # SELL: цена выросла до уровня
            for level in self._state.sell_levels:
                if level.status == "waiting" and price_ton >= level.price_ton:
                    res = self._execute_sell(level, price_ton)
                    if res.get("ok"):
                        self._record_trade("SELL", level, price_ton)
                        self._state.last_action = (
                            f"SELL L{level.id}: {level.amount_grinch:.0f} @ {price_ton:.6f} "
                            f"| profit {level.profit_ton:+.3f} TON"
                        )
                        log.info("[Grid] %s", self._state.last_action)
                        self._place_rebuy(level)
                    self._save_state()
                    return

            # BUY: цена упала до уровня
            for level in self._state.buy_levels:
                if level.status == "waiting" and price_ton <= level.price_ton:
                    res = self._execute_buy(level, price_ton)
                    if res.get("ok"):
                        self._record_trade("BUY", level, price_ton)
                        self._state.last_action = f"BUY L{level.id}: {level.amount_grinch:.0f} @ {price_ton:.6f}"
                        log.info("[Grid] %s", self._state.last_action)
                        self._place_resell(level)
                    self._save_state()
                    return

    def _should_recenter(self, price_ton):
        if not self._state.center_price or not self._state.step_pct:
            return False
        steps_away = abs(math.log(price_ton / self._state.center_price)) / math.log(
            1 + self._state.step_pct / 100
        )
        if steps_away >= GridConfig.RECENTER_STEPS:
            if time.time() - self._state.last_rebuild >= GridConfig.RECENTER_COOLDOWN:
                return True
        return False

    def _maybe_build_grid(self, center_price):
        ton_bal, grin_bal = self._get_balances()
        if ton_bal is None:
            log.warning("[Grid] No balances available, skipping grid build")
            return
        atr_pct = self._calc_atr_pct()
        step = GridConfig.adaptive_step(atr_pct)
        self.build_grid(
            center_price, step_pct=step, grinch_balance=grin_bal, ton_balance=ton_bal
        )

    def build_grid(
        self,
        center_price,
        step_pct=None,
        sell_levels=None,
        buy_levels=None,
        grinch_balance=None,
        ton_balance=None,
    ):
        with self._lock:
            step = step_pct or GridConfig.DEFAULT_STEP_PCT
            step = max(GridConfig.MIN_STEP_PCT, min(GridConfig.MAX_STEP_PCT, step))
            n_sell = sell_levels or GridConfig.SELL_LEVELS_COUNT
            n_buy = buy_levels or GridConfig.BUY_LEVELS_COUNT

            s = GridState()
            s.active = True
            s.center_price = center_price
            s.step_pct = step
            s.last_rebuild = time.time()

            # Upper / Lower price
            s.upper_price = round(center_price * (1 + step / 100) ** n_sell, 6)
            s.lower_price = round(center_price / (1 + step / 100) ** n_buy, 6)

            # Распределяем GRINCH на SELL-уровни
            avail_grinch = max(0.0, float(grinch_balance or 0.0))
            grin_per_sell = avail_grinch / n_sell if n_sell > 0 else 0

            for i in range(1, n_sell + 1):
                price = center_price * (1 + step / 100) ** i
                s.sell_levels.append(
                    GridLevel(
                        id=i,
                        side="sell",
                        price_ton=round(price, 6),
                        amount_grinch=round(grin_per_sell, 2),
                        amount_ton=0,
                    )
                )
            s.grid_reserved_grinch = avail_grinch

            # Распределяем TON на BUY-уровни
            avail_ton = max(0.0, float(ton_balance or 0.0) - GridConfig.GAS_RESERVE_TON)
            ton_per_buy = avail_ton / n_buy if n_buy > 0 else 0
            min_order = GridConfig.min_profitable_order_ton(step)

            for i in range(1, n_buy + 1):
                price = center_price / (1 + step / 100) ** i
                amount_ton = ton_per_buy
                if amount_ton < min_order:
                    amount_ton = 0
                s.buy_levels.append(
                    GridLevel(
                        id=-i,
                        side="buy",
                        price_ton=round(price, 6),
                        amount_grinch=0,
                        amount_ton=round(amount_ton, 2),
                    )
                )

            self._state = s
            self._save_state()
            log.info(
                "[Grid] Built: center=%.6f step=%.1f%% sell=%d buy=%d "
                "upper=%.6f lower=%.6f grin=%.0f ton=%.1f",
                center_price,
                step,
                n_sell,
                n_buy,
                s.upper_price,
                s.lower_price,
                avail_grinch,
                avail_ton,
            )
            return s

    def _execute_sell(self, level, price_ton):
        try:
            if level.amount_grinch <= 0:
                return {"ok": False, "error": "zero_amount"}
            result = self._dc.sell(level.amount_grinch)
            if result.get("ok"):
                received_ton = result.get(
                    "received_ton",
                    level.amount_grinch * price_ton * (1 - GridConfig.FEE_PCT),
                )
                net_ton = received_ton - GridConfig.GAS_PER_TRADE_TON
                cost_ton = level.amount_ton or (
                    level.amount_grinch
                    * level.price_ton
                    / (1 + self._state.step_pct / 100)
                )
                profit = net_ton - cost_ton
                level.status = "filled"
                level.filled_at = time.time()
                level.fill_price_ton = price_ton
                level.profit_ton = round(profit, 4)
                level.tx_hash = result.get("tx_hash", "")
                self._state.total_profit_ton += profit
                self._state.total_sell_cycles += 1
                self._state.grid_reserved_grinch = max(
                    0.0, self._state.grid_reserved_grinch - level.amount_grinch
                )
                if profit > 0:
                    self._state.win_streak += 1
                    self._state.loss_streak = 0
                else:
                    self._state.loss_streak += 1
                    self._state.win_streak = 0
                return {"ok": True}
            return {"ok": False, "error": result.get("error")}
        except Exception as e:
            log.error("[Grid] SELL error: %s", e)
            return {"ok": False, "error": str(e)}

    def _execute_buy(self, level, price_ton):
        try:
            if level.amount_ton <= 0:
                return {"ok": False, "error": "zero_amount"}
            result = self._dc.buy(level.amount_ton)
            if result.get("ok"):
                received_grinch = result.get(
                    "received_grinch",
                    level.amount_ton / price_ton * (1 - GridConfig.FEE_PCT),
                )
                level.amount_grinch = round(received_grinch, 2)
                level.status = "filled"
                level.filled_at = time.time()
                level.fill_price_ton = price_ton
                self._state.grid_reserved_grinch += received_grinch
                return {"ok": True}
            return {"ok": False, "error": result.get("error")}
        except Exception as e:
            log.error("[Grid] BUY error: %s", e)
            return {"ok": False, "error": str(e)}

    def _place_rebuy(self, sold_level):
        """После SELL — ставим BUY на той же цене для реинвеста."""
        rebuy = GridLevel(
            id=sold_level.id * 1000,
            side="buy",
            price_ton=round(sold_level.price_ton / (1 + self._state.step_pct / 100), 6),
            amount_ton=(
                round(sold_level.profit_ton + sold_level.amount_ton, 2)
                if sold_level.profit_ton > 0
                else sold_level.amount_ton
            ),
            status="waiting",
        )
        self._state.buy_levels.append(rebuy)
        log.info(
            "[Grid] ReBUY placed @ %.6f for %.2f TON", rebuy.price_ton, rebuy.amount_ton
        )

    def _place_resell(self, bought_level):
        """После BUY — ставим SELL на цену покупки + шаг."""
        resell = GridLevel(
            id=bought_level.id * 1000,
            side="sell",
            price_ton=round(
                bought_level.fill_price_ton * (1 + self._state.step_pct / 100), 6
            ),
            amount_grinch=bought_level.amount_grinch,
            status="waiting",
        )
        self._state.sell_levels.append(resell)
        log.info(
            "[Grid] ReSELL placed @ %.6f for %.0f GRINCH",
            resell.price_ton,
            resell.amount_grinch,
        )

    def _record_trade(self, side, level, price_ton):
        entry = {
            "time": time.time(),
            "side": side,
            "price": round(price_ton, 6),
            "amount_grinch": round(level.amount_grinch, 2),
            "amount_ton": round(level.amount_ton, 2),
            "profit_ton": round(level.profit_ton, 4) if side == "SELL" else 0.0,
            "cumulative_profit": round(self._state.total_profit_ton, 4),
        }
        self._trade_history.append(entry)
        self._save_trade_history()

    def _matched_trades_24h(self):
        """Сколько сделок за последние 24 часа."""
        cutoff = time.time() - 86400
        return len([t for t in self._trade_history if t["time"] >= cutoff])

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self._state.to_dict(), f, indent=2)
        except Exception as e:
            log.warning("[Grid] save state error: %s", e)

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    self._state = GridState.from_dict(json.load(f))
        except Exception as e:
            log.warning("[Grid] load state error: %s", e)

    def _save_trade_history(self):
        try:
            with open(TRADE_HISTORY_FILE, "w") as f:
                json.dump(self._trade_history, f, indent=2)
        except Exception as e:
            log.warning("[Grid] save history error: %s", e)

    def _load_trade_history(self):
        try:
            if os.path.exists(TRADE_HISTORY_FILE):
                with open(TRADE_HISTORY_FILE, "r") as f:
                    self._trade_history = json.load(f)
        except Exception as e:
            log.warning("[Grid] load history error: %s", e)

    def status(self):
        s = self._state
        profit_per_grid = 0.0
        if s.step_pct > 0:
            cycle_factor = (1.0 + s.step_pct / 100.0) * (
                1.0 - GridConfig.FEE_PCT
            ) ** 2 - 1.0
            profit_per_grid = round(cycle_factor * 100, 2)
        return {
            "active": s.active,
            "center_price": s.center_price,
            "upper_price": s.upper_price,
            "lower_price": s.lower_price,
            "step_pct": s.step_pct,
            "profit_per_grid_pct": profit_per_grid,
            "sell_waiting": len([l for l in s.sell_levels if l.status == "waiting"]),
            "buy_waiting": len([l for l in s.buy_levels if l.status == "waiting"]),
            "total_profit_ton": round(s.total_profit_ton, 4),
            "total_cycles": s.total_sell_cycles,
            "matched_24h": self._matched_trades_24h(),
            "win_streak": s.win_streak,
            "loss_streak": s.loss_streak,
            "last_action": s.last_action,
            "trade_count": len(self._trade_history),
        }

    def get_pnl_history(self):
        return [(t["time"], t["cumulative_profit"]) for t in self._trade_history]
