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
import tempfile
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

    @classmethod
    def min_profitable_order_ton(cls, step_pct=None):
        """Минимальный размер BUY/DCA, при котором цикл покрывает газ.

        Статический MIN_ORDER_TON защищает от пыли, но не учитывает, что
        фиксированный газ съедает весь результат маленького ордера. При
        шаге 4% на DeDust это около 31 TON; при более широком шаге порог
        уменьшается. Возвращаем максимум из обоих ограничений.
        """
        try:
            step = float(step_pct)
        except (TypeError, ValueError):
            step = cls.DEFAULT_STEP_PCT
        step = max(step, 0.0)
        cycle_factor = (
            (1.0 + step / 100.0) * (1.0 - cls.FEE_PCT) ** 2 - 1.0
        )
        if cycle_factor <= 0:
            return float("inf")
        gas_min = (cls.GAS_PER_TRADE_TON * 2.0) / cycle_factor
        # Округление вверх до 0.1 TON, чтобы не создать пограничный
        # уровень, который после округления снова станет убыточным.
        gas_min = math.ceil(gas_min * 10.0) / 10.0
        return max(float(cls.MIN_ORDER_TON), gas_min)
    # ATR-пороги для heuristic-шага
    ATR_WIDE_PCT       = 5.0   # ATR > 5% → шаг 8%
    ATR_NORM_PCT       = 3.0   # ATR 3-5% → шаг 6%
    ATR_NARROW_PCT     = 2.0   # ATR 2-3% → шаг 5%
    # AI-пороги (% уверенности)
    AI_SKIP_SELL_BUY_CONF  = 75.0   # пропустить SELL если AI BUY ≥ 75%
    AI_SKIP_BUY_SELL_CONF  = 60.0   # пропустить BUY если AI SELL ≥ 60%
    # Оставлено для совместимости со старыми настройками/UI. AI SELL
    # проверяется на каждом BUY/DCA-уровне отдельно, глобальной заморозки нет.
    AI_FREEZE_BUY_SELL     = 80.0
    # ── Только-в-плюс под AI ──────────────────────────────────────────
    AI_MIN_BUY_CONF        = 55.0   # минимальный BUY-сигнал для открытия любой покупки
    AI_BUY_SIZE_MIN_MULT   = 0.7    # множитель суммы при AI BUY = AI_MIN_BUY_CONF
    AI_BUY_SIZE_MAX_MULT   = 1.8    # множитель суммы при AI BUY = 100%
    # DCA — добавление позиции вниз
    DCA_MIN_CONF       = 48.0   # мин. уверенность GridAI для DCA-закупки
    DCA_MAX_LEVELS     = 5      # макс. активных DCA-уровней одновременно
    DCA_STEP_MULT      = 1.5    # DCA-триггер ниже центра на step * 1.5
    # Compound реинвест
    COMPOUND_RATE      = 0.02   # +2% к размеру reinvest за каждый прибыльный цикл
    COMPOUND_MAX_MULT  = 1.3    # максимальный множитель размера (снижено: 268TON>баланс при 2.0)
    # Compound BUY размещается НЕ на полный шаг вниз, а на REINVEST_STEP_MULT×step
    # (0.6 = 60% шага). Меньшее расстояние → BUY заполняется быстрее при откатах.
    REINVEST_STEP_MULT = 0.60   # множитель шага для первого реинвест-BUY
    REINVEST_STEP_MULT2 = 1.10  # второй реинвест-BUY (если капитала хватает на 2)
    # Авто-перецентровка
    RECENTER_STEPS     = 1.8    # шагов от центра до тихой перецентровки (было 2.5)
    RECENTER_COOLDOWN  = 1800   # секунд между перецентровками (было 3600)
    # Период опроса цены
    TICK_INTERVAL_SEC  = int(os.getenv("GRID_TICK_SEC", "20"))
    # Интервал обновления GridAI-параметров (тиков)
    AI_TUNE_EVERY_N    = 10
    # ── Деплой простаивающего баланса в новые BUY-уровни ──────────────────
    # Если на кошельке лежит > IDLE_TON_THRESHOLD свободных TON (не в ордерах,
    # не зарезервированных под газ) — автоматически добавляем новые BUY-уровни
    # ниже текущей цены, чтобы деньги работали, а не лежали без дела.
    IDLE_TON_THRESHOLD    = float(os.getenv("GRID_IDLE_THRESHOLD",  "20.0"))  # мин. простой (TON)
    IDLE_LEVEL_TON        = float(os.getenv("GRID_IDLE_LEVEL_TON",  "20.0"))  # TON на 1 новый уровень
    IDLE_DEPLOY_MAX_LEVELS= int(os.getenv("GRID_IDLE_MAX_LEVELS",   "3"))     # макс. новых уровней за вызов
    IDLE_COOLDOWN_SEC     = int(os.getenv("GRID_IDLE_COOLDOWN",     "120"))   # пауза между деплоями (2 мин)
    # ── Momentum-reversal gate ─────────────────────────────────────────────
    # Блокирует BUY пока цена ещё активно падает. Значение = порог падения
    # momentum за последние 20 тиков (%). При -2% и хуже — ждём разворота.
    MOMENTUM_BUY_BLOCK_PCT = float(os.getenv("GRID_MOMENTUM_BLOCK", "2.0"))
    # ── Anti-cascade защита ────────────────────────────────────────────────
    # Не делать больше CASCADE_MAX_BUYS покупок за CASCADE_WINDOW_SEC секунд.
    # Защищает от слива всего капитала в затяжной dump.
    CASCADE_MAX_BUYS    = int(os.getenv("GRID_CASCADE_MAX",    "2"))
    CASCADE_WINDOW_SEC  = int(os.getenv("GRID_CASCADE_WINDOW", "600"))   # 10 мин
    CASCADE_COOLDOWN_SEC= int(os.getenv("GRID_CASCADE_COOLDOWN","300"))  # 5 мин паузы
    # ── Depth-weighted sizing для idle-deploy ─────────────────────────────
    # Каждый шаг глубже anchor = +IDLE_DEPTH_BOOST к размеру ордера (до IDLE_DEPTH_MAX_MULT)
    IDLE_DEPTH_BOOST    = float(os.getenv("GRID_IDLE_DEPTH_BOOST", "0.15"))  # +15% за шаг
    IDLE_DEPTH_MAX_MULT = float(os.getenv("GRID_IDLE_DEPTH_MAX",   "1.5"))   # макс 1.5x

    # ── [УЛУЧШЕНИЕ] Compound acceleration ──────────────────────────────────
    # Динамическая ставка compound: base 2%, +0.5% за каждый выигрыш подряд (макс 5%)
    COMPOUND_ACCEL_PER_WIN = 0.005   # +0.5% к compound rate за WIN STREAK
    COMPOUND_ACCEL_MAX     = 0.05    # максимальная ставка compound за тик
    # Третий реинвест-BUY: активируется когда compound_multiplier >= 1.5x
    REINVEST_STEP_MULT3    = 1.60    # третий BUY на 1.6× шага — глубокое страхование
    # ── [УЛУЧШЕНИЕ] Volatility spike protection ────────────────────────────
    # Если цена упала > SPIKE_DROP_MULT × ATR за 1 тик — включить усиленную защиту
    SPIKE_DROP_MULT        = 1.5     # множитель ATR для срабатывания spike-защиты
    SPIKE_PROTECTION_SEC   = 600     # держать усиленную защиту 10 минут
    SPIKE_MOMENTUM_MULT    = 2.0     # во время spike: momentum_block × этот множитель
    # ── [УЛУЧШЕНИЕ] Adaptive tick speed ───────────────────────────────────
    TICK_INTERVAL_FAST     = int(os.getenv("GRID_TICK_FAST",   "10"))  # сек если цена рядом с уровнем
    TICK_INTERVAL_SLOW     = int(os.getenv("GRID_TICK_SLOW",   "30"))  # сек если цена далеко
    TICK_NEAR_LEVEL_PCT    = 0.5     # "рядом" = в пределах 0.5% от уровня
    # ── [УЛУЧШЕНИЕ] Idle deploy — динамический порог + сброс по цене ──────
    IDLE_BALANCE_PCT       = 0.10    # деплоить если простаивает > 10% от суммарного баланса в TON
    IDLE_PRICE_RESET_STEPS = 1.5     # сбросить cooldown если цена ушла > N шагов от последнего деплоя
    IDLE_DEPLOY_MAX_LEVELS_RICH = 5  # макс. новых уровней если свободного TON очень много (>3×порога)
    # ── [УЛУЧШЕНИЕ] Order sizing — Kelly boost ─────────────────────────────
    AI_BUY_SIZE_KELLY_MAX      = 2.2    # при высоком win_rate разрешаем до 2.2x
    AI_BUY_SIZE_KELLY_MIN_WR   = 8      # win_streak порог для Kelly-буста
    # ── [УЛУЧШЕНИЕ] Regime confirmation ────────────────────────────────────
    REGIME_CONFIRM_TICKS   = 2   # требуется подряд N одинаковых режимов перед применением политики
    # ── [УЛУЧШЕНИЕ] DCA-reduce: прибыль сетки снижает DCA-минус ───────────
    # После каждого прибыльного SELL: DCA_REDUCE_RATE × profit_ton TON
    # тратится на покупку GRINCH по текущей цене и добавляется в открытую
    # DCA-позицию трейдера. Это снижает среднюю цену входа и уменьшает минус.
    DCA_REDUCE_ENABLED    = True   # включить авто-снижение DCA-минуса
    DCA_REDUCE_RATE       = 0.25   # 25% от прибыли каждого SELL → в DCA-позицию
    DCA_REDUCE_MIN_PROFIT = 1.0    # мин. прибыль TON для срабатывания (не тратим копейки)


# ─── AI-менеджер сетки ───────────────────────────────────────────────────────

class GridAIManager:
    """
    Полное AI-управление сеткой GRINCH/TON.

    Каждые AI_MANAGE_EVERY_N тиков (~2.5 мин при TICK=30s):
      • Авто-активация/деактивация по режиму рынка
      • Авто-перестройка при смене режима или исчерпании уровней
      • Динамический выбор шага и количества уровней по ATR×policy
      • Индивидуальный пропуск BUY/DCA-уровня при сильном AI SELL-сигнале
    """

    # Политика для каждого режима рынка
    REGIME_POLICY: dict = {
        # режим         active  step_mult  levels  описание
        "SIDEWAYS":     {"active": True,  "step_mult": 0.85, "levels": 12,
                         "desc": "боковик — плотная сетка"},
        "SQUEEZE":      {"active": True,  "step_mult": 0.85, "levels": 12,
                         "desc": "сжатие волатильности — плотная сетка"},
        "RANGING":      {"active": True,  "step_mult": 0.85, "levels": 12,
                         "desc": "диапазон — плотная сетка"},
        "MILD_TREND":   {"active": True,  "step_mult": 1.0,  "levels": 10,
                         "desc": "умеренный тренд"},
        "TREND":        {"active": True,  "step_mult": 1.3,  "levels": 8,
                         "desc": "тренд — широкий шаг"},
        "TREND_UP":     {"active": True,  "step_mult": 1.5,  "levels": 7,
                         "desc": "тренд вверх — меньше уровней"},
        "UPTREND":      {"active": True,  "step_mult": 1.5,  "levels": 7,
                         "desc": "тренд вверх — меньше уровней"},
        "TREND_DOWN":   {"active": True,  "step_mult": 1.2,  "levels": 8,
                         "desc": "тренд вниз — широкая защитная сетка"},
        "DOWNTREND":    {"active": True,  "step_mult": 1.2,  "levels": 8,
                         "desc": "тренд вниз — широкая защитная сетка"},
        "VOLATILE":     {"active": True,  "step_mult": 1.1,  "levels": 9,
                         "desc": "волатильность — шаг немного шире"},
        "TRANSITION":   {"active": True,  "step_mult": 1.0,  "levels": 10,
                         "desc": "переход режимов"},
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
        # [УЛУЧШ] Regime confirmation: применять политику только после N тиков с одним режимом
        self._pending_regime:        str = "UNKNOWN"
        self._regime_confirm_count:  int = 0
        # [УЛУЧШ] Плавное изменение шага: запомнить последний целевой шаг
        self._last_target_step:    float = 0.0

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

        # [УЛУЧШ] Regime confirmation: не дёргать политику при мимолётных сменах режима.
        # Применяем новую политику только после REGIME_CONFIRM_TICKS подряд с одним режимом.
        if regime == self._pending_regime:
            self._regime_confirm_count += 1
        else:
            self._pending_regime       = regime
            self._regime_confirm_count = 1
        _confirmed_regime = (regime if self._regime_confirm_count >= GridConfig.REGIME_CONFIRM_TICKS
                             else self._last_regime)

        policy = self.REGIME_POLICY.get(_confirmed_regime, self.REGIME_POLICY["UNKNOWN"])
        decisions: list = []

        # ── 1. Активация / деактивация ────────────────────────────────────
        should_active = policy["active"]

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

        # ── 2. Динамический шаг — управляется adjust_step_by_atr() (ML) ───
        # УБРАНО: GridAI-Mgr не должен менять шаг напрямую через atr*mult —
        # это конфликтует с adjust_step_by_atr() (GridAI ML-ансамбль),
        # вызываемым в _tick() до ai_manager.tick(). При VOLATILE-режиме
        # ML даёт ≥5% (boundary [5,10]), а примитивная формула atr×1.1≈2.4%
        # → clamped к MIN=4.0% → бесконечная осцилляция 5.5%↔4.0%.

        # ── 3. Авто-перестройка ───────────────────────────────────────────
        if price_ton > 0:
            now = time.time()
            rebuild_reason = self._need_rebuild(
                sell_levels, regime, now, ton_balance, grinch_balance)

            if rebuild_reason:
                # Не строим обычную сетку из пыли/нулевого баланса. Исключение
                # только для reserve-reconcile: он должен убрать stale SELL
                # даже когда физический GRINCH уже стал нулевым.
                if grinch_balance <= 1000 and not rebuild_reason.startswith(
                        "сверка резерва"):
                    rebuild_reason = ""

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
                      ton_balance: float = 0.0,
                      grinch_balance: float = 0.0) -> str:
        """Возвращает причину перестройки или ''."""

        with self._trader._lock:
            buy_levels_copy = list(self._trader._state.buy_levels)
            grid_reserved = max(
                0.0, float(self._trader._state.grid_reserved_grinch or 0.0))

        # ── Сверка резерва с физическим балансом ───────────────────────────
        # После закрытия/изменения DCA старый grid_reserved_grinch может
        # пережить фактический баланс. Это отдельный rebuild-сигнал и он
        # намеренно проверяется до общего cooldown: иначе stale SELL-уровни
        # продолжали бы показывать/продавать несуществующие монеты.
        try:
            dca_reserved = self._trader._get_total_dca_reserved_grinch()
        except Exception:
            dca_reserved = 0.0
        available_for_grid = max(
            0.0, float(grinch_balance or 0.0) - dca_reserved)
        reserve_delta = abs(grid_reserved - available_for_grid)
        reserve_tolerance = max(
            100.0, max(grid_reserved, available_for_grid) * 0.01)
        if reserve_delta > reserve_tolerance:
            return (
                f"сверка резерва Grid: сохранено {grid_reserved:.0f}, "
                f"доступно после DCA {available_for_grid:.0f}")

        # ── Без кулдауна: все BUY-уровни no_funds, но TON есть ──────────────
        # Проверяем при каждом вызове — кулдаун здесь не применяем,
        # иначе пополнение кошелька не активирует сетку 30 минут.
        original_buys = [l for l in buy_levels_copy if -100 < l.id < 0]
        if original_buys:
            no_funds_buys = [l for l in original_buys if l.status == "no_funds"]
            _min_order = GridConfig.min_profitable_order_ton(
                self._trader._state.step_pct or GridConfig.DEFAULT_STEP_PCT
            )
            if (len(no_funds_buys) == len(original_buys)
                    and ton_balance > _min_order * len(original_buys)):
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
            meaningful = {
                "SIDEWAYS", "SQUEEZE", "RANGING", "MILD_TREND",
                "TREND", "TREND_UP", "UPTREND", "TREND_DOWN", "DOWNTREND",
                "VOLATILE", "PUMP", "POST_PUMP", "DISTRIBUTION",
            }
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
    # Баланс уровня принадлежит либо самой сетке, либо DCA. Старые
    # grid_state.json без этого поля мигрируются в Grid, кроме SELL с
    # пометкой DCA-цикла.
    owner:          str   = "grid"


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
    compound_win_streak:  int   = 0     # серия подряд прибыльных SELL (dynamic compound rate)
    # Служебные поля
    created_at:           float = 0.0
    last_tick_ts:         float = 0.0
    last_recenter_ts:     float = 0.0
    last_action:          str   = ""
    paused_reason:        str   = ""
    tick_count:           int   = 0
    # Логический резерв GRINCH, принадлежащий Grid. Не пересчитывается из
    # общего кошелька при каждом rebuild: общий кошелёк также содержит DCA.
    grid_reserved_grinch: float = 0.0
    # Локальный DCA-резерв Grid (DCA-уровни, купленные самим Grid).
    # Основной Trader-DCA добавляется динамически из open_trades.
    dca_reserved_grinch: float = 0.0

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

        def _level(raw: dict) -> GridLevel:
            """Load old levels which predate the owner field."""
            item = dict(raw or {})
            owner = item.get("owner")
            if owner not in ("grid", "dca"):
                side = item.get("side", "sell")
                note = str(item.get("note") or "")
                owner = "dca" if (
                    side == "dca" or "dca" in note.lower()
                ) else "grid"
            # Older snapshots and experimental versions may contain extra
            # bookkeeping keys.  Do not pass those into the dataclass
            # constructor: a restart must remain compatible with them.
            fields = {
                "id", "side", "price_ton", "amount_grinch", "amount_ton",
                "status", "filled_at", "fill_price_ton", "profit_ton",
                "tx_hash", "note",
            }
            data = {key: item[key] for key in fields if key in item}
            data["owner"] = owner
            return GridLevel(**data)

        for k, v in d.items():
            if k == "sell_levels":
                s.sell_levels     = [_level(l) for l in (v or [])]
            elif k == "buy_levels":
                s.buy_levels      = [_level(l) for l in (v or [])]
            elif k == "dca_levels":
                s.dca_levels      = [_level(l) for l in (v or [])]
            elif k == "completed_fills":
                s.completed_fills = [_level(l) for l in (v or [])]
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

        # Миграция старого состояния: до разделения балансов owner не
        # сохранялся. DCA-SELL можно определить по note, остальные SELL
        # относятся к Grid. DCA-уровни всегда принадлежат DCA.
        all_levels = s.sell_levels + s.buy_levels + s.dca_levels + s.completed_fills
        for level in all_levels:
            if level.side == "dca":
                level.owner = "dca"
            elif level.side == "sell" and str(level.note or "").startswith("DCA-"):
                level.owner = "dca"
            elif level.owner not in ("grid", "dca"):
                level.owner = "grid"

        # Старый JSON не имел grid_reserved_grinch. Восстанавливаем только
        # ещё не проданный Grid-баланс; filled SELL уже физически отсутствует.
        raw_reserve = d.get("grid_reserved_grinch")
        try:
            reserve = float(raw_reserve) if raw_reserve is not None else 0.0
        except (TypeError, ValueError):
            reserve = 0.0
        if reserve <= 0:
            reserve = sum(
                float(level.amount_grinch or 0)
                for level in s.sell_levels
                if level.owner == "grid" and level.status != "filled"
            )
        s.grid_reserved_grinch = max(0.0, reserve)
        try:
            dca_reserve = float(d.get("dca_reserved_grinch", 0.0) or 0.0)
        except (TypeError, ValueError):
            dca_reserve = 0.0
        if dca_reserve <= 0:
            dca_reserve = sum(
                float(level.amount_grinch or 0)
                for level in s.sell_levels
                if level.owner == "dca"
                and level.status != "filled"
            )
        s.dca_reserved_grinch = max(0.0, dca_reserve)
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
        # Сетка работает в том же процессе, что и основной Trader, но имеет
        # собственный lifecycle через GridState.active. Переключатель DCA
        # (trader.trading_enabled) не должен останавливать независимую сетку.
        self._trader_ref = None
        self._ai_manager = GridAIManager(self)   # ← полное AI-управление

        # История цен для momentum (последние 20 тиков = ~10 мин при 30с)
        self._price_history: deque = deque(maxlen=20)
        # Momentum в % (обновляется каждый тик)
        self._price_momentum_pct: float = 0.0
        # Время последнего деплоя простаивающего баланса (throttle)
        self._last_idle_deploy_ts: float = 0.0
        # Anti-cascade: метки времени последних BUY-исполнений
        self._buy_timestamps: deque = deque(maxlen=50)
        # Cascade hold: заморозка BUY до этого момента
        self._cascade_hold_until: float = 0.0
        # [УЛУЧШ] Adaptive tick: текущий интервал опроса (обновляется каждый тик)
        self._adaptive_tick_interval: float = float(GridConfig.TICK_INTERVAL_SEC)
        # [УЛУЧШ] Spike protection: до какого момента активна усиленная защита
        self._spike_protection_until: float = 0.0
        # [УЛУЧШ] Spike detection: цена на прошлом тике для расчёта резкого обвала
        self._prev_tick_price:        float = 0.0
        # [УЛУЧШ] Idle deploy: цена при последнем деплое (для сброса cooldown по движению цены)
        self._last_idle_deploy_price: float = 0.0
        # Suppress-set: не спамить лог про одни и те же убыточные уровни каждый тик
        self._unprofitable_warned: set = set()
        # DCA-reduce: Lock предотвращает конкурентные запуски.
        # acquire(blocking=False) — пропустить если предыдущий buy ещё идёт.
        self._dca_reduce_lock = threading.Lock()

        self._load_state()
        self._cleanup_stale_idle_levels()
        self._cleanup_dead_dca_levels()
        log.info("[Grid] Инициализирован v3. active=%s sell=%d buy=%d dca=%d "
                 "compound=%.2fx",
                 self._state.active,
                 len(self._state.sell_levels),
                 len(self._state.buy_levels),
                 len(self._state.dca_levels),
                 self._state.compound_multiplier)

    def _cleanup_stale_idle_levels(self):
        """Удаляет все waiting BUY уровни, у которых цикл заведомо убыточен.

        Покрывает два случая:
        - idle-deploy уровни, появившиеся при изменении IDLE_LEVEL_TON/GAS_PER_TRADE_TON
        - реинвест-уровни с маленьким amount_ton (возникают когда шаг осциллировал
          или прибыль от SELL была слишком мала для покрытия gas×2)

        Безопасно удалять: уровни в статусе waiting не исполнены, реальных средств
        не содержат — TON остаётся в кошельке.
        """
        step_pct = self._state.step_pct or GridConfig.DEFAULT_STEP_PCT
        cycle_factor = (1 + step_pct / 100) * (1 - GridConfig.FEE_PCT) ** 2 - 1
        if cycle_factor <= 0:
            return
        min_ton = GridConfig.min_profitable_order_ton(step_pct)

        stale = [
            l for l in self._state.buy_levels
            if l.status == "waiting"
            and l.amount_ton < min_ton
        ]
        if not stale:
            return

        stale_ids = {l.id for l in stale}
        before = len(self._state.buy_levels)
        self._state.buy_levels = [
            l for l in self._state.buy_levels if l.id not in stale_ids
        ]
        removed = before - len(self._state.buy_levels)
        log.info(
            "[Grid] 🧹 Очистка: удалено %d убыточных waiting BUY уровней "
            "(amount_ton < %.1f TON при шаге %.1f%%) — ids=%s",
            removed, min_ton, step_pct,
            sorted(stale_ids),
        )
        self._save_state()

    def _cleanup_dead_dca_levels(self):
        """Удаляет DCA-уровни, которые никогда не смогут исполниться:
        1. amount_ton < MIN_ORDER_TON — слишком маленький ордер (баг размера).
        2. (проверяется в тике) price_ton > текущей цены — уровень выше рынка.
        """
        bad = [
            l for l in self._state.dca_levels
            if l.status == "waiting"
            and l.amount_ton < GridConfig.min_profitable_order_ton(
                self._state.step_pct or GridConfig.DEFAULT_STEP_PCT
            )
        ]
        if not bad:
            return
        bad_ids = {l.id for l in bad}
        self._state.dca_levels = [
            l for l in self._state.dca_levels if l.id not in bad_ids
        ]
        log.info(
            "[Grid] 🧹 Очистка DCA: удалено %d уровней с amount_ton < %.1f TON (ids=%s)",
            len(bad_ids), GridConfig.min_profitable_order_ton(
                self._state.step_pct or GridConfig.DEFAULT_STEP_PCT
            ), sorted(bad_ids),
        )
        self._save_state()

    # ── Внешние зависимости ───────────────────────────────────────────────────

    def inject(self, dedust_client=None, ai_engine=None, grid_ai=None,
               trader_ref=None):
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
        if trader_ref is not None:
            self._trader_ref = trader_ref
            log.info("[Grid] Ручной переключатель синхронизирован с Trader")

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
            old_sell_levels = [
                l for l in self._state.sell_levels
                if l.status != "filled" and (l.amount_grinch or 0) > 0
            ]
            old_dca_levels = list(self._state.dca_levels)
            # dca_levels records the lifecycle of DCA orders, so a filled
            # level can already have its matching SELL filled as well.  It
            # is not a live balance by itself.  The persistent reserve is
            # decremented only after a confirmed DCA SELL and is the source
            # of truth for the next rebuild.
            old_dca_reserved = max(
                0.0, float(self._state.dca_reserved_grinch or 0)
            )
            old_total_dca_reserved = max(
                0.0,
                self._get_dca_reserved_grinch() + old_dca_reserved,
            )
            # Самосверка резерва: физический кошелёк после DCA — верхняя
            # граница того, чем действительно может владеть Grid. Раньше
            # здесь безусловно сохранялся старый reserve из JSON, поэтому
            # после изменения DCA появлялись SELL на уже отсутствующие монеты.
            grid_balance_now = max(
                0.0, float(grinch_balance or 0.0) - old_total_dca_reserved)
            old_grid_reserved = grid_balance_now

            # Если старых SELL больше, чем позволяет актуальный резерв,
            # убираем самые дальние уровни целиком. Частичный SELL оставил бы
            # трудно проверяемую пыль и мог бы снова попасть в следующий
            # rebuild; удалённые слоты будут восстановлены ниже из остатка.
            old_grid_levels = [
                l for l in old_sell_levels
                if l.owner == "grid" and (l.amount_grinch or 0) > 0
            ]
            allocated_grid = sum(
                float(l.amount_grinch or 0) for l in old_grid_levels)
            excess_grid = max(0.0, allocated_grid - old_grid_reserved)
            if excess_grid > 0:
                for level in sorted(
                        old_grid_levels,
                        key=lambda item: item.price_ton,
                        reverse=True):
                    if excess_grid <= 0:
                        break
                    old_sell_levels.remove(level)
                    excess_grid -= float(level.amount_grinch or 0)
                    log.info(
                        "[Grid] reserve reconcile: удалён stale SELL L%d "
                        "(%.0f GRINCH)", level.id, level.amount_grinch)

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
            state.sell_levels         = old_sell_levels
            state.dca_levels          = old_dca_levels
            state.grid_reserved_grinch = old_grid_reserved
            state.dca_reserved_grinch = old_dca_reserved

            # ── SELL-уровни (пирамидальное распределение) ─────────────
            # GridAI v3: нижние уровни получают больше GRINCH → больше
            # прибыли при умеренном росте (пирамида весов 1.30→0.70)
            pyramid_weights = (
                self._grid_ai.get_pyramid_weights(sell_levels)
                if self._grid_ai else [1.0] * sell_levels
            )
            # Первый build получает только свободный после DCA GRINCH.
            # Последующие rebuild НЕ забирают баланс заново: сохраняется
            # отдельный резерв Grid и все ещё не исполненные SELL-уровни.
            if state.grid_reserved_grinch <= 0:
                dca_reserved = old_total_dca_reserved
                state.grid_reserved_grinch = max(
                    0.0, float(grinch_balance) - dca_reserved
                )
            else:
                dca_reserved = old_total_dca_reserved
            allocated_grinch = sum(
                float(l.amount_grinch or 0)
                for l in state.sell_levels
                if l.owner == "grid" and l.status != "filled"
            )
            unallocated_grinch = max(
                0.0, state.grid_reserved_grinch - allocated_grinch
            )
            if dca_reserved > 0 or old_grid_reserved > 0:
                log.info("[Grid] build_grid: wallet=%.0f DCA=%.0f "
                         "Grid reserve=%.0f allocated=%.0f free=%.0f",
                         grinch_balance, dca_reserved,
                         state.grid_reserved_grinch, allocated_grinch,
                         unallocated_grinch)
            existing_grid_sell_count = sum(
                1 for l in state.sell_levels
                if l.owner == "grid" and l.status != "filled"
            )
            new_grid_slots = max(0, sell_levels - existing_grid_sell_count)
            new_weights = [
                max(0.0, float(pyramid_weights[i - 1]))
                for i in range(1, new_grid_slots + 1)
            ]
            weight_total = sum(new_weights)
            # Нормируем по фактическим новым слотам, а не по общему числу
            # уровней. После удаления stale SELL первый вес может быть 1.3;
            # деление только на new_grid_slots тогда превышало бы резерв.
            base_grinch = (
                unallocated_grinch / weight_total
                if weight_total > 0 else 0
            )
            grinch_per_level = base_grinch  # для обратной совместимости отчёта
            next_id = max((l.id for l in state.sell_levels), default=0) + 1
            for i in range(1, new_grid_slots + 1):
                trigger = current_price_ton * (1 + step_pct / 100) ** i
                w = new_weights[i - 1] if i - 1 < len(new_weights) else 1.0
                amount = min(
                    round(base_grinch * w, 2),
                    round(unallocated_grinch, 2),
                )
                if amount * trigger < GridConfig.MIN_ORDER_TON:
                    continue
                state.sell_levels.append(GridLevel(
                    id=next_id, side="sell",
                    price_ton=round(trigger, 8),
                    amount_grinch=amount,
                    amount_ton=0.0,
                    status="waiting",
                    note=(f"+{round((trigger/current_price_ton-1)*100, 1)}% от центра"
                          f" | вес×{w:.2f}"),
                    owner="grid",
                ))
                next_id += 1
                unallocated_grinch = max(0.0, unallocated_grinch - amount)

            # ── BUY-уровни ─────────────────────────────────────────────
            usable_ton    = max(0.0, ton_balance - GridConfig.GAS_RESERVE_TON)
            ton_per_level = usable_ton / buy_levels if buy_levels > 0 and usable_ton > 0 else 0
            _min_buy_order = GridConfig.min_profitable_order_ton(step_pct)
            for i in range(1, buy_levels + 1):
                trigger = current_price_ton / (1 + step_pct / 100) ** i
                st = "waiting" if ton_per_level >= _min_buy_order else "no_funds"
                state.buy_levels.append(GridLevel(
                    id=-i, side="buy",
                    price_ton=round(trigger, 8),
                    amount_grinch=0.0,
                    amount_ton=round(ton_per_level, 4) if st == "waiting" else 0.0,
                    status=st,
                    note=f"-{round((1-trigger/current_price_ton)*100, 1)}% от центра",
                    owner="grid",
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
                # DCA и Grid имеют независимые переключатели. Поле
                # manual_trading_enabled оставлено для совместимости UI и
                # теперь означает разрешение самой сетки.
                "manual_trading_enabled": bool(s.active),
                "grid_trading_enabled": bool(s.active),
                "dca_trading_enabled": (
                    bool(getattr(self._trader_ref, "trading_enabled", False))
                    if self._trader_ref is not None else None
                ),
                "blocked_reason": "",
                "center_price_ton":   s.center_price_ton,
                "step_pct":           s.step_pct,
                "total_profit_ton":   round(s.total_profit_ton, 4),
                "total_sell_cycles":  s.total_sell_cycles,
                "total_buy_cycles":   s.total_buy_cycles,
                "total_dca_cycles":   s.total_dca_cycles,
                "compound_multiplier": round(s.compound_multiplier, 3),
                "compound_bonus_ton": round(s.total_compound_bonus, 4),
                "grid_reserved_grinch": round(
                    max(0.0, s.grid_reserved_grinch), 2),
                "dca_reserved_grinch": round(
                    self._get_total_dca_reserved_grinch(), 2),
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
                     "owner": l.owner,
                     "profit_ton": round(l.profit_ton, 4),
                     "note": l.note, "filled_at": l.filled_at}
                    for l in s.sell_levels
                ],
                "completed_fills": [
                    {"id": l.id, "price_ton": l.price_ton,
                     "amount_grinch": l.amount_grinch,
                     "owner": l.owner,
                     "profit_ton": round(l.profit_ton, 4),
                     "fill_price_ton": l.fill_price_ton,
                     "note": l.note, "filled_at": l.filled_at}
                    for l in sorted(s.completed_fills, key=lambda x: x.filled_at or 0, reverse=True)
                ],
                "buy_levels": [
                    {"id": l.id, "price_ton": l.price_ton,
                     "amount_ton": l.amount_ton, "status": l.status,
                     "owner": l.owner,
                     "note": l.note, "filled_at": l.filled_at}
                    for l in s.buy_levels
                ],
                "dca_levels": [
                    {"id": l.id, "price_ton": l.price_ton,
                     "amount_ton": l.amount_ton, "status": l.status,
                     "owner": l.owner,
                     "note": l.note, "filled_at": l.filled_at,
                     "profit_ton": round(l.profit_ton, 4)}
                    for l in s.dca_levels
                ],
                "idle_deploy": {
                    "waiting_count": len([
                        l for l in s.buy_levels
                        if "idle-deploy" in (l.note or "") and l.status == "waiting"
                    ]),
                    "waiting_ton": round(sum(
                        l.amount_ton for l in s.buy_levels
                        if "idle-deploy" in (l.note or "") and l.status == "waiting"
                    ), 2),
                    "filled_count": len([
                        l for l in s.buy_levels
                        if "idle-deploy" in (l.note or "") and l.status == "filled"
                    ]),
                },
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
            # [УЛУЧШ] Adaptive tick: быстро если рядом с уровнем, медленно если далеко
            time.sleep(self._adaptive_tick_interval)

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

        # ── [УЛУЧШ] Spike protection: резкий обвал > SPIKE_DROP_MULT × ATR за 1 тик ─
        if self._prev_tick_price > 0 and atr_pct > 0 and price_ton > 0:
            _tick_drop_pct = (self._prev_tick_price - price_ton) / self._prev_tick_price * 100
            if _tick_drop_pct > GridConfig.SPIKE_DROP_MULT * atr_pct:
                self._spike_protection_until = time.time() + GridConfig.SPIKE_PROTECTION_SEC
                log.info(
                    "[Grid] 🚨 Spike-protect ON: -%.2f%% за тик (ATR=%.2f%%×%.1f). "
                    "Защита %ds",
                    _tick_drop_pct, atr_pct, GridConfig.SPIKE_DROP_MULT,
                    GridConfig.SPIKE_PROTECTION_SEC,
                )
        _spike_active = time.time() < self._spike_protection_until
        self._prev_tick_price = price_ton

        # ── [УЛУЧШ] Adaptive tick: ускоряемся если цена рядом с уровнем ──
        try:
            _near = False
            _step_p = self._state.step_pct or GridConfig.DEFAULT_STEP_PCT
            for _lv in list(self._state.sell_levels) + list(self._state.buy_levels):
                if _lv.status != "waiting" or _lv.price_ton <= 0:
                    continue
                if abs(_lv.price_ton - price_ton) / price_ton * 100 < GridConfig.TICK_NEAR_LEVEL_PCT:
                    _near = True
                    break
            self._adaptive_tick_interval = (
                GridConfig.TICK_INTERVAL_FAST if _near
                else max(GridConfig.TICK_INTERVAL_SEC, GridConfig.TICK_INTERVAL_SLOW)
            )
        except Exception:
            pass

        # ── Обновляем счётчик режима в GridAI ────────────────────────────
        if self._grid_ai:
            try:
                self._grid_ai.update_regime(regime)
            except Exception:
                pass

        # ── v5: Инжектируем рыночный контекст в GridAI ───────────────────
        if self._grid_ai:
            try:
                self._inject_market_context_to_grid_ai(atr_pct, regime)
            except Exception as _e:
                log.debug("[Grid] inject_market_ctx error: %s", _e)

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

        # ── AI SELL применяется на уровне конкретной операции ───────────
        # Не замораживаем весь BUY-проход: каждый waiting BUY/DCA-уровень
        # отдельно проверит AI_SKIP_BUY_SELL_CONF в своей ветке. SELL-уровни
        # всегда продолжают обрабатываться независимо от AI SELL.
        buy_frozen = False

        # ── GridAI: мультикритериальная пауза BUY ─────────────────────────
        _drawdown_pct = 0.0
        if self._grid_ai:
            try:
                _center = self._state.center_price_ton or price_ton
                _drawdown_pct = max(0.0, (1.0 - price_ton / _center) * 100.0) if _center > 0 else 0.0
                if self._grid_ai.should_pause_buying(
                        regime, _drawdown_pct, ai_sell_conf / 100.0):
                    buy_frozen = True
                    log.info(
                        "[Grid] 🛑 GridAI пауза BUY (режим=%s просадка=%.1f%%)",
                        regime, _drawdown_pct,
                    )
            except Exception:
                pass

        # ── v5: GridAI детектор ловушки ───────────────────────────────────
        if self._grid_ai and self._state.active:
            try:
                trap = self._grid_ai.check_trap_exit(
                    regime, _drawdown_pct, price_ton,
                    self._state.center_price_ton or price_ton)
                if trap.get("trap"):
                    action = trap.get("action", "HOLD")
                    reason = trap.get("reason", "")
                    conf   = trap.get("confidence", 0)
                    log.warning(
                        "[Grid] 🚨 GridAI ЛОВУШКА: %s (уверенность=%.0f%%) "
                        "причина: %s", action, conf, reason)
                    # REDUCE (≥50% уверенности) — замораживаем BUY
                    if action in ("REDUCE", "EXIT"):
                        buy_frozen = True
                    # EXIT (≥75% уверенности) — сообщаем через socketio
                    if action == "EXIT":
                        try:
                            from app import socketio as _sio
                            _sio.emit("grid_trap_alert", {
                                "action":     action,
                                "confidence": conf,
                                "reason":     reason,
                                "regime":     regime,
                                "drawdown":   round(_drawdown_pct, 1),
                            })
                        except Exception:
                            pass
            except Exception:
                pass

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
                        dca_freed = (
                            self._available_grinch_for_level(_lv)
                            >= _lv.amount_grinch
                        )
                    except Exception:
                        dca_freed = False
                    should_restore = price_below_trigger or dca_freed
                    reason = (
                        f"↩ восст. (откат {price_ton:.6f}<{_lv.price_ton:.6f})"
                        if price_below_trigger
                        else f"↩ восст. (DCA свободно ≥{_lv.amount_grinch:.0f} GRINCH)"
                    )

                if should_restore:
                    # Уровни с нулевым GRINCH нельзя продать — сразу помечаем skipped_small
                    if _lv.side in ("sell", "dca") and (_lv.amount_grinch or 0) < 100:
                        _lv.status = "skipped_small"
                        _lv.note   = "нет GRINCH после восстановления (amount=0)"
                    else:
                        _lv.status = "waiting"
                        _lv.note   = reason
                    restored_n += 1
            if restored_n:
                log.info("[Grid] ♻️ Восстановлено %d SELL → waiting/skipped_small", restored_n)

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

            # ── Anti-cascade: не более CASCADE_MAX_BUYS BUY за CASCADE_WINDOW_SEC ─
            _now_ts = time.time()
            _cascade_active = _now_ts < self._cascade_hold_until
            if not _cascade_active:
                _recent_buy_count = sum(
                    1 for _t in self._buy_timestamps
                    if _now_ts - _t < GridConfig.CASCADE_WINDOW_SEC
                )
                if _recent_buy_count >= GridConfig.CASCADE_MAX_BUYS:
                    self._cascade_hold_until = _now_ts + GridConfig.CASCADE_COOLDOWN_SEC
                    _cascade_active = True
                    log.info(
                        "[Grid] 🛡 Cascade-protect: %d BUY за %ds → пауза %ds",
                        _recent_buy_count, GridConfig.CASCADE_WINDOW_SEC,
                        GridConfig.CASCADE_COOLDOWN_SEC,
                    )

            # ── BUY-уровни ────────────────────────────────────────────────
            if not executed and not buy_frozen and not _cascade_active:
                for level in sorted(self._state.buy_levels,
                                    key=lambda l: l.price_ton, reverse=True):
                    if level.status != "waiting":
                        continue
                    if level.amount_ton < GridConfig.min_profitable_order_ton(
                            self._state.step_pct or GridConfig.DEFAULT_STEP_PCT):
                        continue
                    if price_ton > level.price_ton:
                        break

                    # ── Стоп: сильный AI SELL-сигнал ─────────────────────
                    if ai_sell_conf >= GridConfig.AI_SKIP_BUY_SELL_CONF:
                        log.info("[Grid] ⏭ BUY L%d @ %.6f — AI SELL %.0f%% → стоп",
                                 level.id, level.price_ton, ai_sell_conf)
                        continue

                    # ── Momentum-reversal gate: не покупать в свободном падении ─
                    # Если цена активно падает (momentum < -X%) — ждём разворота.
                    # Исключение: цена уже ниже уровня на >1 шаг (глубокий дип —
                    # откладывать дальше опасно, лучше войти).
                    _deep_dip = (level.price_ton > 0 and
                                 price_ton < level.price_ton * (1 - self._state.step_pct / 100))
                    # [УЛУЧШ] Spike protection: во время обвала — двойной порог блокировки
                    _momentum_block = GridConfig.MOMENTUM_BUY_BLOCK_PCT
                    if time.time() < self._spike_protection_until:
                        _momentum_block *= GridConfig.SPIKE_MOMENTUM_MULT
                    if (self._price_momentum_pct < -_momentum_block
                            and not _deep_dip):
                        log.info(
                            "[Grid] ⏳ BUY L%d @ %.6f — цена падает "
                            "(momentum=%.1f%% < -%.1f%%), ждём разворота",
                            level.id, level.price_ton,
                            self._price_momentum_pct, GridConfig.MOMENTUM_BUY_BLOCK_PCT,
                        )
                        continue

                    # ── ТОЛЬКО-В-ПЛЮС: главный гейт — математика цикла ──
                    # BUY выполняется только если BUY→SELL цикл прибылен.
                    # AI влияет лишь на размер ордера, не блокирует.
                    profitable, profit_est = self._is_profitable_buy_cycle(level)
                    if not profitable:
                        # Логируем только первый раз — не спамим каждые 30с для тех же уровней
                        if level.id not in self._unprofitable_warned:
                            log.info("[Grid] ⚠️ BUY L%d @ %.6f — цикл убыточен (est %+.4f TON)",
                                     level.id, level.price_ton, profit_est)
                            self._unprofitable_warned.add(level.id)
                        self._state.last_action = (
                            f"BUY L{level.id} пропущен (цикл убыточен {profit_est:+.4f} TON)")
                        continue
                    # Уровень стал прибыльным — убираем из suppress-set (цена/шаг изменились)
                    self._unprofitable_warned.discard(level.id)

                    # ── AI масштабирует размер (0.7x–1.8x), не блокирует ─
                    # [УЛУЧШ] Нелинейная кривая + Kelly-буст по win_streak GridAI
                    _kelly_max = GridConfig.AI_BUY_SIZE_MAX_MULT
                    if self._grid_ai:
                        try:
                            _streak = self._grid_ai.win_streak
                            if _streak >= GridConfig.AI_BUY_SIZE_KELLY_MIN_WR:
                                _kb = min(1.0, (_streak - GridConfig.AI_BUY_SIZE_KELLY_MIN_WR) / 10.0)
                                _kelly_max = GridConfig.AI_BUY_SIZE_MAX_MULT + _kb * (
                                    GridConfig.AI_BUY_SIZE_KELLY_MAX - GridConfig.AI_BUY_SIZE_MAX_MULT)
                        except Exception:
                            pass
                    if ai_buy_conf >= GridConfig.AI_MIN_BUY_CONF:
                        # Квадратичная кривая: сильный сигнал → непропорционально больший ордер
                        _t = (ai_buy_conf - GridConfig.AI_MIN_BUY_CONF) / max(1.0, 100.0 - GridConfig.AI_MIN_BUY_CONF)
                        ai_size_mult = GridConfig.AI_BUY_SIZE_MIN_MULT + (_t ** 1.5) * (_kelly_max - GridConfig.AI_BUY_SIZE_MIN_MULT)
                    else:
                        # Слабый AI-сигнал → минимальный размер (осторожный вход)
                        ai_size_mult = GridConfig.AI_BUY_SIZE_MIN_MULT
                        log.info("[Grid] 📉 BUY L%d — AI слабый (%.0f%%), вход мин. ×%.2f (прибыль est +%.4f TON)",
                                 level.id, ai_buy_conf, ai_size_mult, profit_est)
                    ai_size_mult = round(
                        max(GridConfig.AI_BUY_SIZE_MIN_MULT,
                            min(_kelly_max, ai_size_mult)), 3)
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
            # Чистим DCA-уровни, которые оказались выше текущей цены —
            # они никогда не исполнятся (BUY ждёт падения ДО уровня).
            stale_above = [
                l for l in self._state.dca_levels
                if l.status == "waiting" and l.price_ton > price_ton * 1.02
            ]
            if stale_above:
                sa_ids = {l.id for l in stale_above}
                self._state.dca_levels = [
                    l for l in self._state.dca_levels if l.id not in sa_ids
                ]
                log.info(
                    "[Grid] 🧹 DCA-уровни выше рынка удалены: %s (цена %.6f)",
                    sorted(sa_ids), price_ton,
                )
                self._save_state()

            if not executed and not buy_frozen:
                for level in sorted(self._state.dca_levels,
                                    key=lambda l: l.price_ton, reverse=True):
                    if level.status != "waiting":
                        continue
                    if level.amount_ton < GridConfig.min_profitable_order_ton(
                            self._state.step_pct or GridConfig.DEFAULT_STEP_PCT):
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
                    # [УЛУЧШ] Нелинейная кривая DCA + Kelly-буст (аналог BUY)
                    _kelly_max_dca = GridConfig.AI_BUY_SIZE_MAX_MULT
                    if self._grid_ai:
                        try:
                            _streak = self._grid_ai.win_streak
                            if _streak >= GridConfig.AI_BUY_SIZE_KELLY_MIN_WR:
                                _kb = min(1.0, (_streak - GridConfig.AI_BUY_SIZE_KELLY_MIN_WR) / 10.0)
                                _kelly_max_dca = GridConfig.AI_BUY_SIZE_MAX_MULT + _kb * (
                                    GridConfig.AI_BUY_SIZE_KELLY_MAX - GridConfig.AI_BUY_SIZE_MAX_MULT)
                        except Exception:
                            pass
                    if ai_buy_conf >= GridConfig.AI_MIN_BUY_CONF:
                        _t = (ai_buy_conf - GridConfig.AI_MIN_BUY_CONF) / max(1.0, 100.0 - GridConfig.AI_MIN_BUY_CONF)
                        ai_size_mult = GridConfig.AI_BUY_SIZE_MIN_MULT + (_t ** 1.5) * (_kelly_max_dca - GridConfig.AI_BUY_SIZE_MIN_MULT)
                    else:
                        ai_size_mult = GridConfig.AI_BUY_SIZE_MIN_MULT
                    ai_size_mult = round(
                        max(GridConfig.AI_BUY_SIZE_MIN_MULT,
                            min(_kelly_max_dca, ai_size_mult)), 3)
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

            # ── Деплой простаивающего баланса в новые BUY-уровни ──────────
            if not executed and not buy_frozen:
                try:
                    self._maybe_deploy_idle_balance(
                        price_ton, ton_bal, ai_buy_conf, regime)
                except Exception as e:
                    log.warning("[Grid] idle-deploy error: %s", e)

            # ── Near-price density: добавить SELL-уровень если рядом нет ни одного ─
            try:
                self._ensure_near_price_sell(price_ton, regime)
            except Exception as e:
                log.warning("[Grid] near-price-sell error: %s", e)

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

            # ── Раздельное владение GRINCH: runtime guard ────────────────
            # Grid и DCA имеют независимые резервы. Общий кошелёк проверяем
            # только как физический верхний предел перед свопом.
            try:
                _free_g = self._available_grinch_for_level(level)
                if _free_g < level.amount_grinch:
                    log.warning(
                        "[Grid] ⛔ SELL L%d заблокирован: "
                        "owner=%s свободно %.0f GRINCH, нужно %.0f — пропуск",
                        level.id, level.owner, _free_g, level.amount_grinch)
                    level.status = "skipped_dca"
                    level.note   = (f"Резерв {level.owner}: свободно "
                                    f"{_free_g:.0f} < нужно {level.amount_grinch:.0f}")
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
                if level.owner == "dca":
                    self._state.dca_reserved_grinch = max(
                        0.0, self._state.dca_reserved_grinch -
                        level.amount_grinch)
                else:
                    self._state.grid_reserved_grinch = max(
                        0.0, self._state.grid_reserved_grinch -
                        level.amount_grinch)
                # Сохраняем в completed_fills — выживает при rebuild
                import copy as _copy
                self._state.completed_fills.append(_copy.copy(level))
                self._state.last_action = (
                    f"✅ SELL L{level.id}: {level.amount_grinch:.0f} GRINCH "
                    f"@ {current_price:.6f} | нетто ≈ {net_ton:.2f} TON "
                    f"| прибыль {level.profit_ton:+.3f} TON")
                log.info("[Grid] %s", self._state.last_action)

                # ── DCA-reduce бюджет — вычисляем ДО compound ─────────
                # Карвим из profit ПЕРВЫМ, чтобы compound не расходовал те же TON.
                _dca_budget = 0.0
                if GridConfig.DCA_REDUCE_ENABLED and profit >= GridConfig.DCA_REDUCE_MIN_PROFIT:
                    _dca_budget = round(profit * GridConfig.DCA_REDUCE_RATE, 4)
                    if _dca_budget < GridConfig.min_profitable_order_ton(
                            self._state.step_pct or GridConfig.DEFAULT_STEP_PCT):
                        _dca_budget = 0.0

                # ── Compound-реинвест ──────────────────────────────────
                if profit > 0:
                    # [УЛУЧШ] Динамическая ставка compound: base 2% + 0.5% за WIN STREAK
                    self._state.compound_win_streak += 1
                    _dyn_rate = min(
                        GridConfig.COMPOUND_ACCEL_MAX,
                        GridConfig.COMPOUND_RATE +
                        self._state.compound_win_streak * GridConfig.COMPOUND_ACCEL_PER_WIN)
                    old_mult = self._state.compound_multiplier
                    new_mult = min(GridConfig.COMPOUND_MAX_MULT, old_mult + _dyn_rate)
                    self._state.compound_multiplier = new_mult
                    bonus = max(0.0, (net_ton - GridConfig.GAS_RESERVE_TON) * (new_mult - 1.0))
                    self._state.total_compound_bonus += bonus
                    log.info("[Grid] 📈 Compound: %.2fx → %.2fx | streak=%d rate=%.3f (+%.4f TON)",
                             old_mult, new_mult, self._state.compound_win_streak, _dyn_rate, bonus)
                else:
                    # Сброс серии при убытке
                    self._state.compound_win_streak = 0

                # Compound reinvest из net_ton минус DCA-бюджет (координация бюджетов)
                ton_to_reinvest = (net_ton - GridConfig.GAS_RESERVE_TON - _dca_budget) * \
                                  self._state.compound_multiplier
                if ton_to_reinvest >= GridConfig.min_profitable_order_ton(
                        self._state.step_pct or GridConfig.DEFAULT_STEP_PCT):
                    self._add_reinvestment_buy(ton_to_reinvest, current_price)

                # ── DCA-reduce: фоновый поток (не блокирует grid._lock) ─
                # blocking=False: если предыдущий buy ещё идёт — пропускаем,
                # чтобы не накапливать очередь блокирующих buy-звонков.
                if _dca_budget >= GridConfig.min_profitable_order_ton(
                        self._state.step_pct or GridConfig.DEFAULT_STEP_PCT):
                    if self._dca_reduce_lock.acquire(blocking=False):
                        threading.Thread(
                            target=self._reduce_dca_loss,
                            args=(_dca_budget, current_price),
                            daemon=True,
                            name="grid-dca-reduce",
                        ).start()
                    else:
                        log.debug("[Grid] DCA-reduce: пропуск — предыдущий запуск активен")

                # ── Обучаем GridAI ─────────────────────────────────────
                if self._grid_ai:
                    try:
                        _center_p = self._state.center_price_ton or current_price
                        _ddown_fill = (max(0.0, (1.0 - current_price / _center_p) * 100.0)
                                       if _center_p > 0 else 0.0)
                        self._grid_ai.record_fill(
                            "sell", self._state.step_pct,
                            atr_pct, regime, profit, profit_pct,
                            compound_mult=self._state.compound_multiplier,
                            drawdown_pct=_ddown_fill)
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
                level.owner          = "grid"
                self._state.grid_reserved_grinch += level.amount_grinch

                self._state.total_buy_cycles += 1
                self._buy_timestamps.append(time.time())   # anti-cascade учёт
                self._state.last_action = (
                    f"✅ BUY L{level.id}: {level.amount_ton:.2f} TON "
                    f"→ {grinch_received:.0f} GRINCH @ {current_price:.6f}")
                log.info("[Grid] %s", self._state.last_action)

                self._add_cycle_sell(grinch_received, current_price,
                                     regime=regime, atr_pct=atr_pct)

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
                level.owner          = "dca"
                self._state.dca_reserved_grinch += level.amount_grinch

                self._state.total_dca_cycles += 1
                self._buy_timestamps.append(time.time())   # anti-cascade учёт
                self._state.last_action = (
                    f"✅ DCA: {level.amount_ton:.2f} TON "
                    f"→ {grinch_received:.0f} GRINCH @ {current_price:.6f} "
                    f"(всего DCA: {self._state.total_dca_cycles})")
                log.info("[Grid] %s", self._state.last_action)

                # SELL-уровень для закрытия DCA позиции
                self._add_cycle_sell(grinch_received, current_price,
                                     note=f"DCA-цикл от {current_price:.6f}",
                                     regime=regime, atr_pct=atr_pct,
                                     owner="dca")

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
        """После SELL: один или два BUY-уровня с compound-суммой.

        Первый BUY размещается на REINVEST_STEP_MULT×step (≈60%) ниже цены SELL
        вместо полного шага — так он заполняется быстрее при малом откате.
        Если ton_amount >= 2×MIN_ORDER_TON, добавляется второй BUY на
        REINVEST_STEP_MULT2×step (≈110%) — создаёт дополнительный BUY→SELL цикл.
        """
        step = self._state.step_pct
        _min_order = GridConfig.min_profitable_order_ton(step)

        def _next_compound_id():
            # Диапазон compound: -101 … -999 (не трогаем DCA -1000…-1999 и idle -2000…)
            compound_ids = [l.id for l in self._state.buy_levels if -999 <= l.id <= -101]
            return (min(compound_ids) - 1) if compound_ids else -101

        # ── Первый BUY: ближе к цене (REINVEST_STEP_MULT × step) ────────────
        buy_price1 = from_price / (1 + step * GridConfig.REINVEST_STEP_MULT / 100)
        nid1 = _next_compound_id()
        self._state.buy_levels.append(GridLevel(
            id=nid1, side="buy",
            price_ton=round(buy_price1, 8),
            amount_grinch=0.0,
            amount_ton=round(ton_amount, 4),
            status="waiting",
            note=(f"compound-реинвест {self._state.compound_multiplier:.2f}x"
                  f" @ {from_price:.6f} | ×{GridConfig.REINVEST_STEP_MULT}шаг"),
            owner="grid",
        ))
        log.info("[Grid] 📥 Реинвест BUY#1 @ %.6f с %.2f TON (×%.2fшаг mult=%.2f)",
                 buy_price1, ton_amount, GridConfig.REINVEST_STEP_MULT,
                 self._state.compound_multiplier)

        # ── Второй BUY: чуть дальше (REINVEST_STEP_MULT2 × step) ────────────
        # Добавляем только если капитал позволяет (≥ 2×MIN_ORDER_TON на каждый)
        if ton_amount >= _min_order * 2:
            # [УЛУЧШ] При compound >= 1.5x: три BUY вместо двух — 40/40/20% капитала
            _use_third = (self._state.compound_multiplier >= 1.5
                          and ton_amount >= _min_order * 3)
            if _use_third:
                third = round(ton_amount * 0.20, 4)
                half  = round((ton_amount - third) / 2, 4)
            else:
                half  = round(ton_amount / 2, 4)
                third = 0.0
            # Скорректировать сумму первого BUY
            self._state.buy_levels[-1].amount_ton = half
            buy_price2 = from_price / (1 + step * GridConfig.REINVEST_STEP_MULT2 / 100)
            nid2 = _next_compound_id()
            self._state.buy_levels.append(GridLevel(
                id=nid2, side="buy",
                price_ton=round(buy_price2, 8),
                amount_grinch=0.0,
                amount_ton=half,
                status="waiting",
                note=(f"compound-реинвест {self._state.compound_multiplier:.2f}x"
                      f" @ {from_price:.6f} | ×{GridConfig.REINVEST_STEP_MULT2}шаг"),
                owner="grid",
            ))
            log.info("[Grid] 📥 Реинвест BUY#2 @ %.6f с %.2f TON (×%.2fшаг)",
                     buy_price2, half, GridConfig.REINVEST_STEP_MULT2)
            # [УЛУЧШ] Третий BUY на глубоком страховании (только при compound >= 1.5x)
            if _use_third and third >= _min_order:
                buy_price3 = from_price / (1 + step * GridConfig.REINVEST_STEP_MULT3 / 100)
                nid3 = _next_compound_id()
                self._state.buy_levels.append(GridLevel(
                    id=nid3, side="buy",
                    price_ton=round(buy_price3, 8),
                    amount_grinch=0.0,
                    amount_ton=third,
                    status="waiting",
                    note=(f"compound-реинвест {self._state.compound_multiplier:.2f}x"
                          f" @ {from_price:.6f} | ×{GridConfig.REINVEST_STEP_MULT3}шаг (глубокий)"),
                    owner="grid",
                ))
                log.info("[Grid] 📥 Реинвест BUY#3 @ %.6f с %.2f TON (×%.2fшаг, compound=%.2fx)",
                         buy_price3, third, GridConfig.REINVEST_STEP_MULT3,
                         self._state.compound_multiplier)

    def _reduce_dca_loss(self, ton_budget: float, current_price_ton: float):
        """Фоновый поток: покупает GRINCH на ton_budget TON и добавляет в DCA-позицию.

        Вызывается из _execute_sell через threading.Thread (daemon).
        _dca_reduce_lock захвачен вызывающей стороной; освобождается здесь в finally.
        ton_budget уже проверен (>= MIN_ORDER_TON) вызывающей стороной.
        """
        try:
            # Проверяем наличие открытой DCA-позиции
            try:
                import db_store as _ds
                open_trades = _ds.open_trades_get()
                long_trades = [t for t in open_trades if t.get("side") == "buy"]
                if not long_trades:
                    log.debug("[Grid] DCA-reduce: нет LONG-позиции — пропуск")
                    return
            except Exception as _e:
                log.debug("[Grid] DCA-reduce: open_trades read error: %s", _e)
                return

            log.info("[Grid] 📉 DCA-reduce: покупаем %.3f TON → снижаем средний вход DCA",
                     ton_budget)

            result = self._dc.buy(ton_budget)
            if not result.get("ok"):
                log.warning("[Grid] DCA-reduce: buy failed — %s", result.get("error"))
                return

            grinch_bought = float(result.get("grinch_received", 0))
            if grinch_bought <= 0:
                grinch_bought = ton_budget / max(current_price_ton, 1e-12) * 0.99
            grinch_bought = round(grinch_bought, 6)

            # USD-цена: берём из соотношения существующей позиции
            _entry_usd = 0.0
            try:
                _lt0    = long_trades[0]
                _ep_usd = float(_lt0.get("entry_price", 0) or 0)
                _ep_ton = float(_lt0.get("entry_price_ton", 0) or 0)
                if _ep_ton > 0 and _ep_usd > 0:
                    _entry_usd = round(current_price_ton * _ep_usd / _ep_ton, 8)
            except Exception:
                pass
            if _entry_usd == 0.0:
                try:
                    from price_feed import get as _pf_get
                    _entry_usd = float(_pf_get("GRINCH") or 0)
                except Exception:
                    pass

            # Получаем трейдер-синглтон
            import sys as _sys, time as _time
            _app = _sys.modules.get("app") or _sys.modules.get("__main__")
            tr   = getattr(_app, "trader", None)

            _max_dca_idx = max((int(t.get("dca_index") or 1) for t in long_trades), default=1)
            # Сохраняем ID оригинальной позиции — он переживёт merge
            _orig_id = long_trades[0].get("id", f"dca_{int(_time.time())}")
            extra_entry = {
                "id":              _orig_id,   # тот же ID → merge сохранит его
                "side":            "buy",
                "symbol":          "GRINCH/TON",
                "amount":          grinch_bought,
                "stake_ton":       ton_budget,
                "entry_price":     _entry_usd,
                "entry_price_ton": round(current_price_ton, 8),
                "dca_entry":       True,
                "dca_index":       _max_dca_idx + 1,
                "opened_at":       _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime()),
                "status":          "open",
                "source":          "grid_dca_reduce",
                "note":            (f"Grid DCA-reduce: +{grinch_bought:.0f} GRINCH "
                                    f"@ {current_price_ton:.6f} TON"),
            }

            if tr is not None:
                # Append + merge под одним захватом _ot_lock (RLock — реентерабелен)
                # чтобы между append и merge не вклинился другой поток.
                with tr._ot_lock:
                    tr.open_trades.append(extra_entry)
                    tr._merge_long_trades()
                # Лог результата
                try:
                    with tr._ot_lock:
                        merged = next((t for t in tr.open_trades
                                       if t.get("side") == "buy"), None)
                    if merged:
                        new_avg  = float(merged.get("entry_price_ton") or current_price_ton)
                        tot_grch = float(merged.get("amount") or 0)
                        log.info(
                            "[Grid] ✅ DCA-reduce OK: +%.0f GRINCH @ %.6f TON | "
                            "avg вход %.6f TON | итого %.0f GRINCH",
                            grinch_bought, current_price_ton, new_avg, tot_grch)
                except Exception:
                    pass
            else:
                # Fallback: обновляем БД напрямую
                try:
                    lt         = long_trades[0]
                    old_amount = float(lt.get("amount") or 0)
                    old_stake  = float(lt.get("stake_ton") or 0)
                    new_amount = old_amount + grinch_bought
                    new_stake  = old_stake + ton_budget
                    lt["amount"]          = round(new_amount, 6)
                    lt["stake_ton"]       = round(new_stake, 4)
                    lt["entry_price_ton"] = round(new_stake / new_amount, 8) if new_amount > 0 else lt.get("entry_price_ton", 0)
                    if old_amount > 0:
                        _old_ep_usd = float(lt.get("entry_price") or 0)
                        _old_ep_ton = old_stake / old_amount
                        if _old_ep_ton > 0 and _old_ep_usd > 0:
                            lt["entry_price"] = round(
                                lt["entry_price_ton"] * _old_ep_usd / _old_ep_ton, 8)
                    _ds.open_trades_save(open_trades)
                    log.info("[Grid] ✅ DCA-reduce (DB fallback): "
                             "+%.0f GRINCH @ %.6f, avg %.6f TON",
                             grinch_bought, current_price_ton, lt["entry_price_ton"])
                except Exception as _dbe:
                    log.warning("[Grid] DCA-reduce DB error: %s", _dbe)
        finally:
            # Всегда освобождаем лок — даже при исключении
            try:
                self._dca_reduce_lock.release()
            except RuntimeError:
                pass

    def _add_cycle_sell(self, grinch_amount: float, buy_price: float,
                        note: str = "", regime: str = "UNKNOWN",
                        atr_pct: float = 0.0, owner: str = "grid"):
        """После BUY: SELL-уровень на шаг выше для замыкания цикла.
        Цена адаптируется к режиму через GridAI.get_sell_target_pct().
        """
        if grinch_amount < 1.0:   # guard: не создавать уровень с нулём/копейками
            log.debug("[Grid] _add_cycle_sell: grinch_amount=%.2f < 1 — пропуск", grinch_amount)
            return
        # Адаптивный целевой %: GridAI учитывает режим и ATR
        # SQUEEZE/SIDEWAYS → 0.85–0.90× шага (быстрый выход)
        # VOLATILE/TREND_UP → 1.10–1.15× (дать прибыли расти)
        # PUMP → 1.30× (максимальная жадность)
        if self._grid_ai:
            try:
                target_pct = self._grid_ai.get_sell_target_pct(
                    self._state.step_pct, regime, atr_pct)
            except Exception:
                target_pct = self._state.step_pct
        else:
            target_pct = self._state.step_pct
        sell_price = buy_price * (1 + target_pct / 100)
        # Уникальный ID: максимальный из cycle-уровней (≥ 100) плюс 1
        cycle_ids = [l.id for l in self._state.sell_levels if l.id >= 100]
        new_id    = (max(cycle_ids) + 1) if cycle_ids else 101
        self._state.sell_levels.append(GridLevel(
            id=new_id, side="sell",
            price_ton=round(sell_price, 8),
            amount_grinch=round(grinch_amount * 0.98, 2),  # 2% запас на газ
            amount_ton=0.0,
            status="waiting",
            note=note or f"цикл от BUY @ {buy_price:.6f}",
            owner="dca" if owner == "dca" else "grid",
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
        _min_order = GridConfig.min_profitable_order_ton(self._state.step_pct)
        base_ton  = _min_order * 1.5
        amount_ton = round(max(base_ton * size_mult, _min_order), 2)

        # Диапазон DCA: -1001 … -1999 (min существующих - 1)
        _dca_ids = [l.id for l in self._state.dca_levels if l.id <= -1000]
        new_id = (min(_dca_ids) - 1) if _dca_ids else -1001
        self._state.dca_levels.append(GridLevel(
            id=new_id, side="dca",
            price_ton=round(dca_price, 8),
            amount_grinch=0.0,
            amount_ton=amount_ton,
            status="waiting",
            note=(f"DCA #{dca_num} | conf={effective_conf:.0f}% | "
                  f"size={size_mult:.2f}x | regime={regime}"),
            owner="dca",
        ))
        log.info("[Grid] 🟣 DCA L%d добавлен @ %.6f с %.2f TON "
                 "(conf=%.1f%% regime=%s drawdown=%.1f%%)",
                 new_id, dca_price, amount_ton, effective_conf, regime, drawdown_pct)

    def _maybe_deploy_idle_balance(self, price_ton: float, ton_bal: float,
                                   ai_buy_conf: float, regime: str):
        """Автоматически размещает новые BUY-уровни из простаивающего TON-баланса.

        Логика:
        1. Считаем TON, уже заморожённый в ожидающих BUY-ордерах.
        2. free_ton = ton_bal − frozen_buy_ton − GAS_RESERVE_TON
        3. Если free_ton ≥ IDLE_TON_THRESHOLD и AI BUY-сигнал приемлем:
           добавляем до IDLE_DEPLOY_MAX_LEVELS новых BUY-уровней ниже текущей
           цены с шагом step_pct, не дублируя существующие уровни.

        Вызывается каждый тик; cooldown IDLE_COOLDOWN_SEC предотвращает
        лавинообразное добавление.
        """
        now = time.time()

        # [УЛУЧШ] Сброс cooldown если цена ушла достаточно далеко от последнего деплоя
        _step_p = self._state.step_pct or GridConfig.DEFAULT_STEP_PCT
        if (self._last_idle_deploy_price > 0 and price_ton > 0
                and abs(price_ton - self._last_idle_deploy_price) / self._last_idle_deploy_price * 100
                    > _step_p * GridConfig.IDLE_PRICE_RESET_STEPS):
            self._last_idle_deploy_ts = 0.0   # сбросить cooldown: цена ушла
            log.debug("[Grid] idle-deploy cooldown сброшен — цена сдвинулась на > %.1f шагов",
                      GridConfig.IDLE_PRICE_RESET_STEPS)

        # ── 0. Перепозиционирование устаревших idle-deploy BUY-уровней ────────
        # Если idle-deploy уровень стоит > 1×step ниже текущей цены —
        # значит цена ушла вверх после деплоя (перецентровка/рост).
        # Отменяем такой уровень → TON освобождается → переразмещаем ближе.
        _step_now = self._state.step_pct or GridConfig.DEFAULT_STEP_PCT
        # Используем center_price как базу сравнения (стабильнее тик-цены).
        # Порог = 2.5×step: уровни на -1×step и -2×step (штатные позиции) не
        # перепозиционируются; только уровни от старого центра (> 2.5×step).
        _ref_price = self._state.center_price_ton or price_ton
        _reposition_thresh = _step_now * 4.0
        _stale_idle = [
            l for l in self._state.buy_levels
            if l.status == "waiting"
            and "idle-deploy" in (l.note or "").lower()
            and _ref_price > 0
            and (_ref_price - l.price_ton) / _ref_price * 100 > _reposition_thresh
        ]
        if _stale_idle:
            for _sl in _stale_idle:
                _sl.status = "cancelled_reposition"
                log.info(
                    "[Grid] 🔄 idle-deploy L%d @ %.8f перепозиционируется "
                    "(%.1f%% ниже центра %.8f, > 4.0×%.1f%%=%.1f%%)",
                    _sl.id, _sl.price_ton,
                    (_ref_price - _sl.price_ton) / _ref_price * 100,
                    _ref_price, _step_now, _reposition_thresh,
                )
            self._last_idle_deploy_ts = 0.0   # сбросить cooldown → немедленный переdeплой
            log.info("[Grid] 🔄 %d idle-deploy уровней отменены → переразмещение по актуальной цене", len(_stale_idle))

        # Чистим накопившиеся cancelled_reposition уровни (оставляем последние 5).
        _cancelled = [l for l in self._state.buy_levels
                      if l.status == "cancelled_reposition"]
        if len(_cancelled) > 5:
            _keep_ids = {l.id for l in sorted(_cancelled, key=lambda x: x.id)[-5:]}
            _before = len(self._state.buy_levels)
            self._state.buy_levels = [
                l for l in self._state.buy_levels
                if l.status != "cancelled_reposition" or l.id in _keep_ids
            ]
            log.debug("[Grid] 🧹 cancelled_reposition: удалено %d старых записей",
                      _before - len(self._state.buy_levels))

        if now - self._last_idle_deploy_ts < GridConfig.IDLE_COOLDOWN_SEC:
            return

        # ── 1. Считаем уже замороженный TON ────────────────────────────────
        frozen_buy_ton = sum(
            l.amount_ton for l in self._state.buy_levels if l.status == "waiting"
        )
        free_ton = ton_bal - frozen_buy_ton - GridConfig.GAS_RESERVE_TON

        # [УЛУЧШ] Динамический порог: max(фиксированный, 10% от суммарного баланса в TON)
        _dyn_threshold = max(
            GridConfig.IDLE_TON_THRESHOLD,
            ton_bal * GridConfig.IDLE_BALANCE_PCT,
        )
        if free_ton < _dyn_threshold:
            return

        # ── 2. AI-фильтр: при сильном SELL-сигнале не усиливаем BUY ────────
        # Аварийный режим: если BUY-уровней нет совсем, снижаем порог до 65%
        # от нормального чтобы не оставлять сетку без возможности закупки.
        _no_buy_levels = not any(l.status == "waiting" for l in self._state.buy_levels)
        _eff_min_conf = (GridConfig.AI_MIN_BUY_CONF * 0.65
                         if _no_buy_levels else GridConfig.AI_MIN_BUY_CONF)
        if ai_buy_conf < _eff_min_conf and regime not in ("SIDEWAYS", "UNKNOWN"):
            if _no_buy_levels:
                log.warning(
                    "[Grid] ⚠️ Нет BUY-уровней, AI BUY=%.0f%% < аварийного порога=%.0f%% "
                    "(режим=%s) — idle-deploy пропущен",
                    ai_buy_conf, _eff_min_conf, regime,
                )
            else:
                log.debug("[Grid] idle-deploy пропущен — AI BUY %.0f%% < %.0f%% в режиме %s",
                          ai_buy_conf, GridConfig.AI_MIN_BUY_CONF, regime)
            return

        # ── 3. Якорная цена для новых idle-deploy уровней ───────────────────
        # Всегда используем CENTER-цену как базу (стабильная точка сетки),
        # а не текущую тик-цену (волатильна, смещает уровни к тем же ценам).
        # Если есть настоящие (compound/реинвест) BUY — берём их нижнюю точку.
        _center = self._state.center_price_ton or price_ton
        waiting_buys = [l for l in self._state.buy_levels if l.status == "waiting"]
        waiting_real  = [l for l in waiting_buys
                         if "idle-deploy" not in (l.note or "").lower()]
        if waiting_real:
            # Есть настоящие (compound/реинвест) BUY — якорь по нижнему
            anchor_price = min(l.price_ton for l in waiting_real)
        else:
            # Только idle-deploy или пусто — якорь по ЦЕНТРУ (не по тику)
            anchor_price = _center

        step_pct = self._state.step_pct or GridConfig.DEFAULT_STEP_PCT

        # ── 3b. Безопасный минимум якоря ─────────────────────────────────────
        # Первый idle-deploy уровень размещается на 1 шаг НИЖЕ anchor:
        #   first_level = anchor / (1 + step/100)
        # Чтобы он не попал сразу под перепозиционирование, нужно:
        #   (center - first_level) / center < reposition_thresh / 100
        # Отсюда: anchor >= center × (1 + step/100) × (1 - reposition_thresh/100)
        _safe_anchor_min = _center * (1 + step_pct / 100) * (1 - _reposition_thresh / 100)
        if anchor_price < _safe_anchor_min:
            log.debug(
                "[Grid] idle-deploy якорь %.8f → %.8f (скорректирован вверх до безопасной зоны)",
                anchor_price, _safe_anchor_min,
            )
            anchor_price = _safe_anchor_min

        # ── 4. Определяем сколько новых уровней добавить ────────────────────
        # [УЛУЧШ] При очень большом свободном балансе добавляем больше уровней
        _max_levels = (GridConfig.IDLE_DEPLOY_MAX_LEVELS_RICH
                       if free_ton >= _dyn_threshold * 3
                       else GridConfig.IDLE_DEPLOY_MAX_LEVELS)
        n_add = min(
            _max_levels,
            int(free_ton / GridConfig.IDLE_LEVEL_TON),
        )
        if n_add <= 0:
            return

        # ── 5. Собираем уже занятые цены (BUY + DCA) чтобы не дублировать ──
        # Cancelled-уровни не блокируют — их место свободно для переразмещения.
        existing_prices = set()
        for l in self._state.buy_levels:
            if l.status != "cancelled_reposition":
                existing_prices.add(round(l.price_ton, 8))
        for l in self._state.dca_levels:
            if l.status != "cancelled_reposition":
                existing_prices.add(round(l.price_ton, 8))

        added = 0
        committed = 0.0   # реально зарезервированный TON (сумма amount_ton добавленных уровней)
        # Диапазон idle-deploy: -2001 … (min существующих - 1), чтобы никогда не повторять
        _idle_ids = [l.id for l in self._state.buy_levels if l.id <= -2000]
        next_id = (min(_idle_ids) - 1) if _idle_ids else -2001
        level_price = anchor_price / (1 + step_pct / 100)   # первый ниже anchor

        for _ in range(n_add * 3):   # итераций с запасом (пропускаем дубли)
            if added >= n_add:
                break
            rounded = round(level_price, 8)

            # ── Зональный ограничитель: не размещаем уровни за пределами ──────
            # порога перепозиционирования — они всё равно будут отменены в
            # следующем тике, создавая бесконечный цикл cancel→redeploy.
            # Уровни идут вглубь монотонно → как только текущий вышел за порог,
            # все последующие тоже выйдут → break, не continue.
            if _center > 0 and (_center - rounded) / _center * 100 >= _reposition_thresh:
                log.debug(
                    "[Grid] idle-deploy: уровень %.8f (%.1f%% от центра) ≥ порога %.1f%% — остановка",
                    rounded, (_center - rounded) / _center * 100, _reposition_thresh,
                )
                break

            # Не дублируем: пропустить если уже есть уровень ближе ±0.5%
            too_close = any(abs(p - rounded) / max(rounded, 1e-12) < 0.005
                            for p in existing_prices)
            if too_close:
                level_price /= (1 + step_pct / 100)
                continue

            # Профит-гейт: BUY→SELL цикл должен быть прибыльным с учётом газа
            # Шаг должен покрыть комиссию (оба плеча) — иначе смысла нет в принципе
            _cycle_factor = (1 + step_pct / 100) * (1 - GridConfig.FEE_PCT) ** 2 - 1
            if _cycle_factor <= 0:
                level_price /= (1 + step_pct / 100)
                continue
            # Минимальный TON при котором газ окупается: gas×2 / cycle_factor
            _min_ton_for_profit = math.ceil(
                GridConfig.GAS_PER_TRADE_TON * 2 / _cycle_factor * 10) / 10  # округл. вверх до 0.1

            # Depth-weighted sizing: чем глубже уровень — тем больший ордер
            # Смысл: глубокий дип = выгодная цена = стоит купить больше.
            depth_steps = (anchor_price / max(rounded, 1e-12) - 1) * 100 / max(step_pct, 0.1)
            depth_mult  = min(GridConfig.IDLE_DEPTH_MAX_MULT,
                              1.0 + depth_steps * GridConfig.IDLE_DEPTH_BOOST)
            remaining   = free_ton - committed   # реальный остаток с учётом уже добавленных
            base_amount = min(GridConfig.IDLE_LEVEL_TON, remaining)
            # Поднимаем до минимально прибыльного (газ-inclusive)
            base_amount = max(base_amount, _min_ton_for_profit)
            amount_ton  = round(base_amount * depth_mult, 2)
            if amount_ton < GridConfig.min_profitable_order_ton(
                    self._state.step_pct or GridConfig.DEFAULT_STEP_PCT):
                break
            # Бюджетный контроль: точная проверка по реально оставшемуся балансу
            if amount_ton > remaining:
                level_price /= (1 + step_pct / 100)
                continue

            self._state.buy_levels.append(GridLevel(
                id=next_id - added,
                side="buy",
                price_ton=rounded,
                amount_grinch=0.0,
                amount_ton=amount_ton,
                status="waiting",
                note=(f"idle-deploy | free={free_ton:.1f} TON | "
                      f"AI BUY={ai_buy_conf:.0f}% | regime={regime}"),
                owner="grid",
            ))
            existing_prices.add(rounded)
            committed += amount_ton   # точный учёт реально зарезервированного TON
            added += 1
            level_price /= (1 + step_pct / 100)

        if added:
            self._last_idle_deploy_ts    = now
            self._last_idle_deploy_price = price_ton  # [УЛУЧШ] для price-movement reset cooldown
            log.info(
                "[Grid] 💰 idle-deploy: +%d BUY-уровней из %.1f свободных TON "
                "(frozen=%.1f TON | AI BUY=%.0f%% | режим=%s)",
                added, free_ton, frozen_buy_ton, ai_buy_conf, regime,
            )

    def _ensure_near_price_sell(self, price_ton: float, regime: str):
        """Near-price density: если нет SELL в пределах 1.5×step → создать из дальнего.

        Раз в тик пассивно проверяет: есть ли хотя бы один ожидающий SELL-уровень
        рядом с ценой (≤ 1.5 шага вверх). Если нет — берёт 35% GRINCH у самого
        крупного дальнего уровня (> 3×step) и создаёт новый ближний. Безопасные
        ограничения: min_grinch_value ≥ MIN_ORDER_TON, min_source ≥ 100К GRINCH.
        Не работает в PUMP/DISTRIBUTION/POST_PUMP.
        """
        if regime in ("PUMP", "DISTRIBUTION", "POST_PUMP"):
            return
        step = self._state.step_pct
        if step <= 0:
            return

        near_limit = price_ton * (1 + step * 1.5 / 100)
        near_sells = [
            l for l in self._state.sell_levels
            if l.status == "waiting"
            and price_ton < l.price_ton <= near_limit
            and l.amount_grinch >= 100
        ]
        if near_sells:
            return  # Уже есть — ничего делать не нужно

        # Не создаём дубликат на той же цене после быстрого исполнения.
        # Иначе при неизменной цене каждый тик мог брать ещё 35% у дальнего
        # уровня и складывать новые SELL с одним и тем же триггером.
        near_price = price_ton * (1 + step / 100)
        same_price_level = any(
            l.status not in ("cancelled_reposition", "skipped_small")
            and l.price_ton > 0
            and abs(l.price_ton / near_price - 1.0) < 0.005
            for l in self._state.sell_levels
        )
        if same_price_level:
            return

        # Ищем самый крупный дальний уровень (> 3× step от цены)
        far_limit = price_ton * (1 + step * 3 / 100)
        far_sells = [
            l for l in self._state.sell_levels
            if l.status == "waiting"
            and l.owner == "grid"
            and l.price_ton > far_limit
            and l.amount_grinch >= 100_000     # минимум 100К GRINCH для донорства
        ]
        if not far_sells:
            return

        source = max(far_sells, key=lambda l: l.amount_grinch)
        split_grinch = round(source.amount_grinch * 0.35, 2)

        # Проверяем минимальный размер ордера
        if split_grinch * price_ton < GridConfig.MIN_ORDER_TON:
            return

        source.amount_grinch = round(source.amount_grinch - split_grinch, 2)

        cycle_ids = [l.id for l in self._state.sell_levels if l.id >= 100]
        new_id = (max(cycle_ids) + 1) if cycle_ids else 101

        self._state.sell_levels.append(GridLevel(
            id=new_id, side="sell",
            price_ton=round(near_price, 8),
            amount_grinch=split_grinch,
            amount_ton=0.0,
            status="waiting",
            note=f"near-density (35% от L{source.id}) @ {near_price:.6f}",
            owner="grid",
        ))
        log.info("[Grid] 🎯 Near-density SELL L%d @ %.6f (+%.1f%%) с %.0f GRINCH ← L%d",
                 new_id, near_price, step, split_grinch, source.id)

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
                if (l.status == "waiting" and l.owner == "grid"
                        and l.price_ton < price_ton):
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
                        owner="grid",
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

    def _inject_market_context_to_grid_ai(self, atr_pct: float, regime: str):
        """v5: Извлечь рыночный контекст из последнего DB-тика и передать в GridAI."""
        if not self._grid_ai:
            return
        try:
            import db_store as _ds
            ticks = _ds.ticks_get_recent(3)
            if not ticks:
                return
            t = ticks[0]  # последний тик

            # ── Рыночные фичи из DB-тика ────────────────────────────────
            rsi      = float(t.get("rsi") or 50.0)
            bb_pct   = float(t.get("bb_pct") or 0.5)
            vol_r    = float(t.get("vol_ratio") or 1.0)
            macd_h   = float(t.get("macd_hist") or 0.0)
            pump_str = str(t.get("pump") or "NONE")
            pump_sc  = 0.0
            if pump_str not in ("NONE", "", "?"):
                try:
                    # pump может быть "PUMP:75" или "DISTRIBUTION" или conf числом
                    pump_sc = float(pump_str.split(":")[-1]) if ":" in pump_str else 60.0
                except Exception:
                    pump_sc = 50.0

            # RSI velocity из 3 последних тиков
            rsi_vel = 0.0
            if len(ticks) >= 3:
                try:
                    rsi_vel = (float(ticks[0].get("rsi") or 50) -
                               float(ticks[2].get("rsi") or 50))
                except Exception:
                    pass

            # Volume trend: сравниваем 2 последних тика
            vol_trend = 0.0
            if len(ticks) >= 2:
                try:
                    v1 = float(ticks[0].get("vol_ratio") or 1.0)
                    v2 = float(ticks[1].get("vol_ratio") or 1.0)
                    vol_trend = max(-1.0, min(1.0, (v1 - v2) / max(v2, 0.1)))
                except Exception:
                    pass

            # Order flow из coin_info (если доступен)
            order_buy = 0.5
            order_net = 0.0
            try:
                from coin_info import get_snapshot as _ci_snap
                ci = _ci_snap()
                if ci:
                    # coin_info возвращает buys_h1/sells_h1 (DexScreener txns за 1 час)
                    buys = float(ci.get("buys_h1") or ci.get("buys_h6") or ci.get("buys_h24") or 0)
                    sells = float(ci.get("sells_h1") or ci.get("sells_h6") or ci.get("sells_h24") or 0)
                    total = buys + sells
                    if total > 0:
                        order_buy = buys / total
                        order_net = (buys - sells) / total
            except Exception:
                pass

            # EMA trend (crude: from regime)
            ema_cross = {
                "TREND_UP": 0.03, "VOLATILE": 0.01,
                "SIDEWAYS": 0.0, "SQUEEZE": 0.0, "RANGING": 0.0,
                "TREND_DOWN": -0.03, "DOWNTREND": -0.06,
                "PUMP": 0.08, "POST_PUMP": -0.02, "DISTRIBUTION": -0.02,
            }.get(regime, 0.0)

            mkt = {
                "rsi":                   rsi,
                "rsi_vel":               rsi_vel,
                "macd_h":                macd_h,
                "bb_pos":                bb_pct,
                "bb_width":              abs(macd_h) * 0.01 + 0.05,  # approx
                "bb_squeeze":            regime in ("SQUEEZE",),
                "vol_ratio":             vol_r,
                "vol_trend":             vol_trend,
                "ema_cross":             ema_cross,
                "order_flow_buy_ratio":  order_buy,
                "order_flow_net":        order_net,
                "pump_score":            pump_sc,
                "liquidity_score":       50.0,  # default — обновляется через liquidity_guard
            }
            self._grid_ai.set_market_context(mkt)

            # ── MTF контекст (оцениваем по нескольким тикам) ─────────────
            try:
                ticks_long = _ds.ticks_get_recent(20)
                if len(ticks_long) >= 5:
                    regimes_5 = [str(t2.get("regime") or "UNKNOWN")
                                 for t2 in ticks_long[:5]]
                    down_count = sum(1 for r in regimes_5
                                     if r in ("TREND_DOWN", "DOWNTREND",
                                              "DISTRIBUTION", "POST_PUMP"))
                    up_count   = sum(1 for r in regimes_5
                                     if r in ("TREND_UP", "PUMP"))

                    t4h = 1 if up_count >= 3 else (-1 if down_count >= 3 else 0)

                    # 1d trend из более длинной истории
                    t1d = 0
                    if len(ticks_long) >= 15:
                        reg_15 = [str(t2.get("regime") or "UNKNOWN")
                                  for t2 in ticks_long[:15]]
                        dn15 = sum(1 for r in reg_15
                                   if r in ("TREND_DOWN", "DOWNTREND"))
                        up15 = sum(1 for r in reg_15
                                   if r in ("TREND_UP", "PUMP"))
                        t1d = 1 if up15 >= 8 else (-1 if dn15 >= 8 else 0)

                    self._grid_ai.set_mtf_context({
                        "trend_4h": t4h,
                        "trend_1d": t1d,
                    })
            except Exception:
                pass

        except Exception as e:
            log.debug("[Grid] inject_mkt_ctx error: %s", e)

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
        """Возвращает (buy_conf%, sell_conf%) для управления сеткой.

        Основной сигнал берём из BrainFusion: туда AI Advisor передаёт
        результат выбранного LLM-провайдера (сейчас OpenAI GPT-4o), а также
        локальные ML/TA/scanner-сигналы. Сырой сигнал из последнего DB-тика
        остаётся безопасным fallback и сохраняет локальную защиту, если
        BrainFusion ещё не получил свежий консенсус.
        """
        try:
            import db_store as _ds
            # Сначала читаем локальный AI-сигнал: он нужен как fallback и
            # сохраняет уже существующие защитные пороги Grid.
            ticks = _ds.ticks_get_recent(1)
            raw_buy, raw_sell = 0.0, 0.0
            if ticks:
                t         = ticks[0]
                sig       = t.get("ai_sig") or t.get("final") or "HOLD"
                conf      = float(t.get("ai_conf") or 0.0)
                prob_up   = float(t.get("prob_up")   or 0.0)
                prob_down = float(t.get("prob_down") or 0.0)
                if sig == "BUY":
                    # conf — уверенность BUY; prob_down — вероятность падения
                    raw_buy  = conf if conf > 0 else prob_up
                    raw_sell = prob_down
                elif sig == "SELL":
                    # conf — уверенность SELL; prob_up — вероятность роста
                    raw_buy  = prob_up
                    raw_sell = conf if conf > 0 else prob_down
                else:
                    # HOLD — возвращаем обе вероятности
                    raw_buy, raw_sell = prob_up, prob_down

            # BrainFusion получает LLM-вердикт из ai_advisor после каждого
            # запуска советника. Поэтому OpenAI влияет на сетку через
            # консенсус, но не вызывается на каждом 10–20-секундном тике.
            try:
                import brain_fusion as _bf
                fusion = _bf.get_fusion_signal()
                action = (getattr(fusion, "action", "") or "").upper()
                fusion_conf = max(
                    0.0, min(100.0, float(
                        getattr(fusion, "consensus_conf", 0.0) or 0.0
                    ))
                )
                if action == "BUY" and fusion_conf > 0:
                    raw_buy = max(raw_buy, fusion_conf)
                elif action == "SELL" and fusion_conf > 0:
                    raw_sell = max(raw_sell, fusion_conf)
                if action in ("BUY", "SELL") and fusion_conf > 0:
                    log.debug(
                        "[Grid] BrainFusion %s %.0f%% включён в AI-фильтр "
                        "(raw BUY=%.0f%% SELL=%.0f%%)",
                        action, fusion_conf, raw_buy, raw_sell,
                    )
            except Exception as _fusion_err:
                log.debug("[Grid] BrainFusion signal unavailable: %s", _fusion_err)

            return raw_buy, raw_sell
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
        """Возвращает физические (GRINCH, TON) балансы кошелька."""
        try:
            from dedust_client import get_shared_balance
            bal = get_shared_balance()
            ton_bal    = float(bal.get("TON", bal.get("ton", 0)))
            grinch_bal = float(bal.get("GRINCH", bal.get("grinch", 0)))
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

    def _get_total_dca_reserved_grinch(self) -> float:
        """Основной Trader-DCA плюс DCA-позиции, открытые Grid."""
        return max(
            0.0,
            self._get_dca_reserved_grinch() +
            float(self._state.dca_reserved_grinch or 0.0),
        )

    def _available_grinch_for_level(self, level: GridLevel) -> float:
        """Возвращает доступный физический баланс именно владельца уровня.

        Резервы являются логическим лимитом, а кошелёк — физическим верхним
        пределом. Это защищает от продажи чужой позиции и от stale-состояния
        после внешней транзакции.
        """
        wallet_grinch, _ = self._get_balances()
        grid_reserved = max(0.0, float(self._state.grid_reserved_grinch or 0.0))
        dca_reserved = self._get_total_dca_reserved_grinch()
        if level.owner == "dca":
            return max(0.0, min(dca_reserved, wallet_grinch - grid_reserved))
        return max(0.0, min(grid_reserved, wallet_grinch - dca_reserved))

    def _heuristic_step(self, atr_pct: float, regime: str = "UNKNOWN") -> float:
        """Эвристический шаг без GridAI."""
        if regime in ("PUMP",):
            return 8.0
        if regime in ("DISTRIBUTION", "POST_PUMP"):
            return 6.0
        if regime in ("TREND_UP", "UPTREND"):
            return 8.0 if atr_pct >= 4.0 else 6.0
        if regime in ("TREND_DOWN", "DOWNTREND"):
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
        """Атомарно сохраняет состояние сетки.

        Нельзя писать прямо в STATE_FILE через ``open(..., "w")``: при
        перезаписи файл сначала обнуляется, и читатель (или рестарт
        контейнера) может увидеть пустой/неполный JSON. Кроме того, часть
        вызовов сохранения происходит из фонового потока, поэтому снимок
        состояния нужно делать под тем же lock, что и его изменение.
        """
        tmp_path = f"{STATE_FILE}.tmp"
        try:
            state_dir = os.path.dirname(STATE_FILE) or "."
            os.makedirs(state_dir, exist_ok=True)
            with self._lock:
                payload = self._state.to_dict()
            # NamedTemporaryFile в том же каталоге гарантирует, что
            # os.replace() останется атомарным даже на mounted volume.
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=state_dir,
                prefix=".grid_state.",
                suffix=".tmp",
                delete=False,
            ) as f:
                tmp_path = f.name
                json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, STATE_FILE)
        except Exception as e:
            log.warning("[Grid] Сохранение state: %s", e)
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass

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
