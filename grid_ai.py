"""
grid_ai.py — Самообучающийся AI-оптимизатор параметров сетки

Учится на реальных сделках прямо во время торговли:
  • Предсказывает оптимальный шаг сетки по текущему рынку (ATR + режим)
  • Рекомендует момент DCA-добавления позиции
  • Определяет оптимальный размер DCA-ордера
  • Хранит опыт в /app/data/grid_ai_experience.json
  • Переобучается автоматически после каждого нового fill
"""

import os
import json
import time
import threading
import logging
from typing import Optional

log = logging.getLogger("grid_ai")

DATA_DIR        = os.getenv("DATA_DIR", ".")
EXPERIENCE_FILE = os.path.join(DATA_DIR, "grid_ai_experience.json")

# Минимум примеров для первого обучения
MIN_SAMPLES = 5


class GridAI:
    """Самообучающийся AI-оптимизатор для сеточной торговли."""

    def __init__(self):
        self._lock      = threading.RLock()
        self._experience: list = []
        self._step_model  = None   # Ridge для предсказания шага
        self._dca_model   = None   # LogisticRegression для DCA-решения
        self._trained     = False
        self._last_train_n = 0
        self._load_experience()
        if len(self._experience) >= MIN_SAMPLES:
            self._train()
        log.info("[GridAI] Инициализирован. Примеров: %d, обучен: %s",
                 len(self._experience), self._trained)

    # ── Публичное API ─────────────────────────────────────────────────────────

    def get_optimal_step(self, atr_pct: float, regime: str = "SIDEWAYS",
                         min_step: float = 4.0, max_step: float = 10.0) -> float:
        """Предсказать оптимальный шаг сетки.

        Если модель не обучена — возвращает heuristic по ATR + режиму.
        Всегда соблюдает min_step (порог безубыточности).
        """
        heuristic = self._heuristic_step(atr_pct, regime)
        heuristic = max(min_step, min(max_step, heuristic))

        if not self._trained or self._step_model is None:
            return heuristic

        try:
            feat = self._make_features(atr_pct, regime)
            pred = float(self._step_model.predict([feat])[0])
            pred = max(min_step, min(max_step, round(pred * 2) / 2))  # шаг 0.5%
            log.debug("[GridAI] step: heuristic=%.1f%% model=%.1f%% (ATR=%.2f%% regime=%s)",
                      heuristic, pred, atr_pct, regime)
            # Плавный переход: среднее между эвристикой и моделью пока данных мало
            weight = min(1.0, len(self._experience) / 50)
            blended = heuristic * (1 - weight) + pred * weight
            return max(min_step, min(max_step, round(blended * 2) / 2))
        except Exception as e:
            log.warning("[GridAI] predict_step error: %s", e)
            return heuristic

    def get_dca_confidence(self, atr_pct: float, regime: str,
                           drawdown_pct: float, price_vs_center_pct: float) -> float:
        """Уверенность что стоит делать DCA-добавление (0-100%).

        > 50% → рекомендуем DCA
        """
        # Жёсткие блокировки: pump/distribution, огромная просадка
        if regime in ("PUMP", "DISTRIBUTION", "POST_PUMP"):
            return 0.0
        if drawdown_pct > 50.0:
            return 0.0
        # Хорошие условия для DCA: боковик с приемлемой просадкой
        if not self._trained or self._dca_model is None:
            if atr_pct >= 2.0 and drawdown_pct < 40.0 and regime in ("SIDEWAYS", "TREND_UP", "UNKNOWN"):
                return 60.0
            return 25.0

        try:
            feat = self._make_features(atr_pct, regime, extra=[drawdown_pct,
                                                                price_vs_center_pct])
            prob = float(self._dca_model.predict_proba([feat])[0][1])
            # Штраф при большой просадке
            if drawdown_pct > 35.0:
                prob *= 0.6
            return round(prob * 100, 1)
        except Exception as e:
            log.warning("[GridAI] dca_confidence error: %s", e)
            return 25.0

    def get_dca_size_multiplier(self, cycle_num: int, win_rate: float) -> float:
        """Рекомендуемый множитель размера DCA-ордера.

        cycle_num: номер DCA (1, 2, 3 ...)
        win_rate:  процент прибыльных fills (0-100)
        Returns: 1.0 – 2.0 (сколько TON от базового размера)
        """
        # Чем выше win_rate и меньше уровень — тем смелее
        base = 1.0 + (min(cycle_num, 3) - 1) * 0.15  # +15% за каждый уровень
        if win_rate >= 70:
            base *= 1.1
        elif win_rate < 40:
            base *= 0.8
        return round(min(base, 2.0), 2)

    def record_fill(self, side: str, step_used: float, atr_pct: float,
                    regime: str, profit_ton: float, profit_pct: float,
                    is_dca: bool = False):
        """Записать результат исполненного уровня для обучения.

        Вызывать после каждого SELL или BUY (когда известен результат).
        """
        entry = {
            "ts":           time.time(),
            "side":         side,
            "step_used":    step_used,
            "atr_pct":      atr_pct,
            "regime":       regime,
            "profit_ton":   profit_ton,
            "profit_pct":   profit_pct,
            "is_dca":       is_dca,
            "is_profitable": 1 if profit_ton > 0 else 0,
        }
        with self._lock:
            self._experience.append(entry)
            if len(self._experience) > 1000:
                self._experience = self._experience[-1000:]
            self._save_experience()
            if (len(self._experience) >= MIN_SAMPLES and
                    len(self._experience) != self._last_train_n):
                self._train()
        log.info("[GridAI] 📝 Fill записан: side=%s step=%.1f%% profit=%+.4f TON "
                 "(%.2f%%). Примеров: %d, обучен: %s",
                 side, step_used, profit_ton, profit_pct,
                 len(self._experience), self._trained)

    def get_stats(self) -> dict:
        """Статистика для /api/grid/status."""
        with self._lock:
            sells = [e for e in self._experience if e.get("side") == "sell"]
            buys  = [e for e in self._experience if e.get("side") == "buy"]
            if not self._experience:
                return {"trained": False, "samples": 0}
            profits = [e["profit_ton"] for e in sells if "profit_ton" in e]
            win_rate = (sum(1 for p in profits if p > 0) / len(profits) * 100
                        if profits else 0)
            avg_step = (sum(e["step_used"] for e in sells) / len(sells)
                        if sells else 0)
            return {
                "trained":        self._trained,
                "samples":        len(self._experience),
                "sell_fills":     len(sells),
                "buy_fills":      len(buys),
                "avg_profit_ton": round(sum(profits) / len(profits), 4) if profits else 0,
                "total_profit_ton": round(sum(profits), 4),
                "win_rate_pct":   round(win_rate, 1),
                "avg_step_used":  round(avg_step, 2),
            }

    # ── Внутренние методы ─────────────────────────────────────────────────────

    def _heuristic_step(self, atr_pct: float, regime: str) -> float:
        """Эвристический шаг без ML — быстрый и надёжный."""
        if regime in ("PUMP",):
            return 8.0
        if regime in ("DISTRIBUTION", "POST_PUMP"):
            return 6.0
        if regime == "TREND_UP":
            # На тренде вверх: чуть шире — меньше уровней срабатывает, больше прибыль
            if atr_pct >= 4.0:
                return 8.0
            return 6.0
        if atr_pct >= 5.0:
            return 8.0
        if atr_pct >= 3.0:
            return 6.0
        if atr_pct >= 2.0:
            return 5.0
        return 4.0

    def _make_features(self, atr_pct: float, regime: str, extra: list = None) -> list:
        """Вектор признаков для sklearn-моделей."""
        regime_map = {
            "TREND_UP": 2, "TREND_DOWN": -2,
            "SIDEWAYS": 0, "PUMP": 3,
            "DISTRIBUTION": -1, "POST_PUMP": -3,
            "UNKNOWN": 0,
        }
        regime_enc = regime_map.get(regime if isinstance(regime, str) else "UNKNOWN", 0)
        try:
            atr_pct = float(atr_pct)
        except (TypeError, ValueError):
            atr_pct = 0.0
        feat = [
            atr_pct,
            regime_enc,
            atr_pct ** 2,
            abs(regime_enc),
            atr_pct * regime_enc,
        ]
        if extra:
            feat.extend(extra)
        return feat

    def _train(self):
        """Обучить/переобучить модели на накопленном опыте."""
        try:
            from sklearn.linear_model import Ridge, LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline

            sells = [e for e in self._experience if e.get("side") == "sell"]

            def _safe_atr(e: dict) -> float:
                """Коерция atr_pct к float — защита от строк/None в старых записях."""
                try:
                    return float(e.get("atr_pct") or 0.0)
                except (TypeError, ValueError):
                    return 0.0

            # ── Step-predictor (Ridge regression) ─────────────────────────
            if len(sells) >= MIN_SAMPLES:
                X_s = [self._make_features(_safe_atr(e), e.get("regime", "SIDEWAYS"))
                       for e in sells]
                y_s = [e["step_used"] for e in sells]
                self._step_model = Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  Ridge(alpha=1.0)),
                ])
                self._step_model.fit(X_s, y_s)

            # ── DCA-classifier (LogisticRegression) ────────────────────────
            all_exp = self._experience
            if len(all_exp) >= MIN_SAMPLES:
                y_p = [e["is_profitable"] for e in all_exp]
                n_pos = sum(y_p)
                n_neg = len(y_p) - n_pos
                if n_pos >= 2 and n_neg >= 1:
                    X_p = [self._make_features(_safe_atr(e), e.get("regime", "SIDEWAYS"))
                           for e in all_exp]
                    self._dca_model = Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  LogisticRegression(C=1.0, max_iter=300, class_weight="balanced")),
                    ])
                    self._dca_model.fit(X_p, y_p)
                else:
                    # Пока все сделки одного класса (только прибыльные) —
                    # DCA-модель не нужна: всегда отвечаем «да» из эвристики.
                    log.info("[GridAI] DCA-модель пропущена: нет убыточных примеров "
                             "(pos=%d neg=%d) — используем эвристику", n_pos, n_neg)

            self._trained      = True
            self._last_train_n = len(self._experience)
            log.info("[GridAI] ✅ Обучение завершено: %d примеров (%d sells), "
                     "step_model=%s dca_model=%s",
                     len(self._experience), len(sells),
                     "OK" if self._step_model else "—",
                     "OK" if self._dca_model  else "—")

        except ImportError:
            log.warning("[GridAI] sklearn не найден, работаем в heuristic-режиме")
        except Exception as e:
            log.error("[GridAI] Ошибка обучения: %s", e, exc_info=True)

    def _save_experience(self):
        try:
            os.makedirs(os.path.dirname(EXPERIENCE_FILE) or ".", exist_ok=True)
            with open(EXPERIENCE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._experience, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("[GridAI] Сохранение опыта: %s", e)

    def _load_experience(self):
        try:
            if os.path.exists(EXPERIENCE_FILE):
                with open(EXPERIENCE_FILE, encoding="utf-8") as f:
                    self._experience = json.load(f)
                log.info("[GridAI] Загружено %d примеров из %s",
                         len(self._experience), EXPERIENCE_FILE)
        except Exception as e:
            log.warning("[GridAI] Загрузка опыта: %s", e)
            self._experience = []


# ── Синглтон ──────────────────────────────────────────────────────────────────

_instance: Optional[GridAI] = None
_init_lock = threading.Lock()


def get_grid_ai() -> GridAI:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = GridAI()
    return _instance
