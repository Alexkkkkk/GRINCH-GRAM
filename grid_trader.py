"""
grid_trader.py — AI-управляемая сеточная торговля GRINCH/TON на DeDust

Архитектура:
  • Recovery-grid: продаём GRINCH траншами по мере роста цены
  • Reinvestment: полученный TON реинвестируется в покупку на откатах
  • AI-фильтр: пропускаем продажу при сильном BUY-сигнале, пропускаем
    покупку при сильном SELL-сигнале
  • Динамический шаг: подстраивается под ATR (волатильность)

Сеточные параметры (по умолчанию):
  • Шаг: 5% (ATR-20 = 2.81% → min безубыток ≈ 2.8% → запас 2.2% чистой прибыли)
  • Комиссия DeDust: 1% каждая сторона + газ ≈ 0.3 TON/сделку
  • Продажа: 9 уровней выше текущей цены (882k / 9 ≈ 98k GRINCH/уровень)
  • Покупка: активируется по мере накопления TON от продаж
"""

import os
import json
import time
import threading
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Optional

log = logging.getLogger("grid")

DATA_DIR  = os.getenv("DATA_DIR", ".")
STATE_FILE = os.path.join(DATA_DIR, "grid_state.json")

# ─── Конфигурация сетки ──────────────────────────────────────────────────────

class GridConfig:
    # Шаг по умолчанию (%)
    DEFAULT_STEP_PCT   = float(os.getenv("GRID_STEP_PCT",   "5.0"))
    # Диапазон динамического шага (%)
    MIN_STEP_PCT       = float(os.getenv("GRID_MIN_STEP",   "2.5"))
    MAX_STEP_PCT       = float(os.getenv("GRID_MAX_STEP",   "10.0"))
    # Количество уровней
    SELL_LEVELS_COUNT  = int(os.getenv("GRID_SELL_LEVELS",  "9"))
    BUY_LEVELS_COUNT   = int(os.getenv("GRID_BUY_LEVELS",   "5"))
    # Минимальная стоимость ордера в TON (защита от пыли)
    MIN_ORDER_TON      = float(os.getenv("GRID_MIN_ORDER",  "15.0"))
    # Резерв TON на газ
    GAS_RESERVE_TON    = float(os.getenv("GRID_GAS_RESERVE","5.0"))
    # ATR-порог для расширения/сужения шага
    ATR_WIDE_PCT       = 5.0   # ATR > 5% → шаг 8%
    ATR_NORM_PCT       = 3.0   # ATR 3-5% → шаг 5%
    ATR_NARROW_PCT     = 2.0   # ATR 2-3% → шаг 3.5%
    # AI-пороги (% уверенности)
    AI_SKIP_SELL_BUY_CONF  = 75.0   # пропустить продажу если AI BUY ≥ 75%
    AI_SKIP_BUY_SELL_CONF  = 70.0   # пропустить покупку если AI SELL ≥ 70%
    # Период опроса цены
    TICK_INTERVAL_SEC  = int(os.getenv("GRID_TICK_SEC", "30"))


# ─── Структуры данных ────────────────────────────────────────────────────────

@dataclass
class GridLevel:
    id: int
    side: str               # 'sell' | 'buy'
    price_ton: float        # цена-триггер (TON/GRINCH)
    amount_grinch: float    # GRINCH на уровне (для sell)
    amount_ton: float       # TON на уровне (для buy)
    status: str             # 'waiting' | 'filled' | 'skipped_ai' | 'no_funds' | 'error'
    filled_at:   float = 0.0
    fill_price_ton: float = 0.0
    profit_ton:  float = 0.0
    tx_hash:     str   = ""
    note:        str   = ""


@dataclass
class GridState:
    active:              bool  = False
    center_price_ton:    float = 0.0
    step_pct:            float = GridConfig.DEFAULT_STEP_PCT
    sell_levels:   List[GridLevel] = field(default_factory=list)
    buy_levels:    List[GridLevel] = field(default_factory=list)
    total_profit_ton:    float = 0.0
    total_sell_cycles:   int   = 0
    total_buy_cycles:    int   = 0
    created_at:          float = 0.0
    last_tick_ts:        float = 0.0
    last_action:         str   = ""
    paused_reason:       str   = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sell_levels"] = [asdict(l) for l in self.sell_levels]
        d["buy_levels"]  = [asdict(l) for l in self.buy_levels]
        return d

    @staticmethod
    def from_dict(d: dict) -> "GridState":
        s = GridState()
        for k, v in d.items():
            if k == "sell_levels":
                s.sell_levels = [GridLevel(**l) for l in (v or [])]
            elif k == "buy_levels":
                s.buy_levels  = [GridLevel(**l) for l in (v or [])]
            else:
                try:
                    setattr(s, k, v)
                except Exception:
                    pass
        return s


# ─── Основной класс ──────────────────────────────────────────────────────────

class GridTrader:
    """AI-управляемая сеточная торговля GRINCH/TON."""

    def __init__(self):
        self._lock    = threading.RLock()
        self._state   = GridState()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._dc      = None    # DeDustClient, инжектируется извне
        self._ai      = None    # AIEngine, инжектируется извне
        self._load_state()
        log.info("[Grid] Инициализирован. active=%s, уровней: sell=%d buy=%d",
                 self._state.active,
                 len(self._state.sell_levels),
                 len(self._state.buy_levels))

    # ── Внешние зависимости ───────────────────────────────────────────────────

    def inject(self, dedust_client=None, ai_engine=None):
        """Инжектируем DeDust-клиент и AI-движок после инициализации."""
        if dedust_client is not None:
            self._dc = dedust_client
            log.info("[Grid] DeDust-клиент подключён")
        if ai_engine is not None:
            self._ai = ai_engine
            log.info("[Grid] AI-движок подключён")

    # ── Запуск фонового потока ────────────────────────────────────────────────

    def start_poller(self):
        """Запустить фоновый поток проверки цены."""
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, name="grid-trader", daemon=True
        )
        self._thread.start()
        log.info("[Grid] Фоновый поток запущен (интервал %ds)",
                 GridConfig.TICK_INTERVAL_SEC)

    # ── Публичное API ─────────────────────────────────────────────────────────

    def build_grid(
        self,
        current_price_ton: float,
        grinch_balance:    float,
        ton_balance:       float,
        step_pct:          float = None,
        sell_levels:       int   = None,
        buy_levels:        int   = None,
    ) -> dict:
        """Построить сетку по текущим рыночным данным.

        Args:
            current_price_ton: цена GRINCH в TON прямо сейчас
            grinch_balance:    сколько GRINCH доступно для продажи
            ton_balance:       сколько TON доступно для покупок
            step_pct:          шаг сетки (% между уровнями)
            sell_levels:       количество уровней продажи
            buy_levels:        количество уровней покупки
        """
        step_pct    = step_pct    or GridConfig.DEFAULT_STEP_PCT
        sell_levels = sell_levels or GridConfig.SELL_LEVELS_COUNT
        buy_levels  = buy_levels  or GridConfig.BUY_LEVELS_COUNT
        step_pct    = max(GridConfig.MIN_STEP_PCT, min(GridConfig.MAX_STEP_PCT, step_pct))

        with self._lock:
            state               = GridState()
            state.active        = False
            state.center_price_ton = current_price_ton
            state.step_pct      = step_pct
            state.created_at    = time.time()

            # ── Уровни продажи: GRINCH → TON (выше текущей цены) ──────────
            grinch_per_level = grinch_balance / sell_levels if sell_levels > 0 else 0
            for i in range(1, sell_levels + 1):
                trigger = current_price_ton * (1 + step_pct / 100) ** i
                if grinch_per_level * trigger < GridConfig.MIN_ORDER_TON:
                    log.debug("[Grid] Уровень SELL %d пропущен — сумма < %.0f TON",
                              i, GridConfig.MIN_ORDER_TON)
                    continue
                state.sell_levels.append(GridLevel(
                    id=i,
                    side="sell",
                    price_ton=round(trigger, 8),
                    amount_grinch=round(grinch_per_level, 2),
                    amount_ton=0.0,
                    status="waiting",
                    note=f"+{round((trigger/current_price_ton-1)*100, 1)}% от центра",
                ))

            # ── Уровни покупки: TON → GRINCH (ниже текущей цены) ──────────
            usable_ton   = max(0.0, ton_balance - GridConfig.GAS_RESERVE_TON)
            ton_per_level = usable_ton / buy_levels if buy_levels > 0 and usable_ton > 0 else 0
            for i in range(1, buy_levels + 1):
                trigger = current_price_ton / (1 + step_pct / 100) ** i
                st = "waiting" if ton_per_level >= GridConfig.MIN_ORDER_TON else "no_funds"
                state.buy_levels.append(GridLevel(
                    id=-i,
                    side="buy",
                    price_ton=round(trigger, 8),
                    amount_grinch=0.0,
                    amount_ton=round(ton_per_level, 4) if st == "waiting" else 0.0,
                    status=st,
                    note=f"-{round((1-trigger/current_price_ton)*100, 1)}% от центра",
                ))

            self._state = state
            self._save_state()

        sell_ok = sum(1 for l in state.sell_levels if l.status == "waiting")
        buy_ok  = sum(1 for l in state.buy_levels  if l.status == "waiting")
        log.info("[Grid] Сетка построена: %d sell + %d buy уровней, шаг=%.1f%%",
                 sell_ok, buy_ok, step_pct)
        return {
            "ok": True,
            "sell_levels_total": len(state.sell_levels),
            "sell_levels_active": sell_ok,
            "buy_levels_total": len(state.buy_levels),
            "buy_levels_active": buy_ok,
            "step_pct": step_pct,
            "center_price_ton": current_price_ton,
            "grinch_per_sell_level": round(grinch_per_level, 0),
            "ton_per_buy_level": round(ton_per_level, 4),
        }

    def activate(self) -> dict:
        """Активировать сетку."""
        with self._lock:
            if not self._state.sell_levels and not self._state.buy_levels:
                return {"ok": False, "error": "Сначала постройте сетку через /api/grid/build"}
            self._state.active        = True
            self._state.paused_reason = ""
            self._save_state()
            log.info("[Grid] ✅ Активирована")
            return {"ok": True, "message": "Grid активирована"}

    def deactivate(self, reason: str = "manual") -> dict:
        """Остановить сетку."""
        with self._lock:
            self._state.active        = False
            self._state.paused_reason = reason
            self._save_state()
            log.info("[Grid] ⏹ Остановлена: %s", reason)
            return {"ok": True, "message": f"Grid остановлена: {reason}"}

    def get_status(self) -> dict:
        """Текущий статус сетки для /api/grid/status."""
        with self._lock:
            s = self._state
            sell_waiting = [l for l in s.sell_levels if l.status == "waiting"]
            sell_filled  = [l for l in s.sell_levels if l.status == "filled"]
            buy_waiting  = [l for l in s.buy_levels  if l.status == "waiting"]
            buy_filled   = [l for l in s.buy_levels  if l.status == "filled"]

            # Ближайшие уровни
            next_sell = min(sell_waiting, key=lambda l: l.price_ton, default=None)
            next_buy  = max(buy_waiting,  key=lambda l: l.price_ton, default=None)

            return {
                "active":            s.active,
                "paused_reason":     s.paused_reason,
                "center_price_ton":  s.center_price_ton,
                "step_pct":          s.step_pct,
                "total_profit_ton":  round(s.total_profit_ton, 4),
                "total_sell_cycles": s.total_sell_cycles,
                "total_buy_cycles":  s.total_buy_cycles,
                "last_tick":         s.last_tick_ts,
                "last_action":       s.last_action,
                "sell": {
                    "total":    len(s.sell_levels),
                    "waiting":  len(sell_waiting),
                    "filled":   len(sell_filled),
                    "next_price_ton": next_sell.price_ton if next_sell else None,
                    "next_pct_away": round(
                        (next_sell.price_ton / s.center_price_ton - 1) * 100, 1
                    ) if next_sell and s.center_price_ton else None,
                },
                "buy": {
                    "total":   len(s.buy_levels),
                    "waiting": len(buy_waiting),
                    "filled":  len(buy_filled),
                    "next_price_ton": next_buy.price_ton if next_buy else None,
                    "next_pct_away": round(
                        (1 - next_buy.price_ton / s.center_price_ton) * 100, 1
                    ) if next_buy and s.center_price_ton else None,
                },
                "sell_levels": [
                    {
                        "id":           l.id,
                        "price_ton":    l.price_ton,
                        "amount_grinch":l.amount_grinch,
                        "status":       l.status,
                        "profit_ton":   round(l.profit_ton, 4),
                        "note":         l.note,
                        "filled_at":    l.filled_at,
                    }
                    for l in s.sell_levels
                ],
                "buy_levels": [
                    {
                        "id":         l.id,
                        "price_ton":  l.price_ton,
                        "amount_ton": l.amount_ton,
                        "status":     l.status,
                        "note":       l.note,
                        "filled_at":  l.filled_at,
                    }
                    for l in s.buy_levels
                ],
            }

    def adjust_step_by_atr(self, atr_pct: float) -> float:
        """Динамически корректирует шаг сетки по ATR."""
        if atr_pct >= GridConfig.ATR_WIDE_PCT:
            step = 8.0
        elif atr_pct >= GridConfig.ATR_NORM_PCT:
            step = 6.0
        elif atr_pct >= GridConfig.ATR_NARROW_PCT:
            step = 5.0
        else:
            step = 3.5
        step = max(GridConfig.MIN_STEP_PCT, min(GridConfig.MAX_STEP_PCT, step))
        with self._lock:
            if abs(self._state.step_pct - step) >= 0.5:
                log.info("[Grid] 📐 Шаг скорректирован: %.1f%% → %.1f%% (ATR=%.2f%%)",
                         self._state.step_pct, step, atr_pct)
                self._state.step_pct = step
        return step

    # ── Фоновый цикл ─────────────────────────────────────────────────────────

    def _loop(self):
        log.info("[Grid] Фоновый цикл запущен")
        while self._running:
            try:
                self._tick()
            except Exception as e:
                log.error("[Grid] Ошибка тика: %s", e, exc_info=True)
            time.sleep(GridConfig.TICK_INTERVAL_SEC)

    def _tick(self):
        # Быстрая проверка без блокировки
        if not self._state.active:
            return
        if not self._dc:
            return

        # Получаем текущую цену (вне блокировки)
        try:
            from price_feed import price_feed
            price_ton = price_feed.get_grinch_ton_price()
            if not price_ton or price_ton <= 0:
                return
        except Exception as e:
            log.warning("[Grid] Ошибка получения цены: %s", e)
            return

        # AI-сигнал (необязательный, не блокирует выполнение)
        ai_buy_conf  = 0.0
        ai_sell_conf = 0.0
        try:
            ai_buy_conf, ai_sell_conf = self._get_ai_signal()
        except Exception:
            pass

        # ATR для динамического шага
        try:
            atr_pct = self._get_atr_pct()
            if atr_pct > 0:
                self.adjust_step_by_atr(atr_pct)
        except Exception:
            pass

        with self._lock:
            self._state.last_tick_ts = time.time()
            executed = False

            # ── Проверяем SELL-уровни (цена поднялась до уровня) ─────────
            for level in sorted(self._state.sell_levels,
                                key=lambda l: l.price_ton):
                if level.status != "waiting":
                    continue
                if price_ton < level.price_ton:
                    break   # уровни отсортированы вверх, дальше всё выше

                # AI-фильтр: пропустить продажу при сильном BUY
                if ai_buy_conf >= GridConfig.AI_SKIP_SELL_BUY_CONF:
                    log.info(
                        "[Grid] ⏭ SELL L%d @ %.6f TON пропущен — AI BUY %.0f%%",
                        level.id, level.price_ton, ai_buy_conf
                    )
                    level.status = "skipped_ai"
                    level.note   = f"AI BUY {ai_buy_conf:.0f}% — пропущено"
                    self._state.last_action = (
                        f"SELL L{level.id} пропущен (AI BUY {ai_buy_conf:.0f}%)"
                    )
                    continue

                if level.amount_grinch < 100:
                    level.status = "skipped_small"
                    continue

                log.info(
                    "[Grid] 🔴 SELL L%d: %.0f GRINCH @ %.6f TON/GRINCH (цена: %.6f)",
                    level.id, level.amount_grinch, level.price_ton, price_ton
                )
                res = self._execute_sell(level, price_ton)
                if res.get("ok"):
                    executed = True
                    break   # одна сделка за тик

            # ── Проверяем BUY-уровни (цена упала до уровня) ──────────────
            if not executed:
                for level in sorted(self._state.buy_levels,
                                    key=lambda l: l.price_ton, reverse=True):
                    if level.status != "waiting":
                        continue
                    if level.amount_ton < GridConfig.MIN_ORDER_TON:
                        continue
                    if price_ton > level.price_ton:
                        break   # отсортированы вниз, дальше всё ниже

                    # AI-фильтр: пропустить покупку при сильном SELL
                    if ai_sell_conf >= GridConfig.AI_SKIP_BUY_SELL_CONF:
                        log.info(
                            "[Grid] ⏭ BUY L%d @ %.6f TON пропущен — AI SELL %.0f%%",
                            level.id, level.price_ton, ai_sell_conf
                        )
                        continue

                    log.info(
                        "[Grid] 🟢 BUY L%d: %.2f TON @ %.6f TON/GRINCH (цена: %.6f)",
                        level.id, level.amount_ton, level.price_ton, price_ton
                    )
                    res = self._execute_buy(level, price_ton)
                    if res.get("ok"):
                        break

            if executed or True:   # всегда сохраняем (обновляем last_tick_ts)
                self._save_state()

    # ── Исполнение сделок ─────────────────────────────────────────────────────

    def _execute_sell(self, level: GridLevel, current_price: float) -> dict:
        """Продать GRINCH на уровне сетки."""
        try:
            # Минимальный нетто-TON: стоимость по центральной цене
            cost_ton = level.amount_grinch * self._state.center_price_ton
            # Не блокируем продажу min_net_ton — пусть AMM preflight в dedust решает
            result = self._dc.sell(level.amount_grinch)
            if result.get("ok"):
                # Оцениваем полученный TON (если dedust не вернул точную цифру)
                received_ton = result.get("received_ton") or (
                    level.amount_grinch * current_price * 0.99   # оценка с -1% slippage
                )
                gas_est  = 0.3
                net_ton  = received_ton - gas_est
                profit   = net_ton - cost_ton

                level.status        = "filled"
                level.filled_at     = time.time()
                level.fill_price_ton = current_price
                level.profit_ton    = round(profit, 4)
                level.tx_hash       = result.get("tx_hash", "")

                self._state.total_profit_ton  += level.profit_ton
                self._state.total_sell_cycles += 1
                self._state.last_action = (
                    f"✅ SELL L{level.id}: {level.amount_grinch:.0f} GRINCH "
                    f"@ {current_price:.6f} | нетто ≈ {net_ton:.2f} TON "
                    f"| прибыль {level.profit_ton:+.3f} TON"
                )
                log.info("[Grid] %s", self._state.last_action)

                # Реинвестируем: добавляем BUY-уровень ниже
                ton_to_reinvest = net_ton - GridConfig.GAS_RESERVE_TON
                if ton_to_reinvest >= GridConfig.MIN_ORDER_TON:
                    self._add_reinvestment_buy(ton_to_reinvest, current_price)
                return {"ok": True}
            else:
                err = result.get("error", "неизвестная ошибка")
                level.status = "error"
                level.note   = err[:120]
                log.warning("[Grid] ❌ SELL L%d провалилась: %s", level.id, err)
                return {"ok": False, "error": err}

        except Exception as e:
            level.status = "error"
            level.note   = str(e)[:120]
            log.error("[Grid] SELL L%d исключение: %s", level.id, e, exc_info=True)
            return {"ok": False, "error": str(e)}

    def _execute_buy(self, level: GridLevel, current_price: float) -> dict:
        """Купить GRINCH на уровне сетки."""
        try:
            result = self._dc.buy(level.amount_ton)
            if result.get("ok"):
                grinch_received = result.get("received_grinch") or (
                    level.amount_ton / current_price * 0.99
                )
                level.status         = "filled"
                level.filled_at      = time.time()
                level.fill_price_ton = current_price
                level.amount_grinch  = round(grinch_received, 2)
                level.tx_hash        = result.get("tx_hash", "")

                self._state.total_buy_cycles += 1
                self._state.last_action = (
                    f"✅ BUY L{level.id}: {level.amount_ton:.2f} TON "
                    f"→ {grinch_received:.0f} GRINCH @ {current_price:.6f}"
                )
                log.info("[Grid] %s", self._state.last_action)

                # Добавляем SELL-уровень выше для замыкания цикла
                self._add_cycle_sell(grinch_received, current_price)
                return {"ok": True}
            else:
                err = result.get("error", "неизвестная ошибка")
                level.status = "error"
                level.note   = err[:120]
                log.warning("[Grid] ❌ BUY L%d провалилась: %s", level.id, err)
                return {"ok": False, "error": err}

        except Exception as e:
            level.status = "error"
            level.note   = str(e)[:120]
            log.error("[Grid] BUY L%d исключение: %s", level.id, e, exc_info=True)
            return {"ok": False, "error": str(e)}

    # ── Динамическое добавление уровней (реинвестирование) ───────────────────

    def _add_reinvestment_buy(self, ton_amount: float, from_price: float):
        """После продажи: добавить BUY-уровень на шаг ниже."""
        buy_price = from_price / (1 + self._state.step_pct / 100)
        new_id    = -(100 + len(self._state.buy_levels))
        self._state.buy_levels.append(GridLevel(
            id=new_id, side="buy",
            price_ton=round(buy_price, 8),
            amount_grinch=0.0,
            amount_ton=round(ton_amount, 4),
            status="waiting",
            note=f"реинвест от SELL @ {from_price:.6f}",
        ))
        log.info("[Grid] 📥 Реинвест BUY @ %.6f с %.2f TON", buy_price, ton_amount)

    def _add_cycle_sell(self, grinch_amount: float, buy_price: float):
        """После покупки: добавить SELL-уровень на шаг выше."""
        sell_price = buy_price * (1 + self._state.step_pct / 100)
        new_id     = 100 + len(self._state.sell_levels)
        self._state.sell_levels.append(GridLevel(
            id=new_id, side="sell",
            price_ton=round(sell_price, 8),
            amount_grinch=round(grinch_amount * 0.98, 2),  # 2% запас на газ
            amount_ton=0.0,
            status="waiting",
            note=f"цикл от BUY @ {buy_price:.6f}",
        ))
        log.info("[Grid] 📤 Цикловый SELL @ %.6f с %.0f GRINCH", sell_price, grinch_amount)

    # ── AI-сигнал ─────────────────────────────────────────────────────────────

    def _get_ai_signal(self) -> tuple:
        """Возвращает (buy_conf%, sell_conf%) 0-100.
        Использует инжектированный AIEngine или brain_fusion."""
        try:
            # Приоритет: BrainFusion (консенсус нескольких моделей)
            from brain_fusion import BrainFusion
            bf = BrainFusion.get_instance() if hasattr(BrainFusion, "get_instance") else None
            if bf:
                state = bf.get_state()
                sig   = state.get("signal", "HOLD")
                conf  = float(state.get("confidence", 0)) * 100
                if sig == "BUY":
                    return conf, 0.0
                if sig == "SELL":
                    return 0.0, conf
                return 0.0, 0.0
        except Exception:
            pass

        # Fallback: инжектированный AIEngine
        try:
            if self._ai:
                from coin_info import coin_info
                candles = coin_info.get_candles(limit=50) if hasattr(coin_info, "get_candles") else []
                if candles and len(candles) >= 20:
                    res  = self._ai.analyze(candles)
                    sig  = res.get("ai_signal", "HOLD")
                    conf = float(res.get("confidence", 0))
                    if sig == "BUY":
                        return conf, 0.0
                    if sig == "SELL":
                        return 0.0, conf
        except Exception:
            pass

        return 0.0, 0.0

    def _get_atr_pct(self) -> float:
        """ATR последних 20 свечей (15m) в % для динамического шага."""
        try:
            from coin_info import coin_info
            candles = coin_info.get_candles(limit=25) if hasattr(coin_info, "get_candles") else []
            if not candles or len(candles) < 5:
                return 0.0
            last20 = candles[-20:]
            ranges = [(c[2] - c[3]) / c[3] * 100 for c in last20 if c[3] > 0]
            return sum(ranges) / len(ranges) if ranges else 0.0
        except Exception:
            return 0.0

    # ── Персистентность ───────────────────────────────────────────────────────

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._state.to_dict(), f, indent=2, ensure_ascii=False,
                          default=str)
        except Exception as e:
            log.warning("[Grid] Не удалось сохранить state: %s", e)

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, encoding="utf-8") as f:
                    self._state = GridState.from_dict(json.load(f))
                log.info("[Grid] Состояние загружено: active=%s, sell=%d, buy=%d",
                         self._state.active,
                         len(self._state.sell_levels),
                         len(self._state.buy_levels))
        except Exception as e:
            log.warning("[Grid] Загрузка состояния провалилась, начинаем чисто: %s", e)
            self._state = GridState()


# ─── Синглтон ────────────────────────────────────────────────────────────────

_grid_trader_instance: Optional[GridTrader] = None
_grid_init_lock = threading.Lock()


def get_grid_trader() -> GridTrader:
    global _grid_trader_instance
    if _grid_trader_instance is None:
        with _grid_init_lock:
            if _grid_trader_instance is None:
                _grid_trader_instance = GridTrader()
    return _grid_trader_instance
