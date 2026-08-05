"""
grid_ai.py v4 — QuantumGrid AI: Нейро-ансамбль для сеточной торговли

Улучшения v4 относительно v3:
  • 20 признаков (было 13): + consecutive_losses, compound_mult, drawdown_pct,
    recent_win_rate_5, fill_density_1h, atr_normalized, regime_confidence
  • 5 базовых регрессоров шага: RF + ExtraTrees + GB + HistGB + Ridge
    + мета-стекер (Ridge над предсказаниями ансамбля)
  • 4 классификатора DCA: RF + ExtraTrees + HistGB + LogisticRegression
  • Инкрементальный SGD-апдейт после каждой сделки (без полного ретрейна)
  • Режимо-специфичные границы шага: SQUEEZE [3,5.5], TREND_UP [6,10], VOLATILE [5,10]
  • Режимо-взвешенный Kelly: отдельный Kelly по каждому режиму + глобальный
  • Новые API: get_sell_target_pct(), get_risk_level(), should_pause_buying()
  • record_fill() принимает compound_mult= и drawdown_pct= (опциональные kwargs)
  • Полная обратная совместимость с v3 experience-файлами (новые поля = defaults)
"""

import os
import gc
import json
import time
import math
import threading
import logging
from collections import deque
from typing import Optional, List, Dict, Tuple

log = logging.getLogger("grid_ai")

DATA_DIR        = os.getenv("DATA_DIR", ".")
EXPERIENCE_FILE = os.path.join(DATA_DIR, "grid_ai_experience.json")

# Минимум примеров для первого обучения
MIN_SAMPLES = 5
# Полужизнь весов (дни): через 7 дней вес = 0.5
DECAY_HALFLIFE_DAYS = 7.0
# Размерность вектора признаков (не менять без перестройки всех моделей)
FEAT_DIM = 20

# Режимо-специфичные границы шага [min%, max%]
REGIME_STEP_BOUNDS: Dict[str, Tuple[float, float]] = {
    "SQUEEZE":      (3.0,  5.5),
    "SIDEWAYS":     (3.5,  7.0),
    "RANGING":      (3.5,  7.0),
    "VOLATILE":     (5.0, 10.0),
    "TREND_UP":     (6.0, 10.0),
    "TREND_DOWN":   (4.0,  8.0),
    "DOWNTREND":    (4.0,  8.0),
    "PUMP":         (7.0, 10.0),
    "DISTRIBUTION": (6.0, 10.0),
    "POST_PUMP":    (5.0,  8.5),
    "UNKNOWN":      (3.5,  8.0),
}


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _exp_decay_weight(ts: float, now: float) -> float:
    """Экспоненциальный вес по возрасту записи (в днях)."""
    age_days = max(0.0, (now - ts) / 86400.0)
    return math.exp(-math.log(2) * age_days / DECAY_HALFLIFE_DAYS)


def _regime_enc(regime: str) -> int:
    """Целочисленное кодирование режима."""
    return {
        "TREND_UP":     2,
        "VOLATILE":     1,
        "SIDEWAYS":     0,
        "SQUEEZE":      0,
        "RANGING":      0,
        "UNKNOWN":      0,
        "TREND_DOWN":  -1,
        "DOWNTREND":   -2,
        "DISTRIBUTION":-1,
        "POST_PUMP":   -3,
        "PUMP":         3,
    }.get(regime if isinstance(regime, str) else "UNKNOWN", 0)


# ─── Основной класс ───────────────────────────────────────────────────────────

class GridAI:
    """Самообучающийся AI-оптимизатор сеточной торговли v4.

    Публичное API (обратно совместимо с v3):
      get_optimal_step(atr_pct, regime, min_step, max_step) → float
      get_dca_confidence(atr_pct, regime, drawdown_pct, price_vs_center_pct) → float
      get_dca_size_multiplier(cycle_num, win_rate) → float
      get_pyramid_weights(n_levels) → List[float]
      update_regime(regime)
      record_fill(side, step_used, atr_pct, regime, profit_ton, profit_pct,
                  is_dca=False, compound_mult=1.0, drawdown_pct=0.0)
      get_stats() → dict

    Новые API v4:
      get_sell_target_pct(step_pct, regime, atr_pct) → float
      get_risk_level() → int  (0=low, 1=medium, 2=high)
      should_pause_buying(regime, drawdown_pct, ai_sell_conf) → bool
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._experience: List[dict] = []

        # ── Модели ансамбля — шаг ─────────────────────────────────────────
        self._step_rf    = None   # RandomForestRegressor
        self._step_et    = None   # ExtraTreesRegressor (v4 new)
        self._step_gb    = None   # GradientBoostingRegressor
        self._step_hgb   = None   # HistGradientBoostingRegressor (v4 new)
        self._step_ridge = None   # Ridge (быстрый baseline)
        self._step_meta  = None   # Мета-стекер (Ridge над предсказаниями)

        # Инкрементальный регрессор шага (SGD, обновляется каждую сделку)
        self._step_sgd   = None

        # ── Модели ансамбля — DCA-решение ─────────────────────────────────
        self._dca_rf     = None   # RandomForestClassifier
        self._dca_et     = None   # ExtraTreesClassifier (v4 new)
        self._dca_hgb    = None   # HistGradientBoostingClassifier (v4 new)
        self._dca_lr     = None   # LogisticRegression

        # Инкрементальный классификатор DCA (SGD, обновляется каждую сделку)
        self._dca_sgd    = None

        # ── Скользящая статистика (v3) ────────────────────────────────────
        self._win_streak:      int   = 0
        self._recent_profits:  deque = deque(maxlen=10)
        self._regime_dur:      int   = 0
        self._last_regime:     str   = ""

        # ── Новые трекеры v4 ──────────────────────────────────────────────
        self._consecutive_losses: int   = 0
        self._recent_atrs:        deque = deque(maxlen=20)   # последние 20 ATR
        self._last_compound_mult: float = 1.0
        # Per-regime статистика прибылей: {regime: [profit, ...]}
        self._regime_profits: Dict[str, list] = {}

        # Калиброванный MIN_STEP (пересчитывается по реальным данным)
        self.calibrated_min_step: float = 4.0
        # Kelly-множитель шага (глобальный + per-regime)
        self._kelly_mult: float = 1.0
        self._kelly_by_regime: Dict[str, float] = {}

        self._trained      = False
        self._last_train_n = 0

        self._load_experience()
        if len(self._experience) >= MIN_SAMPLES:
            self._train()
        log.info("[GridAI v4] Инициализирован. Примеров: %d, обучен: %s, "
                 "min_step=%.2f%% kelly=%.3f",
                 len(self._experience), self._trained,
                 self.calibrated_min_step, self._kelly_mult)

    # ══════════════════════════════════════════════════════════════════════════
    # Публичное API
    # ══════════════════════════════════════════════════════════════════════════

    def get_optimal_step(self, atr_pct: float, regime: str = "SIDEWAYS",
                         min_step: float = None, max_step: float = 10.0) -> float:
        """Предсказать оптимальный шаг сетки.

        v4: использует 5 моделей + мета-стекинг; применяет режимо-специфичные
        границы шага и режимо-взвешенный Kelly.
        """
        if min_step is None:
            min_step = self.calibrated_min_step

        # Режимо-специфичные границы (v4)
        r_min, r_max = REGIME_STEP_BOUNDS.get(
            regime if isinstance(regime, str) else "UNKNOWN",
            (min_step, max_step))
        effective_min = max(min_step, r_min)
        effective_max = min(max_step, r_max)
        if effective_min >= effective_max:
            effective_max = effective_min + 1.0

        heuristic = self._heuristic_step(atr_pct, regime)
        heuristic = max(effective_min, min(effective_max, heuristic))

        if not self._trained:
            return heuristic

        try:
            feat  = self._make_features(atr_pct, regime)
            preds = self._predict_step_ensemble(feat)

            if not preds:
                return heuristic

            # Среднее ансамбля (base + meta, если есть)
            ml_pred = sum(preds) / len(preds)
            ml_pred = max(effective_min, min(effective_max,
                                              round(ml_pred * 2) / 2))

            # Режимо-взвешенный Kelly (v4)
            regime_kelly = self._kelly_by_regime.get(regime, self._kelly_mult)
            blended_kelly = 0.6 * self._kelly_mult + 0.4 * regime_kelly
            ml_pred = max(effective_min,
                          min(effective_max, ml_pred * blended_kelly))
            ml_pred = round(ml_pred * 2) / 2

            # Плавный переход по числу примеров
            n = len(self._experience)
            weight = min(1.0, (n - MIN_SAMPLES) / 45.0)
            blended = heuristic * (1 - weight) + ml_pred * weight

            result = max(effective_min, min(effective_max,
                                             round(blended * 2) / 2))
            log.debug("[GridAI v4] step: h=%.1f ml=%.1f k=%.2f → %.1f "
                      "(ATR=%.2f%% regime=%s n=%d w=%.2f)",
                      heuristic, ml_pred, blended_kelly, result,
                      atr_pct, regime, n, weight)
            return result

        except Exception as e:
            log.warning("[GridAI v4] predict_step error: %s", e)
            return heuristic

    def get_dca_confidence(self, atr_pct: float, regime: str,
                           drawdown_pct: float,
                           price_vs_center_pct: float) -> float:
        """Уверенность что стоит делать DCA-добавление (0–100%).

        Жёсткие блокировки: PUMP/DISTRIBUTION/POST_PUMP или просадка > 50%.
        Мягкий порог: режимо-зависимый — в боковике агрессивнее, в тренде вниз — нет.
        NOTE: drawdown_pct и price_vs_center_pct — постпредикционные множители,
              не входят в вектор признаков (правило v3 сохранено).
        """
        if regime in ("PUMP", "DISTRIBUTION", "POST_PUMP"):
            return 0.0
        if drawdown_pct > 50.0:
            return 0.0

        regime_bias = {
            "SIDEWAYS":   1.20,
            "SQUEEZE":    1.15,
            "RANGING":    1.10,
            "UNKNOWN":    1.00,
            "VOLATILE":   0.90,
            "TREND_UP":   0.80,
            "TREND_DOWN": 0.50,
            "DOWNTREND":  0.40,
        }
        bias = regime_bias.get(regime, 1.0)

        # Блокировка если risk_level = HIGH
        if self.get_risk_level() >= 2:
            bias *= 0.5

        # Эвристика без модели
        if not self._trained or (self._dca_rf is None and self._dca_et is None
                                  and self._dca_hgb is None):
            raw = (60.0 * bias if atr_pct >= 2.0 and drawdown_pct < 40.0
                   else 25.0)
            if drawdown_pct > 35.0:
                raw *= 0.6
            return round(max(0.0, min(100.0, raw)), 1)

        try:
            feat  = self._make_features(atr_pct, regime)
            probs = self._predict_dca_ensemble(feat)

            if not probs:
                return 25.0

            prob = (sum(probs) / len(probs)) * bias

            # Постпредикционные множители (runtime-only контекст)
            if drawdown_pct > 35.0:
                prob *= 0.6
            elif drawdown_pct > 25.0:
                prob *= 0.8

            # Штраф за приближение к центру снизу
            if price_vs_center_pct > 5.0:
                prob *= 0.85

            # Бонус за серию побед
            if self._win_streak >= 3:
                prob = min(1.0, prob * 1.1)

            # Инкрементальный SGD — дополнительный сигнал
            if self._dca_sgd is not None:
                try:
                    sgd_prob = float(
                        self._dca_sgd.predict_proba([feat])[0][1])
                    prob = 0.75 * prob + 0.25 * sgd_prob
                except Exception:
                    pass

            return round(max(0.0, min(100.0, prob * 100)), 1)

        except Exception as e:
            log.warning("[GridAI v4] dca_confidence error: %s", e)
            return 25.0

    def get_dca_size_multiplier(self, cycle_num: int, win_rate: float) -> float:
        """Рекомендуемый множитель размера DCA-ордера (Kelly-based).

        cycle_num: номер DCA (1, 2, 3 ...)
        win_rate:  процент прибыльных fills (0–100)
        Returns:   0.7 – 2.0
        """
        kelly = self._kelly_mult if self._kelly_mult > 0 else 1.0
        level_decay = max(0.7, 1.0 - (cycle_num - 1) * 0.1)

        if win_rate >= 80:
            wr_mult = 1.15
        elif win_rate >= 60:
            wr_mult = 1.0
        elif win_rate < 40:
            wr_mult = 0.8
        else:
            wr_mult = 0.9

        # v4: дополнительный штраф при высоком risk_level
        risk_penalty = [1.0, 0.9, 0.75][min(self.get_risk_level(), 2)]

        result = kelly * level_decay * wr_mult * risk_penalty
        return round(max(0.7, min(2.0, result)), 2)

    def get_pyramid_weights(self, n_levels: int) -> List[float]:
        """Веса распределения GRINCH по уровням (пирамида: больше на нижних).

        v4: учитывает текущий риск-уровень — при HIGH риске выравниваем пирамиду.
        """
        if n_levels <= 0:
            return []
        if n_levels == 1:
            return [1.0]

        risk = self.get_risk_level()
        if risk >= 2:
            # Плоское распределение при высоком риске
            max_ratio, min_ratio = 1.10, 0.90
        elif risk == 1:
            max_ratio, min_ratio = 1.20, 0.80
        else:
            max_ratio, min_ratio = 1.30, 0.70

        weights = [
            max_ratio - (max_ratio - min_ratio) * i / (n_levels - 1)
            for i in range(n_levels)
        ]
        avg = sum(weights) / len(weights)
        return [round(w / avg, 4) for w in weights]

    def update_regime(self, regime: str):
        """Обновить счётчик длительности текущего режима."""
        if regime != self._last_regime:
            self._regime_dur  = 0
            self._last_regime = regime
        else:
            self._regime_dur += 1

    def record_fill(self, side: str, step_used: float, atr_pct: float,
                    regime: str, profit_ton: float, profit_pct: float,
                    is_dca: bool = False,
                    compound_mult: float = 1.0,
                    drawdown_pct: float = 0.0):
        """Записать результат исполненного уровня.

        v4: принимает compound_mult= и drawdown_pct= (опциональные kwargs).
        Все новые поля имеют defaults → полная обратная совместимость.
        """
        now     = time.time()
        atr_pct = _safe_float(atr_pct)

        # ── Производные признаки (вычисляются ДО обновления трекеров) ────────
        recent = list(self._recent_profits)
        recent_avg = sum(recent) / len(recent) if recent else 0.0
        profit_momentum = 0.0
        if len(recent) >= 3:
            profit_momentum = (recent[-1] - recent[0]) / max(
                abs(recent[0]) + 1e-6, 1e-6)
            profit_momentum = max(-2.0, min(2.0, profit_momentum))

        # v4: recent win_rate_5, fill_density, ATR normalization
        rp5 = list(self._recent_profits)[-5:]
        recent_win_rate_5 = sum(1 for x in rp5 if x > 0) / max(len(rp5), 1)

        mean_atr = (sum(self._recent_atrs) / len(self._recent_atrs)
                    if self._recent_atrs else atr_pct)
        atr_norm = atr_pct / max(mean_atr, 0.5) if mean_atr > 0 else 1.0

        fill_density = self._compute_fill_density()
        regime_conf  = min(self._regime_dur / 20.0, 1.0)

        entry = {
            # v3 поля
            "ts":             now,
            "side":           side,
            "step_used":      step_used,
            "atr_pct":        atr_pct,
            "regime":         regime,
            "profit_ton":     profit_ton,
            "profit_pct":     profit_pct,
            "is_dca":         is_dca,
            "is_profitable":  1 if profit_ton > 0 else 0,
            "win_streak":       self._win_streak,
            "recent_avg_profit": round(recent_avg, 4),
            "profit_momentum":   round(profit_momentum, 4),
            "hour":             int(time.strftime("%H", time.gmtime(now))),
            "regime_duration":  self._regime_dur,
            # v4 новые поля
            "consecutive_losses": self._consecutive_losses,
            "compound_mult":      round(max(1.0, min(2.0, compound_mult)), 4),
            "drawdown_pct":       round(max(0.0, min(60.0, drawdown_pct)), 2),
            "recent_win_rate_5":  round(recent_win_rate_5, 3),
            "fill_density_1h":    round(fill_density, 2),
            "atr_normalized":     round(max(0.3, min(3.0, atr_norm)), 4),
            "regime_confidence":  round(regime_conf, 3),
        }

        # ── Обновляем трекеры ПОСЛЕ создания записи ───────────────────────
        self._recent_profits.append(profit_ton)
        self._recent_atrs.append(atr_pct)
        self._last_compound_mult = compound_mult

        if profit_ton > 0:
            self._win_streak        += 1
            self._consecutive_losses = 0
        else:
            self._win_streak         = 0
            self._consecutive_losses += 1

        # Per-regime статистика (только продажи)
        if side == "sell":
            if regime not in self._regime_profits:
                self._regime_profits[regime] = []
            self._regime_profits[regime].append(profit_ton)
            # Хранить только последние 50 по каждому режиму
            if len(self._regime_profits[regime]) > 50:
                self._regime_profits[regime] = self._regime_profits[regime][-50:]

        with self._lock:
            self._experience.append(entry)
            if len(self._experience) > 2000:
                self._experience = self._experience[-2000:]
            self._save_experience()

            # Инкрементальный апдейт SGD-моделей (без полного ретрейна)
            self._incremental_update(entry)

            # Полный ретрейн только если число примеров изменилось
            if (len(self._experience) >= MIN_SAMPLES and
                    len(self._experience) != self._last_train_n):
                self._train()

        log.info("[GridAI v4] 📝 Fill: side=%s step=%.1f%% profit=%+.4f TON "
                 "(%.2f%%) streak=%d consec_loss=%d n=%d обучен=%s",
                 side, step_used, profit_ton, profit_pct,
                 self._win_streak, self._consecutive_losses,
                 len(self._experience), self._trained)

    # ── Новые API v4 ──────────────────────────────────────────────────────────

    def get_sell_target_pct(self, step_pct: float, regime: str,
                            atr_pct: float) -> float:
        """Оптимальный целевой % SELL выше цены покупки.

        По умолчанию = step_pct, но режимо-адаптивно:
          SQUEEZE/SIDEWAYS → 0.85× (быстрый выход)
          TREND_UP/VOLATILE → 1.15× (дать прибыли расти)
          PUMP → 1.3× (максимальная жадность)
          TREND_DOWN/POST_PUMP → 0.75× (консервативный выход)
        Учитывает ATR — при высоком ATR позволяет больший target.
        """
        regime_mult = {
            "SQUEEZE":      0.85,
            "SIDEWAYS":     0.90,
            "RANGING":      0.90,
            "UNKNOWN":      1.00,
            "VOLATILE":     1.10,
            "TREND_UP":     1.15,
            "TREND_DOWN":   0.80,
            "DOWNTREND":    0.75,
            "POST_PUMP":    0.75,
            "DISTRIBUTION": 0.85,
            "PUMP":         1.30,
        }.get(regime if isinstance(regime, str) else "UNKNOWN", 1.0)

        # ATR-бонус: при ATR > 4% расширяем target до +10%
        atr_bonus = 1.0 + max(0.0, min(0.10, (_safe_float(atr_pct) - 3.0) / 30.0))

        result = step_pct * regime_mult * atr_bonus
        return round(max(step_pct * 0.7, min(step_pct * 1.5, result)), 2)

    def get_risk_level(self) -> int:
        """Текущий уровень риска по состоянию AI.

        Returns:
          0 — LOW:    нормальная работа, можно быть агрессивнее
          1 — MEDIUM: осторожнее, без повышения размеров
          2 — HIGH:   тормозим BUY, уменьшаем позиции
        """
        # Высокий риск: 4+ убытка подряд
        if self._consecutive_losses >= 4:
            return 2

        # Высокий риск: последние 5 сделок — только убытки
        rp5 = list(self._recent_profits)[-5:]
        if len(rp5) >= 5 and sum(1 for x in rp5 if x > 0) == 0:
            return 2

        # Средний риск: 2-3 убытка подряд
        if self._consecutive_losses >= 2:
            return 1

        # Средний риск: < 40% winrate за последние 5
        if len(rp5) >= 5 and sum(1 for x in rp5 if x > 0) / len(rp5) < 0.4:
            return 1

        return 0

    def should_pause_buying(self, regime: str, drawdown_pct: float,
                            ai_sell_conf: float) -> bool:
        """Мультикритериальная рекомендация приостановить покупки.

        Бот может игнорировать эту рекомендацию — это совет, не команда.
        """
        # Всегда пауза в PUMP/DISTRIBUTION
        if regime in ("PUMP", "DISTRIBUTION"):
            return True

        # Высокий риск + сильная просадка
        if self.get_risk_level() >= 2 and drawdown_pct > 25.0:
            return True

        # AI сигнализирует о продаже + мы в просадке
        if ai_sell_conf >= 0.75 and drawdown_pct > 15.0:
            return True

        # 5+ убытков подряд — жёсткая пауза
        if self._consecutive_losses >= 5:
            return True

        return False

    def get_stats(self) -> dict:
        """Расширенная статистика для дашборда и /api/grid/status.
        v4: + per-regime breakdown, risk_level, SGD-info, v4 features summary.
        """
        with self._lock:
            now  = time.time()
            exp  = self._experience
            sells = [e for e in exp if e.get("side") == "sell"]
            buys  = [e for e in exp if e.get("side") == "buy"]

            if not exp:
                return {"trained": False, "samples": 0, "version": "v4"}

            profits = [e["profit_ton"] for e in sells if "profit_ton" in e]
            wins    = [p for p in profits if p > 0]
            losses  = [p for p in profits if p <= 0]
            win_rate = len(wins) / len(profits) * 100 if profits else 0

            recent_sells = sells[-20:]
            recent_wins  = sum(1 for e in recent_sells if e.get("profit_ton", 0) > 0)
            recent_wr    = recent_wins / len(recent_sells) * 100 if recent_sells else 0

            steps = [e["step_used"] for e in sells if "step_used" in e]
            avg_step = sum(steps) / len(steps) if steps else 0

            if len(profits) >= 10:
                prev5 = sum(profits[-10:-5]) / 5
                last5 = sum(profits[-5:]) / 5
                trend = "📈" if last5 > prev5 else "📉"
            else:
                trend = "—"

            kelly_edge = 0.0
            if wins and losses:
                avg_w = sum(wins) / len(wins)
                avg_l = sum(abs(l) for l in losses) / len(losses)
                p     = len(wins) / len(profits)
                kelly_edge = round((p * avg_w - (1 - p) * avg_l) / avg_w, 3)

            fill_times = sorted([e.get("ts", 0) for e in sells if e.get("ts")])
            avg_fill_hours = 0.0
            if len(fill_times) >= 2:
                gaps = [fill_times[i+1]-fill_times[i]
                        for i in range(len(fill_times)-1)]
                avg_fill_hours = round(sum(gaps) / len(gaps) / 3600, 2)

            # v4: per-regime breakdown
            regime_stats = {}
            for r, rp in self._regime_profits.items():
                if rp:
                    regime_stats[r] = {
                        "count":    len(rp),
                        "win_rate": round(sum(1 for x in rp if x > 0) / len(rp) * 100, 1),
                        "avg_pnl":  round(sum(rp) / len(rp), 4),
                        "kelly":    round(self._kelly_by_regime.get(r, self._kelly_mult), 3),
                    }

            return {
                "version":          "v4",
                "trained":          self._trained,
                "samples":          len(exp),
                "sell_fills":       len(sells),
                "buy_fills":        len(buys),
                "total_profit_ton": round(sum(profits), 4),
                "avg_profit_ton":   round(sum(profits)/len(profits), 4) if profits else 0,
                "best_profit_ton":  round(max(profits), 4) if profits else 0,
                "worst_profit_ton": round(min(profits), 4) if profits else 0,
                "win_rate_pct":     round(win_rate, 1),
                "recent_win_rate":  round(recent_wr, 1),
                "profit_trend":     trend,
                "avg_step_used":    round(avg_step, 2),
                "win_streak":       self._win_streak,
                "consecutive_losses": self._consecutive_losses,
                "risk_level":       self.get_risk_level(),
                "kelly_edge":       kelly_edge,
                "kelly_mult":       round(self._kelly_mult, 3),
                "calibrated_min_step": round(self.calibrated_min_step, 2),
                "avg_fill_hours":   avg_fill_hours,
                "regime_duration":  self._regime_dur,
                "last_regime":      self._last_regime,
                "regime_breakdown": regime_stats,
                "ensemble":         self._ensemble_info(),
                "feat_dim":         FEAT_DIM,
            }

    # ══════════════════════════════════════════════════════════════════════════
    # Внутренние методы
    # ══════════════════════════════════════════════════════════════════════════

    def _compute_fill_density(self) -> float:
        """Число fills за последний час (из experience)."""
        now    = time.time()
        cutoff = now - 3600.0
        count  = sum(1 for e in self._experience if e.get("ts", 0) > cutoff)
        return float(count)

    def _ensemble_info(self) -> dict:
        return {
            "step_models": [m for m, v in [
                ("RF",    self._step_rf),
                ("ET",    self._step_et),
                ("GB",    self._step_gb),
                ("HistGB",self._step_hgb),
                ("Ridge", self._step_ridge),
                ("Meta",  self._step_meta),
                ("SGD",   self._step_sgd),
            ] if v],
            "dca_models": [m for m, v in [
                ("RF",    self._dca_rf),
                ("ET",    self._dca_et),
                ("HistGB",self._dca_hgb),
                ("LR",    self._dca_lr),
                ("SGD",   self._dca_sgd),
            ] if v],
        }

    def _heuristic_step(self, atr_pct: float, regime: str) -> float:
        """Эвристический шаг без ML — быстрый и надёжный."""
        if regime == "PUMP":
            return 8.0
        if regime in ("DISTRIBUTION", "POST_PUMP"):
            return 6.0
        if regime in ("TREND_UP", "VOLATILE"):
            return 8.0 if atr_pct >= 4.0 else 6.0
        if atr_pct >= 5.0:
            return 8.0
        if atr_pct >= 3.0:
            return 6.0
        if atr_pct >= 2.0:
            return 5.0
        return 4.0

    def _make_features(self, atr_pct: float, regime: str,
                       entry: dict = None) -> list:
        """Вектор признаков v4 (ровно FEAT_DIM=20 значений).

        КРИТИЧЕСКИ ВАЖНО: этот метод должен возвращать ровно FEAT_DIM фич
        при любом вызове — иначе sklearn поднимет ошибку размерности.
        Все новые поля берутся из entry (при обучении) или из self (при инференсе),
        с явными defaults для обратной совместимости с v3-записями.
        """
        re = _regime_enc(regime)
        atr = _safe_float(atr_pct)

        # ── Блок 1: базовые 5 (v1, обратная совместимость) ──────────────────
        feat = [
            atr,
            float(re),
            atr ** 2,
            float(abs(re)),
            atr * re,
        ]

        # ── Блок 2: контекстные 8 (v3) ───────────────────────────────────────
        if entry is not None:
            win_streak      = _safe_float(entry.get("win_streak", 0))
            recent_avg      = _safe_float(entry.get("recent_avg_profit", 0))
            profit_momentum = _safe_float(entry.get("profit_momentum", 0))
            hour            = _safe_float(entry.get("hour", 12))
            regime_dur      = _safe_float(entry.get("regime_duration", 0))
        else:
            win_streak      = float(self._win_streak)
            recent_avg      = (sum(self._recent_profits) / len(self._recent_profits)
                               if self._recent_profits else 0.0)
            profit_momentum = 0.0
            if len(self._recent_profits) >= 3:
                rp = list(self._recent_profits)
                profit_momentum = (rp[-1] - rp[0]) / max(abs(rp[0]) + 1e-6, 1e-6)
                profit_momentum = max(-2.0, min(2.0, profit_momentum))
            hour       = float(int(time.strftime("%H", time.gmtime())))
            regime_dur = float(self._regime_dur)

        hour_sin = math.sin(2 * math.pi * hour / 24)
        hour_cos = math.cos(2 * math.pi * hour / 24)

        feat.extend([
            min(win_streak, 10.0),
            max(-5.0, min(5.0, recent_avg)),
            max(-2.0, min(2.0, profit_momentum)),
            hour_sin,
            hour_cos,
            min(regime_dur, 50.0),
            1.0 if re > 0 else 0.0,
            1.0 if re < 0 else 0.0,
        ])  # +8 = 13 total

        # ── Блок 3: новые 7 (v4) ─────────────────────────────────────────────
        if entry is not None:
            consec_loss  = _safe_float(entry.get("consecutive_losses", 0))
            compound     = _safe_float(entry.get("compound_mult", 1.0))
            drawdown     = _safe_float(entry.get("drawdown_pct", 0.0))
            win_rate_5   = _safe_float(entry.get("recent_win_rate_5", 0.5))
            fill_dens    = _safe_float(entry.get("fill_density_1h", 0.0))
            atr_norm     = _safe_float(entry.get("atr_normalized", 1.0))
            reg_conf     = _safe_float(entry.get("regime_confidence", 0.5))
        else:
            consec_loss  = float(self._consecutive_losses)
            compound     = float(self._last_compound_mult)
            drawdown     = 0.0  # runtime only — не храним в self (передаётся извне)
            rp5          = list(self._recent_profits)[-5:]
            win_rate_5   = (sum(1 for x in rp5 if x > 0) / max(len(rp5), 1))
            fill_dens    = self._compute_fill_density()
            mean_atr     = (sum(self._recent_atrs) / len(self._recent_atrs)
                            if self._recent_atrs else atr)
            atr_norm     = atr / max(mean_atr, 0.5)
            reg_conf     = min(self._regime_dur / 20.0, 1.0)

        feat.extend([
            min(consec_loss, 10.0),
            max(1.0, min(2.0, compound)),
            max(0.0, min(50.0, drawdown)),
            max(0.0, min(1.0, win_rate_5)),
            min(fill_dens / 5.0, 4.0),
            max(0.3, min(3.0, atr_norm)),
            max(0.0, min(1.0, reg_conf)),
        ])  # +7 = 20 total

        assert len(feat) == FEAT_DIM, (
            f"[GridAI v4] FEAT_DIM mismatch: expected {FEAT_DIM}, got {len(feat)}")
        return feat

    def _predict_step_ensemble(self, feat: list) -> list:
        """Предсказание шага всем ансамблем + мета-стекинг."""
        base_preds = []
        models = [self._step_rf, self._step_et, self._step_gb,
                  self._step_hgb, self._step_ridge]

        for m in models:
            if m is not None:
                try:
                    base_preds.append(float(m.predict([feat])[0]))
                except Exception:
                    pass

        if not base_preds:
            return []

        # Мета-стекинг: Ridge над предсказаниями базовых моделей
        if self._step_meta is not None and len(base_preds) >= 3:
            try:
                # Дополняем до полного вектора (если не все модели обучены)
                meta_input = base_preds[:5]
                while len(meta_input) < 5:
                    meta_input.append(sum(base_preds) / len(base_preds))
                meta_pred = float(self._step_meta.predict([meta_input])[0])
                # Мета-предсказание имеет вес 0.4 от ансамбля
                avg_base  = sum(base_preds) / len(base_preds)
                return [0.6 * avg_base + 0.4 * meta_pred]
            except Exception:
                pass

        # SGD инкрементальный — дополнительный голос
        if self._step_sgd is not None:
            try:
                sgd_pred = float(self._step_sgd.predict([feat])[0])
                base_preds.append(sgd_pred)
            except Exception:
                pass

        return base_preds

    def _predict_dca_ensemble(self, feat: list) -> list:
        """Предсказание DCA вероятности ансамблем классификаторов."""
        probs = []
        models = [self._dca_rf, self._dca_et, self._dca_hgb, self._dca_lr]
        for m in models:
            if m is not None:
                try:
                    probs.append(float(m.predict_proba([feat])[0][1]))
                except Exception:
                    pass
        return probs

    def _compute_sample_weights(self, entries: list, now: float) -> list:
        """Временные веса: свежие примеры важнее старых."""
        weights = []
        for e in entries:
            ts = _safe_float(e.get("ts", now - 86400))
            weights.append(max(0.01, _exp_decay_weight(ts, now)))
        return weights

    def _calibrate_min_step(self, sells: list):
        """Авто-калибровка MIN_STEP по реальным данным."""
        if len(sells) < 3:
            return
        profitable = [e for e in sells if e.get("profit_ton", 0) > 0]
        if not profitable:
            return
        min_profitable_step = min(
            _safe_float(e.get("step_used", 4.0)) for e in profitable)
        calibrated = round(max(3.5, min_profitable_step - 0.25) * 2) / 2
        if abs(calibrated - self.calibrated_min_step) >= 0.5:
            log.info("[GridAI v4] ⚙️ MIN_STEP: %.2f%% → %.2f%% (%d прибыльных сделок)",
                     self.calibrated_min_step, calibrated, len(profitable))
        self.calibrated_min_step = calibrated

    def _compute_kelly_mult(self, profits: list):
        """Kelly criterion → глобальный множитель шага (Half-Kelly)."""
        if len(profits) < 5:
            self._kelly_mult = 1.0
            return

        wins   = [p for p in profits if p > 0]
        losses = [abs(p) for p in profits if p <= 0]

        if not wins:
            self._kelly_mult = 0.7
            return
        if not losses:
            self._kelly_mult = 1.1
            return

        p     = len(wins) / len(profits)
        avg_w = sum(wins) / len(wins)
        avg_l = sum(losses) / len(losses)

        kelly = (p * avg_w - (1 - p) * avg_l) / avg_w
        mult  = 1.0 + max(-0.3, min(0.3, kelly * 0.5))
        self._kelly_mult = round(mult, 3)

    def _compute_kelly_by_regime(self):
        """Per-regime Kelly (v4 новое)."""
        for regime, rp in self._regime_profits.items():
            if len(rp) < 5:
                self._kelly_by_regime[regime] = self._kelly_mult
                continue
            wins   = [p for p in rp if p > 0]
            losses = [abs(p) for p in rp if p <= 0]
            if not wins or not losses:
                self._kelly_by_regime[regime] = self._kelly_mult
                continue
            p     = len(wins) / len(rp)
            avg_w = sum(wins)   / len(wins)
            avg_l = sum(losses) / len(losses)
            kelly = (p * avg_w - (1 - p) * avg_l) / avg_w
            mult  = 1.0 + max(-0.3, min(0.3, kelly * 0.5))
            self._kelly_by_regime[regime] = round(mult, 3)

    def _safe_atr(self, e: dict) -> float:
        try:
            return float(e.get("atr_pct") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _incremental_update(self, entry: dict):
        """Инкрементальное обновление SGD-моделей после каждой сделки (v4)."""
        try:
            from sklearn.linear_model import SGDRegressor, SGDClassifier

            feat = [self._make_features(self._safe_atr(entry),
                                         entry.get("regime", "SIDEWAYS"), entry)]

            # SGD-регрессор шага
            if entry.get("side") == "sell" and entry.get("step_used") is not None:
                y_step = [_safe_float(entry.get("step_used", 4.0))]
                if self._step_sgd is None:
                    self._step_sgd = SGDRegressor(
                        loss="huber", penalty="l2", alpha=0.01,
                        learning_rate="invscaling", eta0=0.05,
                        power_t=0.5, max_iter=1, tol=None, random_state=42)
                    self._step_sgd.partial_fit(feat, y_step)
                else:
                    self._step_sgd.partial_fit(feat, y_step)

            # SGD-классификатор DCA
            y_cls = [int(entry.get("is_profitable", 0))]
            if self._dca_sgd is None:
                self._dca_sgd = SGDClassifier(
                    loss="log_loss", penalty="l2", alpha=0.01,
                    learning_rate="invscaling", eta0=0.05,
                    power_t=0.5, max_iter=1, tol=None, random_state=42)
                # SGDClassifier нужно видеть оба класса при первом вызове
                self._dca_sgd.partial_fit(feat, y_cls, classes=[0, 1])
            else:
                self._dca_sgd.partial_fit(feat, y_cls)

        except ImportError:
            pass
        except Exception as e:
            log.debug("[GridAI v4] incremental_update error: %s", e)

    def _train(self):
        """Полное переобучение ансамбля моделей."""
        try:
            from sklearn.ensemble import (
                RandomForestRegressor, ExtraTreesRegressor,
                GradientBoostingRegressor,
                RandomForestClassifier, ExtraTreesClassifier)
            from sklearn.linear_model import Ridge, LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline

            # HistGradientBoosting — пробуем, но не падаем если нет
            try:
                from sklearn.ensemble import (
                    HistGradientBoostingRegressor,
                    HistGradientBoostingClassifier)
                _has_hgb = True
            except ImportError:
                _has_hgb = False

            now   = time.time()
            sells = [e for e in self._experience if e.get("side") == "sell"]
            all_e = self._experience

            # ── Калибровка и Kelly ──────────────────────────────────────────
            profits = [e.get("profit_ton", 0) for e in sells]
            self._calibrate_min_step(sells)
            self._compute_kelly_mult(profits)
            self._compute_kelly_by_regime()

            # ── Step-ансамбль: 5 базовых моделей ───────────────────────────
            if len(sells) >= MIN_SAMPLES:
                X_s = [self._make_features(self._safe_atr(e),
                                            e.get("regime", "SIDEWAYS"), e)
                       for e in sells]
                y_s = [_safe_float(e.get("step_used", 4.0)) for e in sells]
                w_s = self._compute_sample_weights(sells, now)

                def _fit_step(model, use_weights=True):
                    if use_weights:
                        model.fit(X_s, y_s, m__sample_weight=w_s)
                    else:
                        model.fit(X_s, y_s)
                    return model

                self._step_rf = _fit_step(Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  RandomForestRegressor(
                        n_estimators=60, max_depth=6, min_samples_leaf=2,
                        random_state=42, n_jobs=1)),
                ]))

                self._step_et = _fit_step(Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  ExtraTreesRegressor(
                        n_estimators=60, max_depth=6, min_samples_leaf=2,
                        random_state=42, n_jobs=1)),
                ]))

                self._step_gb = _fit_step(Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  GradientBoostingRegressor(
                        n_estimators=80, max_depth=3,
                        learning_rate=0.08, random_state=42)),
                ]))

                if _has_hgb:
                    try:
                        hgb_step = HistGradientBoostingRegressor(
                            max_iter=80, max_depth=4,
                            learning_rate=0.08, random_state=42)
                        hgb_step.fit(X_s, y_s, sample_weight=w_s)
                        self._step_hgb = hgb_step
                    except Exception as he:
                        log.debug("[GridAI v4] HistGB step skip: %s", he)

                self._step_ridge = _fit_step(Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  Ridge(alpha=1.0)),
                ]))

                # Мета-стекинг (Ridge над предсказаниями базовых моделей)
                # Используем in-sample предсказания — небольшой overfit допустим
                # благодаря временным весам
                if len(sells) >= 15:
                    try:
                        meta_X = []
                        for x in X_s:
                            preds = []
                            for m in [self._step_rf, self._step_et,
                                      self._step_gb, self._step_hgb,
                                      self._step_ridge]:
                                if m is not None:
                                    preds.append(float(m.predict([x])[0]))
                            # Дополняем до 5 если некоторые модели отсутствуют
                            while len(preds) < 5:
                                preds.append(sum(preds) / max(len(preds), 1))
                            meta_X.append(preds[:5])

                        meta = Pipeline([
                            ("sc", StandardScaler()),
                            ("m",  Ridge(alpha=0.5)),
                        ])
                        meta.fit(meta_X, y_s, m__sample_weight=w_s)
                        self._step_meta = meta
                        log.info("[GridAI v4] 🔗 Мета-стекер обучен на %d продажах",
                                 len(sells))
                    except Exception as me:
                        log.debug("[GridAI v4] meta-stacker error: %s", me)

                log.info("[GridAI v4] 📊 Step-ансамбль (RF+ET+GB+HistGB+Ridge+Meta) "
                         "на %d продажах (w_min=%.2f w_max=%.2f)",
                         len(sells), min(w_s), max(w_s))
                gc.collect()

            # ── DCA-ансамбль: 4 классификатора ─────────────────────────────
            if len(all_e) >= MIN_SAMPLES:
                y_p   = [int(e.get("is_profitable", 0)) for e in all_e]
                n_pos = sum(y_p)
                n_neg = len(y_p) - n_pos

                if n_pos >= 2 and n_neg >= 1:
                    X_p = [self._make_features(self._safe_atr(e),
                                               e.get("regime", "SIDEWAYS"), e)
                           for e in all_e]
                    w_p = self._compute_sample_weights(all_e, now)

                    def _fit_cls(model, use_weights=True):
                        if use_weights:
                            model.fit(X_p, y_p, m__sample_weight=w_p)
                        else:
                            model.fit(X_p, y_p)
                        return model

                    self._dca_rf = _fit_cls(Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  RandomForestClassifier(
                            n_estimators=60, max_depth=5,
                            class_weight="balanced", random_state=42, n_jobs=1)),
                    ]))

                    self._dca_et = _fit_cls(Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  ExtraTreesClassifier(
                            n_estimators=60, max_depth=5,
                            class_weight="balanced", random_state=42, n_jobs=1)),
                    ]))

                    if _has_hgb:
                        try:
                            hgb_cls = HistGradientBoostingClassifier(
                                max_iter=80, max_depth=4,
                                learning_rate=0.08, random_state=42,
                                class_weight="balanced")
                            hgb_cls.fit(X_p, y_p, sample_weight=w_p)
                            self._dca_hgb = hgb_cls
                        except Exception as he:
                            log.debug("[GridAI v4] HistGB dca skip: %s", he)

                    self._dca_lr = _fit_cls(Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  LogisticRegression(
                            C=1.0, max_iter=500,
                            class_weight="balanced", random_state=42)),
                    ]))

                    log.info("[GridAI v4] 📊 DCA-ансамбль (RF+ET+HistGB+LR) "
                             "на %d примерах (pos=%d neg=%d)",
                             len(all_e), n_pos, n_neg)
                    gc.collect()
                else:
                    log.info("[GridAI v4] DCA пропущен: один класс (pos=%d neg=%d)",
                             n_pos, n_neg)

            self._trained      = True
            self._last_train_n = len(self._experience)
            log.info("[GridAI v4] ✅ Обучение завершено: %d примеров (%d sells) "
                     "| min_step=%.2f%% kelly=%.3f risk=%d",
                     len(self._experience), len(sells),
                     self.calibrated_min_step, self._kelly_mult,
                     self.get_risk_level())

        except ImportError as e:
            log.warning("[GridAI v4] sklearn не найден: %s — heuristic-режим", e)
        except Exception as e:
            log.error("[GridAI v4] Ошибка обучения: %s", e, exc_info=True)

    def _save_experience(self):
        try:
            os.makedirs(os.path.dirname(EXPERIENCE_FILE) or ".", exist_ok=True)
            with open(EXPERIENCE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._experience, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("[GridAI v4] Сохранение опыта: %s", e)

    def _load_experience(self):
        try:
            if os.path.exists(EXPERIENCE_FILE):
                with open(EXPERIENCE_FILE, encoding="utf-8") as f:
                    self._experience = json.load(f)
                log.info("[GridAI v4] Загружено %d примеров из %s",
                         len(self._experience), EXPERIENCE_FILE)
                self._rebuild_rolling_state()
        except Exception as e:
            log.warning("[GridAI v4] Загрузка опыта: %s", e)
            self._experience = []

    def _rebuild_rolling_state(self):
        """Восстанавливаем все трекеры из загруженной истории."""
        sells = sorted(
            [e for e in self._experience if e.get("side") == "sell"],
            key=lambda x: x.get("ts", 0))

        self._win_streak        = 0
        self._consecutive_losses = 0

        for e in self._experience:
            # ATR history
            atr = _safe_float(e.get("atr_pct"))
            if atr > 0:
                self._recent_atrs.append(atr)
            # compound_mult (последнее значение)
            cm = _safe_float(e.get("compound_mult", 1.0))
            if cm > 1.0:
                self._last_compound_mult = cm

        for e in sells:
            profit = _safe_float(e.get("profit_ton", 0))
            self._recent_profits.append(profit)
            regime = e.get("regime", "UNKNOWN")
            if regime not in self._regime_profits:
                self._regime_profits[regime] = []
            self._regime_profits[regime].append(profit)
            if profit > 0:
                self._win_streak        += 1
                self._consecutive_losses = 0
            else:
                self._win_streak         = 0
                self._consecutive_losses += 1

        # Подрезаем per-regime до 50
        for r in list(self._regime_profits):
            if len(self._regime_profits[r]) > 50:
                self._regime_profits[r] = self._regime_profits[r][-50:]


# ── Синглтон ──────────────────────────────────────────────────────────────────

_instance:  Optional[GridAI] = None
_init_lock: threading.Lock   = threading.Lock()


def get_grid_ai() -> GridAI:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = GridAI()
    return _instance
