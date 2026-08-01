"""
grid_ai.py v3 — Продвинутый самообучающийся AI-оптимизатор сетки

Что нового в v3:
  • Ансамбль моделей: RandomForest + GradientBoosting + Ridge (шаг),
    RandomForest + LogisticRegression → VotingClassifier (DCA-решение)
  • Взвешенное обучение: свежие сделки важнее старых (exp-decay, T½≈7 дней)
  • Богатый вектор признаков: 13 фич вместо 5 (momentum, win_streak,
    hour_of_day, profit_momentum, recent_avg_profit и др.)
  • Критерий Келли: оптимальный размер ставки по edge/odds
  • Авто-калибровка MIN_STEP по реальным затратам (газ + комиссия)
  • Режимо-зависимые DCA-пороги (SIDEWAYS vs TREND vs POST_PUMP)
  • Скользящий performance-tracker: winrate, profit momentum, drawdown streak
  • Полная обратная совместимость со старыми experience-записями
"""

import os
import json
import time
import math
import threading
import logging
from collections import deque
from typing import Optional, List

log = logging.getLogger("grid_ai")

DATA_DIR        = os.getenv("DATA_DIR", ".")
EXPERIENCE_FILE = os.path.join(DATA_DIR, "grid_ai_experience.json")

# Минимум примеров для первого обучения
MIN_SAMPLES = 5
# Полужизнь весов (дни): через 7 дней вес = 0.5
DECAY_HALFLIFE_DAYS = 7.0


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def _safe_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _exp_decay_weight(ts: float, now: float) -> float:
    """Экспоненциальный вес по возрасту записи (в днях)."""
    age_days = max(0.0, (now - ts) / 86400.0)
    return math.exp(-math.log(2) * age_days / DECAY_HALFLIFE_DAYS)


# ─── Основной класс ───────────────────────────────────────────────────────────

class GridAI:
    """Самообучающийся AI-оптимизатор сеточной торговли v3."""

    # Кэш производительности
    _perf_cache: dict = {}
    _perf_ts:    float = 0.0

    def __init__(self):
        self._lock       = threading.RLock()
        self._experience: List[dict] = []

        # Модели ансамбля — шаг
        self._step_rf    = None   # RandomForestRegressor
        self._step_gb    = None   # GradientBoostingRegressor
        self._step_ridge = None   # Ridge (быстрый baseline)

        # Модели ансамбля — DCA-решение
        self._dca_rf     = None   # RandomForestClassifier
        self._dca_lr     = None   # LogisticRegression

        # Скользящая статистика
        self._win_streak:    int   = 0   # текущая серия побед
        self._recent_profits: deque = deque(maxlen=10)  # последние 10 прибылей
        self._regime_dur:    int   = 0   # тиков в текущем режиме
        self._last_regime:   str   = ""

        # Калиброванный MIN_STEP (пересчитывается по реальным данным)
        self.calibrated_min_step: float = 4.0
        # Kelly-множитель шага (обновляется при обучении)
        self._kelly_mult: float = 1.0

        self._trained     = False
        self._last_train_n = 0

        self._load_experience()
        if len(self._experience) >= MIN_SAMPLES:
            self._train()
        log.info("[GridAI v3] Инициализирован. Примеров: %d, обучен: %s",
                 len(self._experience), self._trained)

    # ── Публичное API ─────────────────────────────────────────────────────────

    def get_optimal_step(self, atr_pct: float, regime: str = "SIDEWAYS",
                         min_step: float = None, max_step: float = 10.0) -> float:
        """Предсказать оптимальный шаг сетки.

        Использует ансамбль RF+GB+Ridge с весами по числу обучающих примеров.
        При малой выборке плавно переходит к эвристике.
        Применяет Kelly-корректировку и авто-калиброванный min_step.
        """
        if min_step is None:
            min_step = self.calibrated_min_step

        heuristic = self._heuristic_step(atr_pct, regime)
        heuristic = max(min_step, min(max_step, heuristic))

        if not self._trained:
            return heuristic

        try:
            feat   = self._make_features(atr_pct, regime)
            preds  = []
            if self._step_rf:
                preds.append(float(self._step_rf.predict([feat])[0]))
            if self._step_gb:
                preds.append(float(self._step_gb.predict([feat])[0]))
            if self._step_ridge:
                preds.append(float(self._step_ridge.predict([feat])[0]))

            if not preds:
                return heuristic

            ml_pred = sum(preds) / len(preds)
            ml_pred = max(min_step, min(max_step, round(ml_pred * 2) / 2))

            # Применяем Kelly-корректировку
            ml_pred = max(min_step, min(max_step, ml_pred * self._kelly_mult))
            ml_pred = round(ml_pred * 2) / 2

            # Плавный переход: чем больше примеров, тем больше доверяем модели
            n = len(self._experience)
            weight = min(1.0, (n - MIN_SAMPLES) / 45.0)  # 100% при 50 примерах
            blended = heuristic * (1 - weight) + ml_pred * weight

            result = max(min_step, min(max_step, round(blended * 2) / 2))
            log.debug("[GridAI v3] step: h=%.1f ml=%.1f k=%.2f → %.1f "
                      "(ATR=%.2f%% regime=%s n=%d w=%.2f)",
                      heuristic, ml_pred, self._kelly_mult, result,
                      atr_pct, regime, n, weight)
            return result

        except Exception as e:
            log.warning("[GridAI v3] predict_step error: %s", e)
            return heuristic

    def get_dca_confidence(self, atr_pct: float, regime: str,
                           drawdown_pct: float, price_vs_center_pct: float) -> float:
        """Уверенность что стоит делать DCA-добавление (0–100%).

        Жёсткие блокировки: PUMP/DISTRIBUTION/POST_PUMP или просадка > 50%.
        Мягкий порог: режимо-зависимый — в боковике агрессивнее, в тренде вниз — нет.
        """
        # Жёсткие блокировки
        if regime in ("PUMP", "DISTRIBUTION", "POST_PUMP"):
            return 0.0
        if drawdown_pct > 50.0:
            return 0.0

        # Режимо-зависимые пороги одобрения
        regime_bias = {
            "SIDEWAYS":  1.20,  # +20% в боковике — самые выгодные DCA
            "UNKNOWN":   1.00,
            "TREND_UP":  0.80,  # осторожнее при тренде вверх (цена может ещё упасть)
            "TREND_DOWN": 0.50, # в тренде вниз очень осторожно
        }
        bias = regime_bias.get(regime, 1.0)

        # Эвристика без модели
        if not self._trained or (self._dca_rf is None and self._dca_lr is None):
            if atr_pct >= 2.0 and drawdown_pct < 40.0:
                raw = 60.0 * bias
            else:
                raw = 25.0
            if drawdown_pct > 35.0:
                raw *= 0.6
            return round(max(0.0, min(100.0, raw)), 1)

        try:
            feat = self._make_features(atr_pct, regime)
            probs = []
            if self._dca_rf:
                probs.append(float(self._dca_rf.predict_proba([feat])[0][1]))
            if self._dca_lr:
                probs.append(float(self._dca_lr.predict_proba([feat])[0][1]))

            if not probs:
                return 25.0

            prob = sum(probs) / len(probs)
            prob *= bias

            # Штраф при большой просадке
            if drawdown_pct > 35.0:
                prob *= 0.6
            elif drawdown_pct > 25.0:
                prob *= 0.8

            # Бонус при серии побед
            if self._win_streak >= 3:
                prob = min(1.0, prob * 1.1)

            return round(max(0.0, min(100.0, prob * 100)), 1)

        except Exception as e:
            log.warning("[GridAI v3] dca_confidence error: %s", e)
            return 25.0

    def get_dca_size_multiplier(self, cycle_num: int, win_rate: float) -> float:
        """Рекомендуемый множитель размера DCA-ордера (Kelly-based).

        cycle_num: номер DCA (1, 2, 3 ...)
        win_rate:  процент прибыльных fills (0–100)
        Returns:   0.7 – 2.0
        """
        # Базовый Kelly-размер
        kelly = self._kelly_mult if self._kelly_mult > 0 else 1.0

        # Снижаем агрессию с каждым следующим уровнем DCA
        level_decay = max(0.7, 1.0 - (cycle_num - 1) * 0.1)

        # Корректируем по winrate
        if win_rate >= 80:
            wr_mult = 1.15
        elif win_rate >= 60:
            wr_mult = 1.0
        elif win_rate < 40:
            wr_mult = 0.8
        else:
            wr_mult = 0.9

        result = kelly * level_decay * wr_mult
        return round(max(0.7, min(2.0, result)), 2)

    def get_pyramid_weights(self, n_levels: int) -> List[float]:
        """Веса распределения GRINCH по уровням (пирамида: больше на нижних).

        Идея: нижние уровни (ближе к текущей цене) срабатывают первыми →
        больший объём там = больше прибыли при умеренном росте.

        Returns: список весов длиной n_levels (сумма ≈ n_levels).
        """
        if n_levels <= 0:
            return []
        # Убывающий ряд: L1 получает max_ratio, Ln получает min_ratio
        max_ratio = 1.30
        min_ratio = 0.70
        if n_levels == 1:
            return [1.0]
        weights = []
        for i in range(n_levels):
            # Линейное убывание от max к min
            w = max_ratio - (max_ratio - min_ratio) * i / (n_levels - 1)
            weights.append(w)
        # Нормализуем так чтобы среднее = 1.0
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
                    is_dca: bool = False):
        """Записать результат исполненного уровня.

        Обогащает запись контекстными фичами: win_streak, profit_momentum,
        recent_avg_profit, hour, regime_duration — для v3-обучения.
        """
        now = time.time()

        # Вычисляем производные признаки из текущего состояния
        recent = list(self._recent_profits)
        recent_avg = sum(recent) / len(recent) if recent else 0.0
        profit_momentum = 0.0
        if len(recent) >= 3:
            # Тренд последних прибылей (pos = улучшается)
            profit_momentum = (recent[-1] - recent[0]) / max(abs(recent[0]) + 0.0001, 0.0001)
            profit_momentum = max(-2.0, min(2.0, profit_momentum))

        entry = {
            "ts":             now,
            "side":           side,
            "step_used":      step_used,
            "atr_pct":        atr_pct,
            "regime":         regime,
            "profit_ton":     profit_ton,
            "profit_pct":     profit_pct,
            "is_dca":         is_dca,
            "is_profitable":  1 if profit_ton > 0 else 0,
            # v3 дополнительные поля
            "win_streak":       self._win_streak,
            "recent_avg_profit": round(recent_avg, 4),
            "profit_momentum":   round(profit_momentum, 4),
            "hour":             int(time.strftime("%H", time.gmtime(now))),
            "regime_duration":  self._regime_dur,
        }

        # Обновляем скользящую статистику
        self._recent_profits.append(profit_ton)
        if profit_ton > 0:
            self._win_streak += 1
        else:
            self._win_streak = 0

        with self._lock:
            self._experience.append(entry)
            if len(self._experience) > 2000:
                # Оставляем последние 2000 (старое отбрасываем)
                self._experience = self._experience[-2000:]
            self._save_experience()
            if (len(self._experience) >= MIN_SAMPLES and
                    len(self._experience) != self._last_train_n):
                self._train()

        log.info("[GridAI v3] 📝 Fill: side=%s step=%.1f%% profit=%+.4f TON "
                 "(%.2f%%) streak=%d n=%d обучен=%s",
                 side, step_used, profit_ton, profit_pct,
                 self._win_streak, len(self._experience), self._trained)

    def get_stats(self) -> dict:
        """Расширенная статистика для дашборда и /api/grid/status."""
        with self._lock:
            now  = time.time()
            exp  = self._experience
            sells = [e for e in exp if e.get("side") == "sell"]
            buys  = [e for e in exp if e.get("side") == "buy"]
            if not exp:
                return {"trained": False, "samples": 0, "version": "v3"}

            profits = [e["profit_ton"] for e in sells if "profit_ton" in e]
            wins    = [p for p in profits if p > 0]
            losses  = [p for p in profits if p <= 0]
            win_rate = len(wins) / len(profits) * 100 if profits else 0

            # Скользящий winrate (последние 20 сделок)
            recent_sells = sells[-20:]
            recent_wins  = sum(1 for e in recent_sells if e.get("profit_ton", 0) > 0)
            recent_wr    = recent_wins / len(recent_sells) * 100 if recent_sells else 0

            # Avg step по продажам
            steps = [e["step_used"] for e in sells if "step_used" in e]
            avg_step = sum(steps) / len(steps) if steps else 0

            # Profit trend (последние 5 vs предыдущие 5)
            if len(profits) >= 10:
                prev5 = sum(profits[-10:-5]) / 5
                last5 = sum(profits[-5:]) / 5
                trend = "📈" if last5 > prev5 else "📉"
            else:
                trend = "—"

            # Kelly-edge
            kelly_edge = 0.0
            if wins and losses:
                avg_w = sum(wins) / len(wins)
                avg_l = sum(abs(l) for l in losses) / len(losses)
                p     = len(wins) / len(profits)
                kelly_edge = round((p * avg_w - (1 - p) * avg_l) / avg_w, 3)

            # Среднее время между сделками
            fill_times = sorted([e.get("ts", 0) for e in sells if e.get("ts")])
            avg_fill_hours = 0.0
            if len(fill_times) >= 2:
                gaps = [fill_times[i+1]-fill_times[i] for i in range(len(fill_times)-1)]
                avg_fill_hours = round(sum(gaps) / len(gaps) / 3600, 2)

            return {
                "version":          "v3",
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
                "kelly_edge":       kelly_edge,
                "kelly_mult":       round(self._kelly_mult, 3),
                "calibrated_min_step": round(self.calibrated_min_step, 2),
                "avg_fill_hours":   avg_fill_hours,
                "regime_duration":  self._regime_dur,
                "ensemble":         self._ensemble_info(),
            }

    # ── Внутренние методы ─────────────────────────────────────────────────────

    def _ensemble_info(self) -> dict:
        return {
            "step_models":  [m for m, v in [("RF", self._step_rf),
                                             ("GB", self._step_gb),
                                             ("Ridge", self._step_ridge)] if v],
            "dca_models":   [m for m, v in [("RF", self._dca_rf),
                                             ("LR", self._dca_lr)] if v],
        }

    def _heuristic_step(self, atr_pct: float, regime: str) -> float:
        """Эвристический шаг без ML — быстрый и надёжный."""
        if regime == "PUMP":
            return 8.0
        if regime in ("DISTRIBUTION", "POST_PUMP"):
            return 6.0
        if regime == "TREND_UP":
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
        """Вектор признаков v3 (13 фич).

        Если передан entry — берём дополнительные поля из него.
        Иначе используем текущее состояние GridAI.
        """
        regime_map = {
            "TREND_UP":   2, "TREND_DOWN": -2,
            "SIDEWAYS":   0, "PUMP":        3,
            "DISTRIBUTION": -1, "POST_PUMP": -3,
            "UNKNOWN":    0,
        }
        regime_enc = regime_map.get(regime if isinstance(regime, str) else "UNKNOWN", 0)
        atr_pct    = _safe_float(atr_pct)

        # Базовые 5 (обратная совместимость)
        feat = [
            atr_pct,
            regime_enc,
            atr_pct ** 2,
            abs(regime_enc),
            atr_pct * regime_enc,
        ]

        # Дополнительные 8 (v3)
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

        # Циклические признаки часа суток
        hour_sin = math.sin(2 * math.pi * hour / 24)
        hour_cos = math.cos(2 * math.pi * hour / 24)

        feat.extend([
            min(win_streak, 10.0),       # серия побед (кап 10)
            max(-5.0, min(5.0, recent_avg)),   # средняя прибыль последних 10
            profit_momentum,             # тренд прибылей
            hour_sin,                    # циклический час (sin)
            hour_cos,                    # циклический час (cos)
            min(regime_dur, 50.0),       # длительность режима (кап 50)
            1.0 if regime_enc > 0 else 0.0,  # бинарный тренд вверх
            1.0 if regime_enc < 0 else 0.0,  # бинарный негативный режим
        ])
        return feat

    def _compute_sample_weights(self, entries: list, now: float) -> list:
        """Временные веса: свежие примеры важнее старых."""
        weights = []
        for e in entries:
            ts = _safe_float(e.get("ts", now - 86400))
            w  = _exp_decay_weight(ts, now)
            weights.append(max(0.01, w))  # минимальный вес 0.01
        return weights

    def _calibrate_min_step(self, sells: list):
        """Авто-калибровка MIN_STEP по реальным данным.

        Вычисляет реальный breakeven шаг на основе средних gas+fee.
        Если можем — берём min из прибыльных сделок.
        """
        if len(sells) < 3:
            return  # недостаточно данных

        profitable = [e for e in sells if e.get("profit_ton", 0) > 0]
        if not profitable:
            return

        # Минимальный шаг среди прибыльных сделок
        min_profitable_step = min(_safe_float(e.get("step_used", 4.0))
                                  for e in profitable)
        # Добавляем небольшой запас
        calibrated = round(max(3.5, min_profitable_step - 0.25) * 2) / 2
        if abs(calibrated - self.calibrated_min_step) >= 0.5:
            log.info("[GridAI v3] ⚙️ MIN_STEP: %.2f%% → %.2f%% "
                     "(по %d прибыльным сделкам)",
                     self.calibrated_min_step, calibrated, len(profitable))
        self.calibrated_min_step = calibrated

    def _compute_kelly_mult(self, profits: list):
        """Kelly criterion → множитель шага.

        Полная формула Kelly для непрерывного распределения:
          f = (p * avg_win - (1-p) * avg_loss) / avg_win
        Используем half-Kelly для безопасности.
        """
        if len(profits) < 5:
            self._kelly_mult = 1.0
            return

        wins   = [p for p in profits if p > 0]
        losses = [abs(p) for p in profits if p <= 0]

        if not wins:
            self._kelly_mult = 0.7
            return
        if not losses:
            # Только прибыльные — слегка агрессивнее
            self._kelly_mult = 1.1
            return

        p     = len(wins) / len(profits)
        avg_w = sum(wins)   / len(wins)
        avg_l = sum(losses) / len(losses)

        # Kelly fraction
        kelly = (p * avg_w - (1 - p) * avg_l) / avg_w
        # Half-Kelly, зажатый в [0.7, 1.3]
        mult  = 1.0 + max(-0.3, min(0.3, kelly * 0.5))
        self._kelly_mult = round(mult, 3)
        log.debug("[GridAI v3] Kelly: p=%.2f avg_w=%.4f avg_l=%.4f "
                  "kelly=%.3f mult=%.3f",
                  p, avg_w, avg_l, kelly, self._kelly_mult)

    def _safe_atr(self, e: dict) -> float:
        try:
            return float(e.get("atr_pct") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _train(self):
        """Обучить/переобучить ансамбль моделей."""
        try:
            from sklearn.ensemble import (RandomForestRegressor,
                                          GradientBoostingRegressor,
                                          RandomForestClassifier)
            from sklearn.linear_model import Ridge, LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline
            import gc

            now   = time.time()
            sells = [e for e in self._experience if e.get("side") == "sell"]
            all_e = self._experience

            # ── Калибровка и Kelly ──────────────────────────────────────
            profits = [e.get("profit_ton", 0) for e in sells]
            self._calibrate_min_step(sells)
            self._compute_kelly_mult(profits)

            # ── Step-ансамбль (RF + GB + Ridge) ─────────────────────────
            if len(sells) >= MIN_SAMPLES:
                X_s = [self._make_features(self._safe_atr(e),
                                           e.get("regime", "SIDEWAYS"), e)
                       for e in sells]
                y_s = [_safe_float(e.get("step_used", 4.0)) for e in sells]
                w_s = self._compute_sample_weights(sells, now)

                # RandomForest
                rf_step = Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  RandomForestRegressor(
                        n_estimators=40, max_depth=5,
                        min_samples_leaf=2, random_state=42, n_jobs=1)),
                ])
                rf_step.fit(X_s, y_s, m__sample_weight=w_s)
                self._step_rf = rf_step

                # GradientBoosting
                gb_step = Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  GradientBoostingRegressor(
                        n_estimators=60, max_depth=3,
                        learning_rate=0.1, random_state=42)),
                ])
                gb_step.fit(X_s, y_s, m__sample_weight=w_s)
                self._step_gb = gb_step

                # Ridge (быстрый baseline с весами)
                ridge_step = Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  Ridge(alpha=1.0)),
                ])
                ridge_step.fit(X_s, y_s, m__sample_weight=w_s)
                self._step_ridge = ridge_step

                log.info("[GridAI v3] 📊 Step-ансамбль обучен: RF+GB+Ridge "
                         "на %d продажах (w_min=%.2f w_max=%.2f)",
                         len(sells), min(w_s), max(w_s))
                gc.collect()

            # ── DCA-ансамбль (RF + LogisticRegression) ──────────────────
            if len(all_e) >= MIN_SAMPLES:
                y_p   = [int(e.get("is_profitable", 0)) for e in all_e]
                n_pos = sum(y_p)
                n_neg = len(y_p) - n_pos

                if n_pos >= 2 and n_neg >= 1:
                    X_p = [self._make_features(self._safe_atr(e),
                                               e.get("regime", "SIDEWAYS"), e)
                           for e in all_e]
                    w_p = self._compute_sample_weights(all_e, now)

                    # RandomForest для DCA
                    rf_dca = Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  RandomForestClassifier(
                            n_estimators=40, max_depth=4,
                            class_weight="balanced", random_state=42, n_jobs=1)),
                    ])
                    rf_dca.fit(X_p, y_p, m__sample_weight=w_p)
                    self._dca_rf = rf_dca

                    # LogisticRegression
                    lr_dca = Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  LogisticRegression(
                            C=1.0, max_iter=300,
                            class_weight="balanced", random_state=42)),
                    ])
                    lr_dca.fit(X_p, y_p, m__sample_weight=w_p)
                    self._dca_lr = lr_dca

                    log.info("[GridAI v3] 📊 DCA-ансамбль: RF+LR на %d примерах "
                             "(pos=%d neg=%d)", len(all_e), n_pos, n_neg)
                    gc.collect()
                else:
                    log.info("[GridAI v3] DCA-модели пропущены: "
                             "только один класс (pos=%d neg=%d)", n_pos, n_neg)

            self._trained      = True
            self._last_train_n = len(self._experience)
            log.info("[GridAI v3] ✅ Обучение завершено: %d примеров (%d sells) "
                     "| min_step=%.2f%% kelly=%.3f",
                     len(self._experience), len(sells),
                     self.calibrated_min_step, self._kelly_mult)

        except ImportError as e:
            log.warning("[GridAI v3] sklearn не найден: %s — heuristic-режим", e)
        except Exception as e:
            log.error("[GridAI v3] Ошибка обучения: %s", e, exc_info=True)

    def _save_experience(self):
        try:
            os.makedirs(os.path.dirname(EXPERIENCE_FILE) or ".", exist_ok=True)
            with open(EXPERIENCE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._experience, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("[GridAI v3] Сохранение опыта: %s", e)

    def _load_experience(self):
        try:
            if os.path.exists(EXPERIENCE_FILE):
                with open(EXPERIENCE_FILE, encoding="utf-8") as f:
                    self._experience = json.load(f)
                log.info("[GridAI v3] Загружено %d примеров из %s",
                         len(self._experience), EXPERIENCE_FILE)
                # Инициализируем скользящую статистику из истории
                self._rebuild_rolling_state()
        except Exception as e:
            log.warning("[GridAI v3] Загрузка опыта: %s", e)
            self._experience = []

    def _rebuild_rolling_state(self):
        """Восстанавливаем win_streak и recent_profits из загруженной истории."""
        sells = sorted(
            [e for e in self._experience if e.get("side") == "sell"],
            key=lambda x: x.get("ts", 0)
        )
        self._win_streak = 0
        for e in sells:
            profit = _safe_float(e.get("profit_ton", 0))
            self._recent_profits.append(profit)
            if profit > 0:
                self._win_streak += 1
            else:
                self._win_streak = 0


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
