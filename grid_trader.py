"""
grid_trader.py — AI-управляемая сеточная торговля GRINCH/TON на DeDust

Архитектура v2 (продвинутая):
  • Recovery-grid: продаём GRINCH траншами по мере роста цены
  • Compound-реинвест: прибыль от каждого SELL увеличивает размер следующего BUY
  • DCA-добавление позиции: GridAI рекомендует докупать GRINCH на откатах
  • AI-фильтр: BrainFusion управляет агрессивностью (trend/sideways/pump/distribution)
  • Авто-перецентровка: при уходе цены > RECENTER_STEPS шагов — тихо пересчитываем центр
  • GridAI обучается на каждом fill и улучшает параметры во время торговли
  • Profit-guard: каждая сделка проверяется на прибыльность (комиссия + газ)

Порог безубыточности:
  DeDust комиссия 1%×2 + газ 0.3 TON на ~44 TON/уровень ≈ 3.8% → MIN_STEP_PCT = 4.0%
"""

import os
import json
import math
import time
import threading
import logging
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import List, Optional

log = logging.getLogger("grid")

DATA_DIR   = os.getenv("DATA_DIR", ".")
STATE_FILE = os.path.join(DATA_DIR, "grid_state.json")


# ─── Конфигурация сетки ───────────────────────────────────────────────────────

class GridConfig:
    # Шаг по умолчанию (%)
    DEFAULT_STEP_PCT   = float(os.getenv("GRID_STEP_PCT",   "5.0"))
    # MIN_STEP_PCT = 4.0 — порог безубыточности при fee 2% + газ 0.3 TON ≈ 3.8%
    MIN_STEP_PCT       = float(os.getenv("GRID_MIN_STEP",   "4.0"))
    MAX_STEP_PCT       = float(os.getenv("GRID_MAX_STEP",   "10.0"))
    # Количество уровней
    SELL_LEVELS_COUNT  = int(os.getenv("GRID_SELL_LEVELS",  "9"))
    BUY_LEVELS_COUNT   = int(os.getenv("GRID_BUY_LEVELS",   "5"))
    # Минимальная стоимость ордера в TON (защита от пыли)
    MIN_ORDER_TON      = float(os.getenv("GRID_MIN_ORDER",  "15.0"))
    # Резерв TON на газ
    GAS_RESERVE_TON    = float(os.getenv("GRID_GAS_RESERVE","5.0"))
    # Комиссия DeDust (% от суммы, одна сторона)
    FEE_PCT            = 0.01   # 1% DeDust fee per side
    # Газ на одну сделку (TON)
    GAS_PER_TRADE_TON  = 0.30
    # ATR-пороги для heuristic-шага
    ATR_WIDE_PCT       = 5.0   # ATR > 5% → шаг 8%
    ATR_NORM_PCT       = 3.0   # ATR 3-5% → шаг 6%
    ATR_NARROW_PCT     = 2.0   # ATR 2-3% → шаг 5%
    # AI-пороги (% уверенности)
    AI_SKIP_SELL_BUY_CONF  = 75.0   # пропустить SELL если AI BUY ≥ 75%
    AI_SKIP_BUY_SELL_CONF  = 60.0   # пропустить BUY если AI SELL ≥ 60%
    AI_FREEZE_BUY_SELL     = 80.0   # заморозить все BUY если AI SELL ≥ 80%
    # ── Только-в-плюс под AI ──────────────────────────────────────────
    AI_MIN_BUY_CONF        = 55.0   # минимальный BUY-сигнал для открытия любой покупки
    AI_BUY_SIZE_MIN_MULT   = 0.7    # множитель суммы при AI BUY = AI_MIN_BUY_CONF
    AI_BUY_SIZE_MAX_MULT   = 1.8    # множитель суммы при AI BUY = 100%
    # DCA — добавление позиции вниз
    DCA_MIN_CONF       = 55.0   # мин. уверенность GridAI для DCA-закупки
    DCA_MAX_LEVELS     = 3      # макс. активных DCA-уровней одновременно
    DCA_STEP_MULT      = 1.5    # DCA-триггер ниже центра на step * 1.5
    # Compound реинвест
    COMPOUND_RATE      = 0.02   # +2% к размеру reinvest за каждый прибыльный цикл
    COMPOUND_MAX_MULT  = 2.0    # максимальный множитель размера
    # Авто-перецентровка
    RECENTER_STEPS     = 2.5    # шагов от центра до тихой перецентровки
    RECENTER_COOLDOWN  = 3600   # секунд между перецентровками
    # Период опроса цены
    TICK_INTERVAL_SEC  = int(os.getenv("GRID_TICK_SEC", "30"))
    # Интервал обновления GridAI-параметров (тиков)
    AI_TUNE_EVERY_N    = 10


# ─── AI-менеджер сетки ───────────────────────────────────────────────────────

class GridAIManager:
    """
    Полное AI-управление сеткой GRINCH/TON.

    Каждые AI_MANAGE_EVERY_N тиков (~2.5 мин при TICK=30s):
      • Авто-активация/деактивация по режиму рынка
      • Авто-перестройка при смене режима или исчерпании уровней
      • Динамический выбор шага и количества уровней по ATR×policy
      • Заморозка при сильном AI SELL-сигнале (≥80%)
    """

    # Политика для каждого режима рынка
    REGIME_POLICY: dict = {
        # режим         active  step_mult  levels  описание
        "SIDEWAYS":     {"active": True,  "step_mult": 0.85, "levels": 12,
                         "desc": "боковик — плотная сетка"},
        "MILD_TREND":   {"active": True,  "step_mult": 1.0,  "levels": 10,
                         "desc": "умеренный тренд"},
        "TREND":        {"active": True,  "step_mult": 1.3,  "levels": 8,
                         "desc": "тренд — широкий шаг"},
        "TREND_UP":     {"active": True,  "step_mult": 1.5,  "levels": 7,
                         "desc": "тренд вверх — меньше уровней"},
        "PUMP":         {"active": False, "step_mult": 2.0,  "levels": 5,
                         "desc": "памп — сетка на паузе"},
        "POST_PUMP":    {"active": False, "step_mult": 1.5,  "levels": 6,
                         "desc": "после пампа — пауза"},
        "DISTRIBUTION": {"active": False, "step_mult": 1.5,  "levels": 6,
                         "desc": "распределение — пауза"},
        "UNKNOWN":      {"active": True,  "step_mult": 1.0,  "levels": 10,
                         "desc": "неизвестный режим"},
    }

    # Тиков между AI-решениями
    AI_MANAGE_EVERY_N:    int   = 5
    # Перестроить если осталось < X% активных SELL-уровней
    REBUILD_SELL_THRESH:  float = 0.30
    # Не менять шаг если разница < X%
    STEP_CHANGE_MIN_DIFF: float = 0.5
    # Не перестраивать чаще чем раз в N секунд
    REBUILD_COOLDOWN:     int   = 1800

    def __init__(self, trader: "GridTrader"):
        self._trader              = trader
        self._last_regime:    str   = "UNKNOWN"
        self._last_rebuild_ts: float = 0.0
        self._paused_by_ai:   bool  = False
        self._decision_log:   list  = []   # последние 20 решений
        self._MAX_LOG:        int   = 20

    # ── Главный метод — вызывается каждый тик ────────────────────────────────

    def tick(self, regime: str, atr_pct: float,
             ai_buy_conf: float, ai_sell_conf: float,
             price_ton: float, grinch_balance: float, ton_balance: float):
        try:
            self._manage(regime, atr_pct, ai_buy_conf, ai_sell_conf,
                         price_ton, grinch_balance, ton_balance)
        except Exception as exc:
            log.warning("[GridAI-Mgr] ошибка: %s", exc)

    def _manage(self, regime, atr_pct, ai_buy_conf, ai_sell_conf,
                price_ton, grinch_balance, ton_balance):
        t = self._trader

        with t._lock:
            tick_n          = t._state.tick_count
            currently_active = t._state.active
            step_now        = t._state.step_pct
            center          = t._state.center_price_ton
            sell_levels     = list(t._state.sell_levels)

        # Только каждые N тиков
        if tick_n % self.AI_MANAGE_EVERY_N != 0:
            return

        policy = self.REGIME_POLICY.get(regime, self.REGIME_POLICY["UNKNOWN"])
        decisions: list = []

        # ── 1. Активация / деактивация ────────────────────────────────────
        should_active = policy["active"]

        # Сильный AI-SELL перекрывает "активен по режиму"
        if ai_sell_conf >= 80.0 and should_active:
            should_active = False
            decisions.append(f"⏸ AI SELL {ai_sell_conf:.0f}% → пауза")

        has_levels = bool(t._state.sell_levels or t._state.buy_levels)
        if should_active and not currently_active and (self._paused_by_ai or has_levels):
            # Режим восстановился — включаем обратно.
            # has_levels: сетка построена но ещё не активирована (напр. после ручного rebuild).
            t.activate()
            self._paused_by_ai = False
            decisions.append(f"▶️ авто-запуск (режим вернулся: {regime})")
        elif not should_active and currently_active:
            reason = f"AI-Mgr: {regime} | SELL={ai_sell_conf:.0f}%"
            t.deactivate(reason=reason)
            self._paused_by_ai = True
            decisions.append(f"⏸ авто-пауза ({policy['desc']})")

        # ── 2. Динамический шаг по ATR×policy ────────────────────────────
        if atr_pct > 0:
            raw_step   = atr_pct * policy["step_mult"]
            target_step = max(GridConfig.MIN_STEP_PCT,
                              min(GridConfig.MAX_STEP_PCT, raw_step))
            if abs(target_step - step_now) >= self.STEP_CHANGE_MIN_DIFF:
                with t._lock:
                    t._state.step_pct = target_step
                decisions.append(
                    f"📐 шаг {step_now:.1f}%→{target_step:.1f}% "
                    f"(ATR={atr_pct:.1f}%×{policy['step_mult']})")

        # ── 3. Авто-перестройка ───────────────────────────────────────────
        if grinch_balance > 1000 and price_ton > 0:
            now = time.time()
            rebuild_reason = self._need_rebuild(
                sell_levels, regime, now, ton_balance)

            if rebuild_reason:
                target_levels = policy["levels"]
                target_step   = t._state.step_pct
                log.info("[GridAI-Mgr] 🔨 Перестройка: %s | %d ур. шаг=%.1f%%",
                         rebuild_reason, target_levels, target_step)
                try:
                    res = t.build_grid(
                        current_price_ton=price_ton,
                        grinch_balance=grinch_balance,
                        ton_balance=ton_balance,
                        step_pct=target_step,
                        sell_levels=target_levels,
                    )
                    if res.get("ok") or res.get("sell_levels_total", 0) > 0:
                        t.activate()
                        self._last_rebuild_ts = now
                        decisions.append(
                            f"🔨 перестройка ({rebuild_reason}) "
                            f"→ {res.get('sell_levels_total', 0)} ур.")
                except Exception as exc:
                    log.warning("[GridAI-Mgr] ошибка перестройки: %s", exc)

        # ── Логируем решение ──────────────────────────────────────────────
        self._last_regime = regime
        if decisions:
            entry = {
                "ts":        time.time(),
                "regime":    regime,
                "atr_pct":   round(atr_pct, 2),
                "ai_buy":    round(ai_buy_conf, 1),
                "ai_sell":   round(ai_sell_conf, 1),
                "decisions": decisions,
                "desc":      policy["desc"],
            }
            self._decision_log.insert(0, entry)
            self._decision_log = self._decision_log[:self._MAX_LOG]
            log.info("[GridAI-Mgr] 🤖 %s | режим=%s ATR=%.1f%% "
                     "BUY=%.0f%% SELL=%.0f%%",
                     " | ".join(decisions), regime,
                     atr_pct, ai_buy_conf, ai_sell_conf)

    def _need_rebuild(self, sell_levels: list, regime: str, now: float,
                      ton_balance: float = 0.0) -> str:
        """Возвращает причину перестройки или ''."""

        # ── Без кулдауна: все BUY-уровни no_funds, но TON есть ──────────────
        # Проверяем при каждом вызове — кулдаун здесь не применяем,
        # иначе пополнение кошелька не активирует сетку 30 минут.
        with self._trader._lock:
            buy_levels_copy = list(self._trader._state.buy_levels)
        original_buys = [l for l in buy_levels_copy if -100 < l.id < 0]
        if original_buys:
            no_funds_buys = [l for l in original_buys if l.status == "no_funds"]
            if (len(no_funds_buys) == len(original_buys)
                    and ton_balance > GridConfig.MIN_ORDER_TON * len(original_buys)):
                return (f"все BUY-уровни no_funds, "
                        f"но TON={ton_balance:.1f} достаточно — активируем BUY")

        if now - self._last_rebuild_ts < self.REBUILD_COOLDOWN:
            return ""

        active_sells  = [l for l in sell_levels
                         if l.status not in ("skipped_ai", "error")]
        waiting_sells = [l for l in active_sells if l.status == "waiting"]

        # Все/почти все уровни исполнены
        if active_sells and len(waiting_sells) / len(active_sells) < self.REBUILD_SELL_THRESH:
            return f"осталось {len(waiting_sells)}/{len(active_sells)} SELL"

        # Смена режима (из неопасных в другой значимый)
        regime_changed = (
            regime != self._last_regime
            and self._last_regime not in ("UNKNOWN", "")
            and regime not in ("UNKNOWN",)
        )
        if regime_changed:
            # Перестраиваем только при значимой смене
            meaningful = {"SIDEWAYS", "TREND", "TREND_UP",
                          "PUMP", "POST_PUMP", "DISTRIBUTION"}
            if regime in meaningful and self._last_regime in meaningful:
                return f"смена режима {self._last_regime}→{regime}"

        return ""

    # ── Статус для API ────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        policy = self.REGIME_POLICY.get(
            self._last_regime, self.REGIME_POLICY["UNKNOWN"])
        return {
            "enabled":      True,
            "last_regime":  self._last_regime,
            "paused_by_ai": self._paused_by_ai,
            "policy":       policy,
            "decision_log": self._decision_log[:10],
            "rebuild_cooldown_left": max(
                0, int(self.REBUILD_COOLDOWN -
                        (time.time() - self._last_rebuild_ts))),
        }


# ─── Структуры данных ─────────────────────────────────────────────────────────

@dataclass
class GridLevel:
    id:             int
    side:           str     # 'sell' | 'buy' | 'dca'
    price_ton:      float   # цена-триггер (TON/GRINCH)
    amount_grinch:  float   # GRINCH на уровне (для sell/dca)
    amount_ton:     float   # TON на уровне (для buy/dca)
    status:         str     # 'waiting'|'filled'|'skipped_ai'|'skipped_dca'|'no_funds'|'error'
    filled_at:      float = 0.0
    fill_price_ton: float = 0.0
    profit_ton:     float = 0.0
    tx_hash:        str   = ""
    note:           str   = ""


@dataclass
class GridState:
    active:               bool  = False
    center_price_ton:     float = 0.0
    step_pct:             float = GridConfig.DEFAULT_STEP_PCT
    sell_levels:    List[GridLevel] = field(default_factory=list)
    buy_levels:     List[GridLevel] = field(default_factory=list)
    dca_levels:     List[GridLevel] = field(default_factory=list)   # DCA-добавления
    completed_fills:      List[GridLevel] = field(default_factory=list)  # все заполненные SELL, выживают при rebuild
    total_profit_ton:     float = 0.0
    total_sell_cycles:    int   = 0
    total_buy_cycles:     int   = 0
    total_dca_cycles:     int   = 0
    # Compound реинвест
    compound_multiplier:  float = 1.0   # растёт с каждым прибыльным SELL
    total_compound_bonus: float = 0.0   # доп. TON от compound-эффекта
    # Служебные поля
    created_at:           float = 0.0
    last_tick_ts:         float = 0.0
    last_recenter_ts:     float = 0.0
    last_action:          str   = ""
    paused_reason:        str   = ""
    tick_count:           int   = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sell_levels"]     = [asdict(l) for l in self.sell_levels]
        d["buy_levels"]      = [asdict(l) for l in self.buy_levels]
        d["dca_levels"]      = [asdict(l) for l in self.dca_levels]
        d["completed_fills"] = [asdict(l) for l in self.completed_fills]
        return d

    @staticmethod
    def from_dict(d: dict) -> "GridState":
        s = GridState()
        for k, v in d.items():
            if k == "sell_levels":
                s.sell_levels     = [GridLevel(**l) for l in (v or [])]
            elif k == "buy_levels":
                s.buy_levels      = [GridLevel(**l) for l in (v or [])]
            elif k == "dca_levels":
                s.dca_levels      = [GridLevel(**l) for l in (v or [])]
            elif k == "completed_fills":
                s.completed_fills = [GridLevel(**l) for l in (v or [])]
            else:
                try:
                    setattr(s, k, v)
                except Exception:
                    pass
        # Авто-миграция: если completed_fills пуст, но в sell_levels есть filled —
        # восстанавливаем из них, чтобы история не терялась после graceful shutdown.
        if not s.completed_fills:
            s.completed_fills = [
                l for l in s.sell_levels if l.status == "filled"
            ]
        return s


# ─── Основной класс ───────────────────────────────────────────────────────────

class GridTrader:
    """AI-управляемая сеточная торговля GRINCH/TON (v3 — pyramid + momentum + GridAI v3)."""

    def __init__(self):
        self._lock    = threading.RLock()
        self._state   = GridState()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._dc      = None    # DeDustClient
        self._ai      = None    # AIEngine
        self._grid_ai = None    # GridAI v3 (самообучающийся оптимизатор)
        self._ai_manager = GridAIManager(self)   # ← полное AI-управление

        # История цен для momentum (последние 20 тиков = ~10 мин при 30с)
        self._price_history: deque = deque(maxlen=20)
        # Momentum в % (обновляется каждый тик)
        self._price_momentum_pct: float = 0.0

        self._load_state()
        log.info("[Grid] Инициализирован v3. active=%s sell=%d buy=%d dca=%d "
                 "compound=%.2fx",
                 self._state.active,
                 len(self._state.sell_levels),
                 len(self._state.buy_levels),
                 len(self._state.dca_levels),
                 self._state.compound_multiplier)

    # ── Внешние зависимости ───────────────────────────────────────────────────

    def inject(self, dedust_client=None, ai_engine=None, grid_ai=None):
        """Инжектируем DeDust, AI-движок и GridAI-оптимизатор."""
        if dedust_client is not None:
            self._dc = dedust_client
            log.info("[Grid] DeDust-клиент подключён")
        if ai_engine is not None:
            self._ai = ai_engine
            log.info("[Grid] AI-движок подключён")
        if grid_ai is not None:
            self._grid_ai = grid_ai
            log.info("[Grid] GridAI-оптимизатор подключён (примеров: %d)",
                     len(grid_ai._experience))

    # ── Запуск фонового потока ────────────────────────────────────────────────

    def start_poller(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, name="grid-trader", daemon=True)
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
        """Построить сетку. Если GridAI обучен — шаг берётся от него."""
        sell_levels = sell_levels or GridConfig.SELL_LEVELS_COUNT
        buy_levels  = buy_levels  or GridConfig.BUY_LEVELS_COUNT

        # Шаг: ручной → GridAI → default
        if step_pct:
            step_pct = max(GridConfig.MIN_STEP_PCT,
                           min(GridConfig.MAX_STEP_PCT, step_pct))
        else:
            regime, atr_pct = self._get_regime()
            if self._grid_ai:
                step_pct = self._grid_ai.get_optimal_step(
                    atr_pct, regime,
                    GridConfig.MIN_STEP_PCT, GridConfig.MAX_STEP_PCT)
            else:
                step_pct = max(GridConfig.MIN_STEP_PCT,
                               min(GridConfig.MAX_STEP_PCT,
                                   self._heuristic_step(atr_pct, regime)))

        with self._lock:
            # Сохраняем compound_multiplier при перестройке
            old_mult      = self._state.compound_multiplier
            old_profit    = self._state.total_profit_ton
            old_sell_c    = self._state.total_sell_cycles
            old_buy_c     = self._state.total_buy_cycles
            old_dca_c     = self._state.total_dca_cycles
            old_cb        = self._state.total_compound_bonus
            old_completed = list(self._state.completed_fills)  # сохраняем историю через rebuild

            state = GridState()
            state.center_price_ton    = current_price_ton
            state.step_pct            = step_pct
            state.created_at          = time.time()
            state.compound_multiplier = old_mult
            state.total_profit_ton    = old_profit
            state.total_sell_cycles   = old_sell_c
            state.total_buy_cycles    = old_buy_c
            state.total_dca_cycles    = old_dca_c
            state.total_compound_bonus = old_cb
            state.completed_fills     = old_completed

            # ── SELL-уровни (пирамидальное распределение) ─────────────
            # GridAI v3: нижние уровни получают больше GRINCH → больше
            # прибыли при умеренном росте (пирамида весов 1.30→0.70)
            pyramid_weights = (
                self._grid_ai.get_pyramid_weights(sell_levels)
                if self._grid_ai else [1.0] * sell_levels
            )
            # Координация Grid ↔ DCA: отнимаем GRINCH, занятый открытыми
            # DCA-позициями. Сетка работает только со «свободным» балансом,
            # чтобы не продавать то, что DCA держит до своей TP.
            dca_reserved    = self._get_dca_reserved_grinch()
            free_grinch     = max(0.0, grinch_balance - dca_reserved)
            if dca_reserved > 0:
                log.info("[Grid] build_grid: кошелёк %.0f GRINCH, "
                         "DCA резерв %.0f, свободно %.0f",
                         grinch_balance, dca_reserved, free_grinch)
            base_grinch = free_grinch / sell_levels if sell_levels > 0 else 0
            grinch_per_level = base_grinch  # для обратной совместимости отчёта
            for i in range(1, sell_levels + 1):
                trigger = current_price_ton * (1 + step_pct / 100) ** i
                w = pyramid_weights[i - 1] if i - 1 < len(pyramid_weights) else 1.0
                amount = round(base_grinch * w, 2)
                if amount * trigger < GridConfig.MIN_ORDER_TON:
                    continue
                state.sell_levels.append(GridLevel(
                    id=i, side="sell",
                    price_ton=round(trigger, 8),
                    amount_grinch=amount,
                    amount_ton=0.0,
                    status="waiting",
                    note=(f"+{round((trigger/current_price_ton-1)*100, 1)}% от центра"
                          f" | вес×{w:.2f}"),
                ))

            # ── BUY-уровни ─────────────────────────────────────────────
            usable_ton    = max(0.0, ton_balance - GridConfig.GAS_RESERVE_TON)
            ton_per_level = usable_ton / buy_levels if buy_levels > 0 and usable_ton > 0 else 0
            for i in range(1, buy_levels + 1):
                trigger = current_price_ton / (1 + step_pct / 100) ** i
                st = "waiting" if ton_per_level >= GridConfig.MIN_ORDER_TON else "no_funds"
                state.buy_levels.append(GridLevel(
                    id=-i, side="buy",
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
        log.info("[Grid] Сетка v2 построена: %d sell + %d buy, шаг=%.1f%%, "
                 "compound=%.2fx", sell_ok, buy_ok, step_pct, old_mult)
        return {
            "ok": True,
            "sell_levels_total":  len(state.sell_levels),
            "sell_levels_active": sell_ok,
            "buy_levels_total":   len(state.buy_levels),
            "buy_levels_active":  buy_ok,
            "step_pct":           step_pct,
            "center_price_ton":   current_price_ton,
            "compound_multiplier": old_mult,
            "grinch_per_sell_level": round(grinch_per_level, 0),
            "ton_per_buy_level":  round(ton_per_level, 4),
        }

    def activate(self) -> dict:
        with self._lock:
            if not self._state.sell_levels and not self._state.buy_levels:
                return {"ok": False, "error": "Сначала постройте сетку через /api/grid/build"}
            self._state.active        = True
            self._state.paused_reason = ""
            self._save_state()
            log.info("[Grid] ✅ Активирована")
            return {"ok": True, "message": "Grid v2 активирована"}

    def deactivate(self, reason: str = "manual") -> dict:
        with self._lock:
            self._state.active        = False
            self._state.paused_reason = reason
            self._save_state()
            log.info("[Grid] ⏹ Остановлена: %s", reason)
            return {"ok": True, "message": f"Grid остановлена: {reason}"}

    def reset_error_levels(self, level_ids: list = None) -> dict:
        """Сбросить error-уровни обратно в waiting.

        level_ids — список id для сброса, или None/[] → сбросить все error-уровни.
        Уровни с amount_grinch == 0 (SELL) / amount_ton == 0 (BUY) будут помечены
        skipped_small — они не могут торговать, но исчезнут из error-статуса.
        """
        with self._lock:
            s = self._state
            all_levels = s.sell_levels + s.buy_levels + s.dca_levels
            targets = [
                l for l in all_levels
                if l.status == "error"
                and (not level_ids or l.id in level_ids)
            ]
            if not targets:
                return {"ok": False, "error": "Нет уровней со статусом error"}

            reset_ids, skipped_ids = [], []
            for l in targets:
                # Уровень без GRINCH/TON не сможет торговать — пометить skipped_small
                if l.side in ("sell", "dca") and (l.amount_grinch or 0) <= 0:
                    l.status = "skipped_small"
                    l.note   = "Нет GRINCH для продажи (авто-скип)"
                    skipped_ids.append(l.id)
                elif l.side == "buy" and (l.amount_ton or 0) <= 0:
                    l.status = "skipped_small"
                    l.note   = "Нет TON для покупки (авто-скип)"
                    skipped_ids.append(l.id)
                else:
                    l.status = "waiting"
                    l.note   = ""
                    reset_ids.append(l.id)

            self._save_state()
            log.info("[Grid] reset_error_levels: waiting=%s skipped_small=%s",
                     reset_ids, skipped_ids)
            return {
                "ok":      True,
                "reset":   reset_ids,
                "skipped": skipped_ids,
                "message": (f"Сброшено в waiting: {reset_ids}; "
                            f"помечено skipped_small (нет баланса): {skipped_ids}"),
            }

    def get_status(self) -> dict:
        """Полный статус для /api/grid/status, включая GridAI-статистику."""
        with self._lock:
            s = self._state
            sell_waiting = [l for l in s.sell_levels if l.status == "waiting"]
            sell_filled  = [l for l in s.sell_levels if l.status == "filled"]
            buy_waiting  = [l for l in s.buy_levels  if l.status == "waiting"]
            buy_filled   = [l for l in s.buy_levels  if l.status == "filled"]
            dca_waiting  = [l for l in s.dca_levels  if l.status == "waiting"]
            dca_filled   = [l for l in s.dca_levels  if l.status == "filled"]

            next_sell = min(sell_waiting, key=lambda l: l.price_ton, default=None)
            next_buy  = max(buy_waiting,  key=lambda l: l.price_ton, default=None)
            next_dca  = max(dca_waiting,  key=lambda l: l.price_ton, default=None)

            ai_stats = {}
            if self._grid_ai:
                try:
                    ai_stats = self._grid_ai.get_stats()
                except Exception:
                    pass

            return {
                "active":             s.active,
                "paused_reason":      s.paused_reason,
                "center_price_ton":   s.center_price_ton,
                "step_pct":           s.step_pct,
                "total_profit_ton":   round(s.total_profit_ton, 4),
                "total_sell_cycles":  s.total_sell_cycles,
                "total_buy_cycles":   s.total_buy_cycles,
                "total_dca_cycles":   s.total_dca_cycles,
                "compound_multiplier": round(s.compound_multiplier, 3),
                "compound_bonus_ton": round(s.total_compound_bonus, 4),
                "last_tick":          s.last_tick_ts,
                "last_action":        s.last_action,
                "grid_ai":            ai_stats,
                "ai_manager":         self._ai_manager.get_status(),
                "sell": {
                    "total":   len(s.sell_levels),
                    "waiting": len(sell_waiting),
                    "filled":  len(sell_filled),
                    "next_price_ton":  next_sell.price_ton if next_sell else None,
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
                "dca": {
                    "total":   len(s.dca_levels),
                    "waiting": len(dca_waiting),
                    "filled":  len(dca_filled),
                    "next_price_ton": next_dca.price_ton if next_dca else None,
                },
                "sell_levels": [
                    {"id": l.id, "price_ton": l.price_ton,
                     "amount_grinch": l.amount_grinch, "status": l.status,
                     "profit_ton": round(l.profit_ton, 4),
                     "note": l.note, "filled_at": l.filled_at}
                    for l in s.sell_levels
                ],
                "completed_fills": [
                    {"id": l.id, "price_ton": l.price_ton,
                     "amount_grinch": l.amount_grinch,
                     "profit_ton": round(l.profit_ton, 4),
                     "fill_price_ton": l.fill_price_ton,
                     "note": l.note, "filled_at": l.filled_at}
                    for l in sorted(s.completed_fills, key=lambda x: x.filled_at or 0, reverse=True)
                ],
                "buy_levels": [
                    {"id": l.id, "price_ton": l.price_ton,
                     "amount_ton": l.amount_ton, "status": l.status,
                     "note": l.note, "filled_at": l.filled_at}
                    for l in s.buy_levels
                ],
                "dca_levels": [
                    {"id": l.id, "price_ton": l.price_ton,
                     "amount_ton": l.amount_ton, "status": l.status,
                     "note": l.note, "filled_at": l.filled_at,
                     "profit_ton": round(l.profit_ton, 4)}
                    for l in s.dca_levels
                ],
            }

    def adjust_step_by_atr(self, atr_pct: float, regime: str = "UNKNOWN") -> float:
        """Корректирует шаг сетки: GridAI → heuristic → MIN_STEP."""
        if self._grid_ai:
            step = self._grid_ai.get_optimal_step(
                atr_pct, regime,
                GridConfig.MIN_STEP_PCT, GridConfig.MAX_STEP_PCT)
        else:
            step = self._heuristic_step(atr_pct, regime)
            step = max(GridConfig.MIN_STEP_PCT, min(GridConfig.MAX_STEP_PCT, step))

        with self._lock:
            if abs(self._state.step_pct - step) >= 0.5:
                log.info("[Grid] 📐 Шаг: %.1f%% → %.1f%% (ATR=%.2f%% режим=%s)",
                         self._state.step_pct, step, atr_pct, regime)
                self._state.step_pct = step
        return step

    # ── Фоновый цикл ─────────────────────────────────────────────────────────

    def _loop(self):
        log.info("[Grid] Фоновый цикл v2 запущен")
        while self._running:
            try:
                self._tick()
            except Exception as e:
                log.error("[Grid] Ошибка тика: %s", e, exc_info=True)
            time.sleep(GridConfig.TICK_INTERVAL_SEC)

    def _tick(self):
        # Без DeDust-клиента совсем ничего не делаем
        if not self._dc:
            return

        # ── Получаем цену (нужна и AI-менеджеру, и торговле) ─────────────
        try:
            from price_feed import price_feed
            price_ton = price_feed.get_grinch_ton_price()
            if not price_ton or price_ton <= 0:
                # Fallback: кросс-курс из USD-цен (если on-chain недоступен)
                usd_g = price_feed.get('GRINCH')
                usd_t = price_feed.get('TON')
                if usd_g and usd_t and usd_t > 0:
                    price_ton = usd_g / usd_t
                    log.info("[Grid] ⚠️ Цена из USD кросс-курса: %.8f TON/GRINCH "
                             "(on-chain недоступен)", price_ton)
                else:
                    log.warning("[Grid] ❌ Нет цены TON/GRINCH — пропуск тика")
                    return
        except Exception as e:
            log.warning("[Grid] Цена: %s", e)
            return

        # ── Режим рынка + ATR ─────────────────────────────────────────────
        regime, atr_pct = self._get_regime()

        # ── Ценовой буфер и momentum ──────────────────────────────────────
        self._price_history.append(price_ton)
        self._price_momentum_pct = self._calc_price_momentum()

        # ── Обновляем счётчик режима в GridAI ────────────────────────────
        if self._grid_ai:
            try:
                self._grid_ai.update_regime(regime)
            except Exception:
                pass

        # ── AI-сигнал (BrainFusion) ───────────────────────────────────────
        ai_buy_conf, ai_sell_conf = 0.0, 0.0
        try:
            ai_buy_conf, ai_sell_conf = self._get_ai_signal()
        except Exception:
            pass

        # ── GridAI-шаг (каждые N тиков) ──────────────────────────────────
        with self._lock:
            self._state.tick_count += 1
            tick_n = self._state.tick_count
        if tick_n % GridConfig.AI_TUNE_EVERY_N == 0 and atr_pct > 0:
            self.adjust_step_by_atr(atr_pct, regime)

        # ── GridAIManager — полное AI-управление сеткой ───────────────────
        # ВАЖНО: вызываем до проверки active, чтобы AI-менеджер мог
        # авто-активировать сетку даже когда та неактивна (напр. после rebuild).
        grinch_bal, ton_bal = self._get_balances()
        self._ai_manager.tick(
            regime, atr_pct, ai_buy_conf, ai_sell_conf,
            price_ton, grinch_bal, ton_bal)

        # ── Если сетка неактивна — дальше не идём (торговля заморожена) ───
        if not self._state.active:
            return

        # ── Заморозка BUY при сильном SELL-сигнале ────────────────────────
        buy_frozen = ai_sell_conf >= GridConfig.AI_FREEZE_BUY_SELL
        if buy_frozen:
            log.info("[Grid] 🧊 BUY заморожены — AI SELL %.0f%%", ai_sell_conf)

        # ── Авто-перецентровка ────────────────────────────────────────────
        try:
            self._maybe_recenter(price_ton, atr_pct, regime)
        except Exception as e:
            log.warning("[Grid] Recenter error: %s", e)

        with self._lock:
            self._state.last_tick_ts = time.time()
            executed = False

            # ── Восстановление skipped_ai / skipped_dca уровней ──────────
            # skipped_ai ставится в двух случаях:
            #   a) AI BUY ≥ 75% (не хотим мешать росту)
            #   b) _maybe_recenter (уровень оказался ниже нового центра)
            # skipped_dca — в _execute_sell при нехватке свободного GRINCH
            #   (кошелёк занят DCA-позицией). Восстанавливается автоматически
            #   когда DCA закрывает позицию и GRINCH освобождается.
            # В обоих случаях состояние обратимо:
            #   • Если цена откатилась ниже триггера → возвращаем waiting.
            #   • Если цена ≥ триггера, но блокировщик снят → тоже waiting.
            restored_n = 0
            for _lv in self._state.sell_levels:
                if _lv.status not in ("skipped_ai", "skipped_dca"):
                    continue
                # ⚠️ Никогда не восстанавливаем уровни ниже центра — они
                # заведомо убыточны (profit-guard всё равно заблокирует) и
                # создают мусорные записи в last_action каждый тик.
                if _lv.price_ton <= self._state.center_price_ton:
                    continue
                price_below_trigger = price_ton < _lv.price_ton

                if _lv.status == "skipped_ai":
                    ai_no_longer_blocks = (price_ton >= _lv.price_ton
                                           and ai_buy_conf < GridConfig.AI_SKIP_SELL_BUY_CONF)
                    should_restore = price_below_trigger or ai_no_longer_blocks
                    reason = (
                        f"↩ восст. (откат {price_ton:.6f}<{_lv.price_ton:.6f})"
                        if price_below_trigger
                        else f"↩ восст. (AI BUY {ai_buy_conf:.0f}%<{GridConfig.AI_SKIP_SELL_BUY_CONF:.0f}%)"
                    )
                else:  # skipped_dca
                    # Восстанавливаем: a) откат — уровень ещё не достигнут,
                    #                  b) DCA закрылась — свободный GRINCH появился
                    try:
                        _reserved = self._get_dca_reserved_grinch()
                        from dedust_client import get_shared_balance as _gsb2
                        _w2 = float(_gsb2().get("GRINCH", 0))
                        _free2 = max(0.0, _w2 - _reserved)
                        dca_freed = _free2 >= _lv.amount_grinch
                    except Exception:
                        dca_freed = False
                    should_restore = price_below_trigger or dca_freed
                    reason = (
                        f"↩ восст. (откат {price_ton:.6f}<{_lv.price_ton:.6f})"
                        if price_below_trigger
                        else f"↩ восст. (DCA свободно ≥{_lv.amount_grinch:.0f} GRINCH)"
                    )

                if should_restore:
                    _lv.status = "waiting"
                    _lv.note   = reason
                    restored_n += 1
            if restored_n:
                log.info("[Grid] ♻️ Восстановлено %d SELL → waiting", restored_n)

            # ── SELL-уровни ───────────────────────────────────────────────
            for level in sorted(self._state.sell_levels, key=lambda l: l.price_ton):
                if level.status != "waiting":
                    continue
                if price_ton < level.price_ton:
                    break

                # Уровень ниже или на уровне центра — заведомо убыточен:
                # сдвигаем в skipped_ai без логирования каждый тик.
                if level.price_ton <= self._state.center_price_ton:
                    level.status = "skipped_ai"
                    level.note   = (f"ниже нового центра "
                                    f"{self._state.center_price_ton:.6f}")
                    log.info("[Grid] ⏩ SELL L%d @ %.6f → skipped (ниже центра %.6f)",
                             level.id, level.price_ton, self._state.center_price_ton)
                    continue

                # AI BUY-фильтр: не мешаем росту
                if ai_buy_conf >= GridConfig.AI_SKIP_SELL_BUY_CONF:
                    log.info("[Grid] ⏭ SELL L%d @ %.6f — AI BUY %.0f%%",
                             level.id, level.price_ton, ai_buy_conf)
                    level.status = "skipped_ai"
                    level.note   = f"AI BUY {ai_buy_conf:.0f}% — пропущено"
                    self._state.last_action = f"SELL L{level.id} пропущен (AI BUY {ai_buy_conf:.0f}%)"
                    continue

                if level.amount_grinch < 100:
                    level.status = "skipped_small"
                    continue

                # Profit-guard
                profitable, profit_est = self._is_profitable_sell(level, price_ton)
                if not profitable:
                    log.info("[Grid] ⚠️ SELL L%d @ %.6f — убыточно (est %+.4f TON)",
                             level.id, level.price_ton, profit_est)
                    self._state.last_action = (
                        f"SELL L{level.id} пропущен (убыточно {profit_est:+.4f} TON)")
                    continue

                log.info("[Grid] 🔴 SELL L%d: %.0f GRINCH @ %.6f (цена: %.6f)",
                         level.id, level.amount_grinch, level.price_ton, price_ton)
                res = self._execute_sell(level, price_ton, atr_pct, regime)
                if res.get("ok"):
                    executed = True
                    break

            # ── BUY-уровни ────────────────────────────────────────────────
            if not executed and not buy_frozen:
                for level in sorted(self._state.buy_levels,
                                    key=lambda l: l.price_ton, reverse=True):
                    if level.status != "waiting":
                        continue
                    if level.amount_ton < GridConfig.MIN_ORDER_TON:
                        continue
                    if price_ton > level.price_ton:
                        break

                    # ── Стоп: сильный AI SELL-сигнал ─────────────────────
                    if ai_sell_conf >= GridConfig.AI_SKIP_BUY_SELL_CONF:
                        log.info("[Grid] ⏭ BUY L%d @ %.6f — AI SELL %.0f%% → стоп",
                                 level.id, level.price_ton, ai_sell_conf)
                        continue

                    # ── ТОЛЬКО-В-ПЛЮС: главный гейт — математика цикла ──
                    # BUY выполняется только если BUY→SELL цикл прибылен.
                    # AI влияет лишь на размер ордера, не блокирует.
                    profitable, profit_est = self._is_profitable_buy_cycle(level)
                    if not profitable:
                        log.info("[Grid] ⚠️ BUY L%d @ %.6f — цикл убыточен (est %+.4f TON)",
                                 level.id, level.price_ton, profit_est)
                        self._state.last_action = (
                            f"BUY L{level.id} пропущен (цикл убыточен {profit_est:+.4f} TON)")
                        continue

                    # ── AI масштабирует размер (0.7x–1.8x), не блокирует ─
                    if ai_buy_conf >= GridConfig.AI_MIN_BUY_CONF:
                        ai_size_mult = GridConfig.AI_BUY_SIZE_MIN_MULT + (
                            (ai_buy_conf - GridConfig.AI_MIN_BUY_CONF) /
                            max(1.0, 100.0 - GridConfig.AI_MIN_BUY_CONF)
                        ) * (GridConfig.AI_BUY_SIZE_MAX_MULT - GridConfig.AI_BUY_SIZE_MIN_MULT)
                    else:
                        # Слабый AI-сигнал → минимальный размер (осторожный вход)
                        ai_size_mult = GridConfig.AI_BUY_SIZE_MIN_MULT
                        log.info("[Grid] 📉 BUY L%d — AI слабый (%.0f%%), вход мин. ×%.2f (прибыль est +%.4f TON)",
                                 level.id, ai_buy_conf, ai_size_mult, profit_est)
                    ai_size_mult = round(
                        max(GridConfig.AI_BUY_SIZE_MIN_MULT,
                            min(GridConfig.AI_BUY_SIZE_MAX_MULT, ai_size_mult)), 3)
                    scaled_ton   = round(level.amount_ton * ai_size_mult, 4)
                    orig_ton     = level.amount_ton

                    log.info("[Grid] 🟢 BUY L%d: %.2f TON (×%.2f AI=%.0f%%) @ %.6f (цена: %.6f)",
                             level.id, scaled_ton, ai_size_mult, ai_buy_conf,
                             level.price_ton, price_ton)
                    level.amount_ton = scaled_ton  # временно, для исполнения
                    level.note = (level.note or "") + \
                        f" | AI BUY {ai_buy_conf:.0f}% × {ai_size_mult:.2f}"
                    res = self._execute_buy(level, price_ton, atr_pct, regime)
                    if not res.get("ok"):
                        level.amount_ton = orig_ton   # откатить если ошибка
                    if res.get("ok"):
                        executed = True
                        break

            # ── DCA-уровни (добавление позиции) ──────────────────────────
            if not executed and not buy_frozen:
                for level in sorted(self._state.dca_levels,
                                    key=lambda l: l.price_ton, reverse=True):
                    if level.status != "waiting":
                        continue
                    if level.amount_ton < GridConfig.MIN_ORDER_TON:
                        continue
                    if price_ton > level.price_ton:
                        break

                    # ── Стоп: сильный AI SELL ────────────────────────────
                    if ai_sell_conf >= GridConfig.AI_SKIP_BUY_SELL_CONF:
                        continue

                    # ── ТОЛЬКО-В-ПЛЮС: DCA — математика цикла ───────────
                    profitable, profit_est = self._is_profitable_buy_cycle(level)
                    if not profitable:
                        continue


                    # Масштабируем размер DCA по AI-уверенности (не блокируем)
                    if ai_buy_conf >= GridConfig.AI_MIN_BUY_CONF:
                        ai_size_mult = GridConfig.AI_BUY_SIZE_MIN_MULT + (
                            (ai_buy_conf - GridConfig.AI_MIN_BUY_CONF) /
                            max(1.0, 100.0 - GridConfig.AI_MIN_BUY_CONF)
                        ) * (GridConfig.AI_BUY_SIZE_MAX_MULT - GridConfig.AI_BUY_SIZE_MIN_MULT)
                    else:
                        ai_size_mult = GridConfig.AI_BUY_SIZE_MIN_MULT
                    ai_size_mult = round(
                        max(GridConfig.AI_BUY_SIZE_MIN_MULT,
                            min(GridConfig.AI_BUY_SIZE_MAX_MULT, ai_size_mult)), 3)
                    orig_ton      = level.amount_ton
                    level.amount_ton = round(orig_ton * ai_size_mult, 4)

                    log.info("[Grid] 🟣 DCA: %.2f TON (×%.2f AI=%.0f%%) @ %.6f (цена: %.6f)",
                             level.amount_ton, ai_size_mult, ai_buy_conf,
                             level.price_ton, price_ton)
                    res = self._execute_dca(level, price_ton, atr_pct, regime)
                    if not res.get("ok"):
                        level.amount_ton = orig_ton
                    if res.get("ok"):
                        executed = True
                        break

            # ── Добавить новый DCA-уровень если нужно ─────────────────────
            if not executed and not buy_frozen:
                try:
                    self._maybe_add_dca_level(price_ton, atr_pct, regime, ai_buy_conf)
                except Exception as e:
                    log.warning("[Grid] DCA-level error: %s", e)

            self._save_state()

    # ── Исполнение сделок ─────────────────────────────────────────────────────

    def _execute_sell(self, level: GridLevel, current_price: float,
                      atr_pct: float = 0.0, regime: str = "UNKNOWN") -> dict:
        """Продать GRINCH. После успеха — compound-реинвест + обучение GridAI."""
        try:
            # Используем ту же логику cost_ton, что и в _is_profitable_sell
            if level.amount_ton > 0:
                cost_ton = level.amount_ton
            else:
                step = self._state.step_pct or GridConfig.DEFAULT_STEP_PCT
                cost_ton = level.amount_grinch * (level.price_ton / (1 + step / 100))

            # ── Координация Grid ↔ DCA: runtime-guard ─────────────────────
            # Проверяем прямо перед свопом: не залезаем ли в GRINCH DCA.
            # DCA reserved читаем из open_trades (актуально всегда).
            try:
                from dedust_client import get_shared_balance as _gsb
                _bal      = _gsb()
                _wallet_g = float(_bal.get("GRINCH", _bal.get("grinch", 0)))
                _reserved = self._get_dca_reserved_grinch()
                _free_g   = max(0.0, _wallet_g - _reserved)
                if _free_g < level.amount_grinch:
                    log.warning(
                        "[Grid] ⛔ SELL L%d заблокирован: "
                        "свободно %.0f GRINCH (кошелёк %.0f − DCA %.0f), "
                        "нужно %.0f — пропуск",
                        level.id, _free_g, _wallet_g, _reserved, level.amount_grinch)
                    level.status = "skipped_dca"
                    level.note   = (f"DCA резерв: свободно {_free_g:.0f} "
                                    f"< нужно {level.amount_grinch:.0f}")
                    return {"ok": False, "error": "dca_reserved"}
            except Exception as _ge:
                log.debug("[Grid] DCA-guard check error: %s", _ge)

            result   = self._dc.sell(level.amount_grinch)
            if result.get("ok"):
                received_ton = result.get("received_ton") or (
                    level.amount_grinch * current_price * (1 - GridConfig.FEE_PCT))
                net_ton  = received_ton - GridConfig.GAS_PER_TRADE_TON
                profit   = net_ton - cost_ton
                profit_pct = (profit / cost_ton * 100) if cost_ton > 0 else 0.0

                level.status         = "filled"
                level.filled_at      = time.time()
                level.fill_price_ton = current_price
                level.profit_ton     = round(profit, 4)
                level.tx_hash        = result.get("tx_hash", "")

                self._state.total_profit_ton  += level.profit_ton
                self._state.total_sell_cycles += 1
                # Сохраняем в completed_fills — выживает при rebuild
                import copy as _copy
                self._state.completed_fills.append(_copy.copy(level))
                self._state.last_action = (
                    f"✅ SELL L{level.id}: {level.amount_grinch:.0f} GRINCH "
                    f"@ {current_price:.6f} | нетто ≈ {net_ton:.2f} TON "
                    f"| прибыль {level.profit_ton:+.3f} TON")
                log.info("[Grid] %s", self._state.last_action)

                # ── Compound-реинвест ──────────────────────────────────
                if profit > 0:
                    old_mult = self._state.compound_multiplier
                    new_mult = min(
                        GridConfig.COMPOUND_MAX_MULT,
                        old_mult + GridConfig.COMPOUND_RATE)
                    self._state.compound_multiplier = new_mult
                    # Точный расчёт: бонус = РЕАЛЬНО реинвестированная сумма сверх
                    # чистых поступлений (за вычетом газового резерва).
                    # Было: net_ton * (mult-1) — чуть завышало на GAS_RESERVE*(mult-1)
                    bonus = max(0.0, (net_ton - GridConfig.GAS_RESERVE_TON) * (new_mult - 1.0))
                    self._state.total_compound_bonus += bonus
                    log.info("[Grid] 📈 Compound: %.2fx → %.2fx (+%.4f TON bonus)",
                             old_mult, new_mult, bonus)

                ton_to_reinvest = (net_ton - GridConfig.GAS_RESERVE_TON) * \
                                  self._state.compound_multiplier
                if ton_to_reinvest >= GridConfig.MIN_ORDER_TON:
                    self._add_reinvestment_buy(ton_to_reinvest, current_price)

                # ── Обучаем GridAI ─────────────────────────────────────
                if self._grid_ai:
                    try:
                        self._grid_ai.record_fill(
                            "sell", self._state.step_pct,
                            atr_pct, regime, profit, profit_pct)
                    except Exception:
                        pass

                # ── Сохраняем в bot_trades для истории P&L ─────────────
                try:
                    import db_store as _db
                    _db.trades_upsert({
                        "id":          f"grid_sell_{int(level.filled_at)}",
                        "side":        "sell",
                        "status":      "closed",
                        "source":      "grid",
                        "symbol":      "GRINCH/TON",
                        "stake_ton":   round(cost_ton, 4),
                        "amount":      round(level.amount_grinch, 2),
                        "open_price":  round(
                            level.price_ton / (1 + profit_pct / 100)
                            if profit_pct else level.price_ton, 8),
                        "close_price": round(current_price, 8),
                        "entry_price": round(level.price_ton, 8),
                        "exit_price":  round(current_price, 8),
                        "profit_ton":  round(profit, 4),
                        "profit_pct":  round(profit_pct, 4),
                        "step_pct":    self._state.step_pct,
                        "regime":      regime,
                        "tx_hash":     level.tx_hash,
                        "closed_at":   time.strftime(
                            "%Y-%m-%dT%H:%M:%S", time.gmtime(level.filled_at)),
                    })
                except Exception as _e:
                    log.debug("[Grid] bot_trades save error: %s", _e)

                return {"ok": True}
            else:
                err = result.get("error", "неизвестная ошибка")
                level.status = "error"
                level.note   = err[:120]
                log.warning("[Grid] ❌ SELL L%d: %s", level.id, err)
                return {"ok": False, "error": err}

        except Exception as e:
            level.status = "error"
            level.note   = str(e)[:120]
            log.error("[Grid] SELL L%d исключение: %s", level.id, e, exc_info=True)
            return {"ok": False, "error": str(e)}

    def _execute_buy(self, level: GridLevel, current_price: float,
                     atr_pct: float = 0.0, regime: str = "UNKNOWN") -> dict:
        """Купить GRINCH. После успеха — SELL-уровень + обучение GridAI."""
        try:
            result = self._dc.buy(level.amount_ton)
            if result.get("ok"):
                # FIX#10: guard against ZeroDivisionError when current_price=0
                _fb = (level.amount_ton / current_price * (1 - GridConfig.FEE_PCT)
                       if current_price > 0 else 0.0)
                grinch_received = result.get("received_grinch") or _fb
                level.status         = "filled"
                level.filled_at      = time.time()
                level.fill_price_ton = current_price
                level.amount_grinch  = round(grinch_received, 2)
                level.tx_hash        = result.get("tx_hash", "")

                self._state.total_buy_cycles += 1
                self._state.last_action = (
                    f"✅ BUY L{level.id}: {level.amount_ton:.2f} TON "
                    f"→ {grinch_received:.0f} GRINCH @ {current_price:.6f}")
                log.info("[Grid] %s", self._state.last_action)

                self._add_cycle_sell(grinch_received, current_price)

                if self._grid_ai:
                    try:
                        self._grid_ai.record_fill(
                            "buy", self._state.step_pct,
                            atr_pct, regime, 0.0, 0.0)
                    except Exception:
                        pass

                return {"ok": True}
            else:
                err = result.get("error", "неизвестная ошибка")
                level.status = "error"
                level.note   = err[:120]
                log.warning("[Grid] ❌ BUY L%d: %s", level.id, err)
                return {"ok": False, "error": err}

        except Exception as e:
            level.status = "error"
            level.note   = str(e)[:120]
            log.error("[Grid] BUY L%d исключение: %s", level.id, e, exc_info=True)
            return {"ok": False, "error": str(e)}

    def _execute_dca(self, level: GridLevel, current_price: float,
                     atr_pct: float = 0.0, regime: str = "UNKNOWN") -> dict:
        """DCA-добавление позиции. Аналог BUY, но учитывается отдельно."""
        try:
            result = self._dc.buy(level.amount_ton)
            if result.get("ok"):
                # FIX#10: guard against ZeroDivisionError when current_price=0
                _fb = (level.amount_ton / current_price * (1 - GridConfig.FEE_PCT)
                       if current_price > 0 else 0.0)
                grinch_received = result.get("received_grinch") or _fb
                level.status         = "filled"
                level.filled_at      = time.time()
                level.fill_price_ton = current_price
                level.amount_grinch  = round(grinch_received, 2)
                level.tx_hash        = result.get("tx_hash", "")

                self._state.total_dca_cycles += 1
                self._state.last_action = (
                    f"✅ DCA: {level.amount_ton:.2f} TON "
                    f"→ {grinch_received:.0f} GRINCH @ {current_price:.6f} "
                    f"(всего DCA: {self._state.total_dca_cycles})")
                log.info("[Grid] %s", self._state.last_action)

                # SELL-уровень для закрытия DCA позиции
                self._add_cycle_sell(grinch_received, current_price,
                                     note=f"DCA-цикл от {current_price:.6f}")

                if self._grid_ai:
                    try:
                        self._grid_ai.record_fill(
                            "buy", self._state.step_pct,
                            atr_pct, regime, 0.0, 0.0, is_dca=True)
                    except Exception:
                        pass

                return {"ok": True}
            else:
                err = result.get("error", "неизвестная ошибка")
                level.status = "error"
                level.note   = err[:120]
                log.warning("[Grid] ❌ DCA: %s", err)
                return {"ok": False, "error": err}

        except Exception as e:
            level.status = "error"
            level.note   = str(e)[:120]
            log.error("[Grid] DCA исключение: %s", e, exc_info=True)
            return {"ok": False, "error": str(e)}

    # ── Динамические уровни ───────────────────────────────────────────────────

    def _add_reinvestment_buy(self, ton_amount: float, from_price: float):
        """После SELL: BUY-уровень на шаг ниже с compound-суммой."""
        buy_price = from_price / (1 + self._state.step_pct / 100)
        new_id    = -(100 + len(self._state.buy_levels))
        self._state.buy_levels.append(GridLevel(
            id=new_id, side="buy",
            price_ton=round(buy_price, 8),
            amount_grinch=0.0,
            amount_ton=round(ton_amount, 4),
            status="waiting",
            note=f"compound-реинвест {self._state.compound_multiplier:.2f}x @ {from_price:.6f}",
        ))
        log.info("[Grid] 📥 Compound-реинвест BUY @ %.6f с %.2f TON (mult=%.2f)",
                 buy_price, ton_amount, self._state.compound_multiplier)

    def _add_cycle_sell(self, grinch_amount: float, buy_price: float,
                        note: str = ""):
        """После BUY: SELL-уровень на шаг выше для замыкания цикла."""
        sell_price = buy_price * (1 + self._state.step_pct / 100)
        new_id     = 100 + len(self._state.sell_levels)
        self._state.sell_levels.append(GridLevel(
            id=new_id, side="sell",
            price_ton=round(sell_price, 8),
            amount_grinch=round(grinch_amount * 0.98, 2),  # 2% запас на газ
            amount_ton=0.0,
            status="waiting",
            note=note or f"цикл от BUY @ {buy_price:.6f}",
        ))
        log.info("[Grid] 📤 Цикловый SELL @ %.6f с %.0f GRINCH",
                 sell_price, grinch_amount)

    def _maybe_add_dca_level(self, price_ton: float, atr_pct: float,
                             regime: str, ai_buy_conf: float):
        """Добавить DCA-уровень если GridAI рекомендует.

        DCA-уровень размещается ниже center_price на DCA_STEP_MULT × step%.
        Не добавляем если уже есть MAX DCA активных уровней.
        """
        if not self._state.center_price_ton:
            return

        # Сколько активных DCA уровней уже есть
        active_dca = sum(1 for l in self._state.dca_levels if l.status == "waiting")
        if active_dca >= GridConfig.DCA_MAX_LEVELS:
            return

        # Цена DCA-триггера: ниже центра на step * DCA_STEP_MULT
        dca_step = self._state.step_pct * GridConfig.DCA_STEP_MULT
        dca_price = self._state.center_price_ton / (1 + dca_step / 100)

        # Не добавляем если текущая цена ещё не упала до уровня
        if price_ton > dca_price * 1.01:
            return

        # Не дублируем близкие уровни (< 1% разницы)
        for l in self._state.dca_levels:
            if l.status == "waiting" and abs(l.price_ton - dca_price) / dca_price < 0.01:
                return

        # Спрашиваем GridAI
        drawdown_pct = (1 - price_ton / self._state.center_price_ton) * 100
        price_vs_center = (price_ton / self._state.center_price_ton - 1) * 100
        if self._grid_ai:
            dca_conf = self._grid_ai.get_dca_confidence(
                atr_pct, regime, drawdown_pct, price_vs_center)
        else:
            dca_conf = 60.0 if regime in ("SIDEWAYS", "UNKNOWN") and drawdown_pct < 40 else 20.0

        # Также учитываем AI BUY-сигнал
        effective_conf = dca_conf * 0.6 + ai_buy_conf * 0.4
        if effective_conf < GridConfig.DCA_MIN_CONF:
            log.debug("[Grid] DCA не добавлен — уверенность %.1f%% < %.1f%%",
                      effective_conf, GridConfig.DCA_MIN_CONF)
            return

        # Размер DCA-ордера: от GridAI, с учётом win_rate
        ai_stats = self._grid_ai.get_stats() if self._grid_ai else {}
        win_rate = ai_stats.get("win_rate_pct", 50.0)
        dca_num  = active_dca + 1
        size_mult = (self._grid_ai.get_dca_size_multiplier(dca_num, win_rate)
                     if self._grid_ai else 1.0)
        base_ton  = GridConfig.MIN_ORDER_TON * 1.5  # 22.5 TON базовый DCA-ордер
        amount_ton = round(base_ton * size_mult, 2)

        new_id = -(1000 + len(self._state.dca_levels))
        self._state.dca_levels.append(GridLevel(
            id=new_id, side="dca",
            price_ton=round(dca_price, 8),
            amount_grinch=0.0,
            amount_ton=amount_ton,
            status="waiting",
            note=(f"DCA #{dca_num} | conf={effective_conf:.0f}% | "
                  f"size={size_mult:.2f}x | regime={regime}"),
        ))
        log.info("[Grid] 🟣 DCA L%d добавлен @ %.6f с %.2f TON "
                 "(conf=%.1f%% regime=%s drawdown=%.1f%%)",
                 new_id, dca_price, amount_ton, effective_conf, regime, drawdown_pct)

    def _maybe_recenter(self, price_ton: float, atr_pct: float, regime: str):
        """Авто-перецентровка: если цена ушла слишком далеко от центра.

        v3 — momentum-aware:
          • Сильный тренд вверх (mom > +1%/тик) → более ранняя перецентровка
            (порог снижается до 1.8 шагов), т.к. старые sell-уровни выгодны,
            но новые нужны выше для дальнейшей торговли.
          • Тренд вниз (mom < -1%) → НЕ перецентровываемся (ждём разворота).
          • Боковик → стандартный порог RECENTER_STEPS.
        Не перецентровывает при pump/distribution — только при спокойном рынке.
        Cooldown между перецентровками: RECENTER_COOLDOWN секунд.
        """
        if not self._state.center_price_ton or not self._state.active:
            return
        if regime in ("PUMP", "DISTRIBUTION", "POST_PUMP"):
            return
        now = time.time()
        if now - self._state.last_recenter_ts < GridConfig.RECENTER_COOLDOWN:
            return

        # Momentum-aware порог: при росте перецентровываемся раньше,
        # при падении — вообще не перецентровываемся (ждём дна)
        mom = self._price_momentum_pct
        if mom < -0.5:
            # Цена падает — перецентровка вниз усилит убыток; пропускаем
            return
        if mom > 1.0:
            # Сильный рост: снижаем порог → добавляем sell выше быстрее
            recenter_threshold = max(1.8, GridConfig.RECENTER_STEPS - 0.7)
        elif mom > 0.3:
            recenter_threshold = max(2.0, GridConfig.RECENTER_STEPS - 0.3)
        else:
            recenter_threshold = GridConfig.RECENTER_STEPS

        # Сколько шагов ушла цена от центра
        pct_from_center = abs(price_ton / self._state.center_price_ton - 1) * 100
        steps_away = pct_from_center / self._state.step_pct if self._state.step_pct > 0 else 0

        if steps_away < recenter_threshold:
            return

        log.info("[Grid] 🔄 Авто-перецентровка: цена %.6f ушла на %.1f шагов "
                 "(%.1f%%) от центра %.6f → новый центр",
                 price_ton, steps_away, pct_from_center,
                 self._state.center_price_ton)

        with self._lock:
            self._state.center_price_ton = price_ton
            self._state.last_recenter_ts = now
            self._state.last_action = (
                f"🔄 Авто-перецентровка @ {price_ton:.6f} "
                f"(ушли {steps_away:.1f} шагов от центра)")

            # ── Сбрасываем SELL-уровни ниже новой цены и собираем GRINCH ──
            freed_grinch = 0.0
            reset = 0
            for l in self._state.sell_levels:
                if l.status == "waiting" and l.price_ton < price_ton:
                    freed_grinch += l.amount_grinch
                    l.amount_grinch = 0.0
                    l.status = "skipped_ai"
                    l.note   = f"ниже нового центра {price_ton:.6f}"
                    reset += 1
            if reset:
                log.info("[Grid] Сброшено %d SELL-уровней ниже нового центра "
                         "(освобождено %.0f GRINCH)", reset, freed_grinch)

            # ── Перераспределяем освобождённый GRINCH в новые SELL выше центра ──
            if freed_grinch >= 1000:
                step_pct = self._state.step_pct
                # Берём максимальный id существующих sell
                max_id = max((l.id for l in self._state.sell_levels), default=0)
                # Сколько новых уровней добавить — по 1 на каждый сброшенный
                grinch_per_new = freed_grinch / reset
                added = 0
                for i in range(1, reset + 3):
                    new_id  = max_id + i
                    trigger = price_ton * (1 + step_pct / 100) ** i
                    # Пропускаем уровень если уже есть с близкой ценой (±0.5%)
                    clash = any(
                        abs(l.price_ton / trigger - 1) < 0.005
                        for l in self._state.sell_levels
                        if l.status not in ("skipped_ai",)
                    )
                    if clash:
                        continue
                    if grinch_per_new * trigger < GridConfig.MIN_ORDER_TON:
                        continue
                    self._state.sell_levels.append(GridLevel(
                        id=new_id, side="sell",
                        price_ton=round(trigger, 8),
                        amount_grinch=round(grinch_per_new, 2),
                        amount_ton=0.0,
                        status="waiting",
                        note=f"recenter-rebuild +{i}шаг @ {trigger:.6f}",
                    ))
                    added += 1
                    if added >= reset:
                        break
                if added:
                    log.info("[Grid] ✅ Добавлено %d новых SELL выше нового центра "
                             "(%.0f GRINCH каждый)", added, grinch_per_new)

    # ── Проверки прибыльности ─────────────────────────────────────────────────

    def _is_profitable_sell(self, level: GridLevel, current_price: float) -> tuple:
        """SELL прибылен если received_ton - gas > cost_ton.

        База затрат (cost_ton) определяется так:
        1. Если level.amount_ton > 0 — уровень был куплен BUY-циклом,
           используем реальные затраты TON.
        2. Иначе (начальный SELL из холдингов) — вычисляем cost как цену
           уровня ниже: price_ton / (1 + step_pct/100). Это корректно
           независимо от того, была ли рецентровка сетки вверх (center_price_ton
           мог вырасти и делал profit ложно отрицательным).
        """
        received_ton = level.amount_grinch * current_price * (1 - GridConfig.FEE_PCT)
        net_ton      = received_ton - GridConfig.GAS_PER_TRADE_TON
        if level.amount_ton > 0:
            # Куплено BUY-циклом — реальные затраты известны
            cost_ton = level.amount_ton
        else:
            # Начальный SELL из холдингов: cost = grinch * цена уровня ниже
            step = self._state.step_pct or GridConfig.DEFAULT_STEP_PCT
            cost_per_grinch = level.price_ton / (1 + step / 100)
            cost_ton = level.amount_grinch * cost_per_grinch
        profit = net_ton - cost_ton
        return profit > 0, round(profit, 4)

    def _is_profitable_buy_cycle(self, level: GridLevel) -> tuple:
        """BUY→SELL цикл прибылен если sell_revenue - gas×2 > buy_cost."""
        if level.price_ton <= 0 or level.amount_ton <= 0:
            return False, 0.0
        sell_target  = level.price_ton * (1 + self._state.step_pct / 100)
        grinch_out   = (level.amount_ton / level.price_ton) * (1 - GridConfig.FEE_PCT)
        sell_revenue = grinch_out * sell_target * (1 - GridConfig.FEE_PCT)
        net          = sell_revenue - GridConfig.GAS_PER_TRADE_TON * 2
        profit       = net - level.amount_ton
        return profit > 0, round(profit, 4)

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def _get_regime(self) -> tuple:
        """Возвращает (regime: str, atr_pct: float) из последнего DB-тика."""
        try:
            import db_store as _ds
            ticks = _ds.ticks_get_recent(1)
            if ticks:
                t = ticks[0]
                regime  = t.get("regime") or "UNKNOWN"
                atr_pct = float(t.get("atr_pct") or 0.0)
                return regime, atr_pct
        except Exception:
            pass
        return "UNKNOWN", self._get_atr_pct()

    def _get_ai_signal(self) -> tuple:
        """Возвращает (buy_conf%, sell_conf%) из последнего DB-тика."""
        try:
            import db_store as _ds
            ticks = _ds.ticks_get_recent(1)
            if ticks:
                t         = ticks[0]
                sig       = t.get("ai_sig") or t.get("final") or "HOLD"
                conf      = float(t.get("ai_conf") or 0.0)
                prob_up   = float(t.get("prob_up")   or 0.0)
                prob_down = float(t.get("prob_down") or 0.0)
                if sig == "BUY":
                    # conf — уверенность BUY; prob_down — вероятность падения
                    buy_val  = conf if conf > 0 else prob_up
                    return buy_val, prob_down
                if sig == "SELL":
                    # conf — уверенность SELL; prob_up — вероятность роста
                    sell_val = conf if conf > 0 else prob_down
                    return prob_up, sell_val
                # HOLD — возвращаем обе вероятности
                return prob_up, prob_down
        except Exception:
            pass
        return 0.0, 0.0

    def _get_atr_pct(self) -> float:
        """ATR из последнего DB-тика (fallback: 0.0)."""
        try:
            import db_store as _ds
            ticks = _ds.ticks_get_recent(1)
            if ticks:
                return float(ticks[0].get("atr_pct") or 0.0)
        except Exception:
            pass
        return 0.0

    def _calc_atr(self, candles: list) -> float:
        if not candles or len(candles) < 5:
            return 0.0
        last20 = candles[-20:]
        ranges = [(c[2] - c[3]) / c[3] * 100 for c in last20 if len(c) > 3 and c[3] > 0]
        return sum(ranges) / len(ranges) if ranges else 0.0

    def _calc_price_momentum(self) -> float:
        """Momentum цены: % изменение за доступную историю (макс ~10 мин).

        Положительный = цена растёт, отрицательный = падает.
        Используем линейную регрессию по точкам для устойчивости к шуму.
        """
        prices = list(self._price_history)
        n = len(prices)
        if n < 3:
            return 0.0
        # Линейная регрессия slope через least-squares
        xs = list(range(n))
        x_mean = (n - 1) / 2.0
        p_mean = sum(prices) / n
        num = sum((xs[i] - x_mean) * (prices[i] - p_mean) for i in range(n))
        den = sum((xs[i] - x_mean) ** 2 for i in range(n))
        if den == 0 or p_mean == 0:
            return 0.0
        slope = num / den  # TON/GRINCH per tick
        # Нормируем в % от средней цены
        mom_pct = slope / p_mean * 100
        return round(max(-20.0, min(20.0, mom_pct)), 4)

    def _get_balances(self) -> tuple:
        """Возвращает (grinch_balance, ton_balance). GRINCH берём из DCA-позиции если кошелёк = 0."""
        try:
            from dedust_client import get_shared_balance
            bal = get_shared_balance()
            ton_bal    = float(bal.get("TON", bal.get("ton", 0)))
            grinch_bal = float(bal.get("GRINCH", bal.get("grinch", 0)))
            if grinch_bal < 1000:
                import db_store as _ds
                _trades    = _ds.open_trades_get()
                grinch_bal = sum(float(t.get("amount", 0)) for t in _trades)
            return grinch_bal, ton_bal
        except Exception:
            return 0.0, 0.0

    def _get_dca_reserved_grinch(self) -> float:
        """Возвращает суммарное кол-во GRINCH, зарезервированное открытыми DCA-позициями.

        Координация Grid ↔ DCA: сетка НЕ должна продавать GRINCH, который
        DCA-трейдер считает своим (open_trades). Используется как в build_grid
        (чтобы выделить сетке только «свободный» GRINCH), так и в _execute_sell
        (runtime-проверка перед реальным свопом).
        """
        try:
            import db_store as _ds
            trades = _ds.open_trades_get()
            return float(sum(float(t.get("amount", 0)) for t in trades
                             if t.get("symbol", "GRINCH") and "GRINCH" in str(t.get("symbol", "GRINCH"))))
        except Exception:
            return 0.0

    def _heuristic_step(self, atr_pct: float, regime: str = "UNKNOWN") -> float:
        """Эвристический шаг без GridAI."""
        if regime in ("PUMP",):
            return 8.0
        if regime in ("DISTRIBUTION", "POST_PUMP"):
            return 6.0
        if regime == "TREND_UP":
            return 8.0 if atr_pct >= 4.0 else 6.0
        if atr_pct >= GridConfig.ATR_WIDE_PCT:
            return 8.0
        if atr_pct >= GridConfig.ATR_NORM_PCT:
            return 6.0
        if atr_pct >= GridConfig.ATR_NARROW_PCT:
            return 5.0
        return 4.0

    # ── Персистентность ───────────────────────────────────────────────────────

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._state.to_dict(), f, indent=2,
                          ensure_ascii=False, default=str)
        except Exception as e:
            log.warning("[Grid] Сохранение state: %s", e)

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, encoding="utf-8") as f:
                    self._state = GridState.from_dict(json.load(f))
                log.info("[Grid] Состояние загружено: active=%s sell=%d buy=%d dca=%d",
                         self._state.active,
                         len(self._state.sell_levels),
                         len(self._state.buy_levels),
                         len(self._state.dca_levels))
        except Exception as e:
            log.warning("[Grid] Загрузка state: %s — чистый старт", e)
            self._state = GridState()


# ─── Синглтон ─────────────────────────────────────────────────────────────────

_grid_trader_instance: Optional[GridTrader] = None
_grid_init_lock = threading.Lock()


def get_grid_trader() -> GridTrader:
    global _grid_trader_instance
    if _grid_trader_instance is None:
        with _grid_init_lock:
            if _grid_trader_instance is None:
                _grid_trader_instance = GridTrader()
    return _grid_trader_instance
