"""
AI Take-Profit Optimizer — динамический предиктор оптимального TP.

Вместо фиксированного DCA_TARGET_PROFIT_PCT (например, 8%) модель предсказывает:
  «В ТЕКУЩИХ условиях цена, скорее всего, вырастет на X% перед откатом»

Это позволяет:
  - Брать больше прибыли при памп-режимах (UPTREND + BREAKOUT → TP=15-25%)
  - Не жадничать при боковике (RANGING → TP=4-6%)
  - Выходить раньше при пост-памп распределении (POST_PUMP → TP=2-4%)

Модель: ExtraTreesRegressor (быстрый, устойчивый к выбросам).
Обучается на реальных закрытых сделках бота.
"""

import time
import math
import logging
import threading
from typing import Optional

log = logging.getLogger("ai_tp_optimizer")

# ─── Пределы предсказания ─────────────────────────────────────────────────────
TP_MIN_PCT     = 2.5    # никогда не ставим TP ниже 2.5%
TP_MAX_PCT     = 35.0   # никогда выше 35%
TP_DEFAULT_PCT = 8.0    # дефолт до обучения модели

# ─── Режимные дефолты (когда модель ещё не обучена) ───────────────────────────
_REGIME_DEFAULTS = {
    "UPTREND":    12.0,
    "BREAKOUT":   18.0,
    "SQUEEZE":     7.0,
    "RANGING":     5.0,
    "TRANSITION":  6.0,
    "VOLATILE":    8.0,
    "DOWNTREND":   4.0,
    "POST_PUMP":   3.5,
    "UNKNOWN":     8.0,
}

_lock    = threading.Lock()
_samples = []      # [(features, actual_peak_pct)]
_model   = None    # sklearn ExtraTreesRegressor
_model_trained_at = 0.0
_RETRAIN_EVERY    = 15


def _build_features(regime: str, pump_score: float, momentum: str,
                    rsi: float, atr_pct: float, sm_score: float,
                    volume_ratio: float, dca_entries: int,
                    hours_in_trade: float, confidence: float) -> list:
    """Вектор признаков для предсказания TP."""
    mom_enc = {"EXPLOSIVE": 3, "SURGE": 2, "BUILDING": 1, "CALM": 0}.get(momentum, 0)
    reg_enc = {
        "DOWNTREND": -2, "POST_PUMP": -2, "VOLATILE": -1,
        "RANGING": 0, "TRANSITION": 0, "SQUEEZE": 1,
        "UPTREND": 2, "BREAKOUT": 3,
    }.get(regime, 0)
    return [
        reg_enc,                            # режим рынка
        min(pump_score, 1.0),               # сила памп-паттерна
        mom_enc,                            # моментум
        max(0.0, min(rsi, 100.0)),          # RSI
        min(atr_pct, 15.0),                 # ATR%
        max(-1.0, min(sm_score, 1.0)),      # умные деньги
        min(volume_ratio, 6.0),             # объём
        min(dca_entries, 5),                # сколько раз уже докупали
        min(hours_in_trade, 72.0),          # часов в позиции
        max(0.0, min(confidence, 1.0)),     # уверенность ML
        reg_enc * pump_score,               # взаимодействие режим×памп
        atr_pct * volume_ratio,             # взаимодействие ATR×объём
    ]


def _try_retrain():
    """Переобучение модели."""
    global _model, _model_trained_at
    with _lock:
        n = len(_samples)
        if n < 12:
            return
    try:
        from sklearn.ensemble import ExtraTreesRegressor
        import numpy as np
        with _lock:
            data = list(_samples)
        X = np.array([s[0] for s in data])
        y = np.array([s[1] for s in data])
        reg = ExtraTreesRegressor(
            n_estimators=80, max_depth=5,
            min_samples_leaf=2, random_state=42,
            n_jobs=1
        )
        reg.fit(X, y)
        with _lock:
            _model = reg
            _model_trained_at = time.time()
        log.info(f"[TPOpt] ✅ Модель обучена на {n} примерах "
                 f"(avg_tp={y.mean():.1f}% min={y.min():.1f}% max={y.max():.1f}%)")
    except Exception as e:
        log.warning(f"[TPOpt] retrain error: {e}")


def record_trade_result(features: list, actual_peak_pct: float):
    """
    Записываем реальный результат: какой максимальный % роста был
    достигнут в сделке (от цены входа до high_water_mark).
    """
    global _samples
    if actual_peak_pct <= 0:
        return
    with _lock:
        _samples.append((features, min(actual_peak_pct, TP_MAX_PCT)))
        if len(_samples) > 500:
            _samples = _samples[-500:]
        should_retrain = len(_samples) % _RETRAIN_EVERY == 0
    if should_retrain:
        threading.Thread(target=_try_retrain, daemon=True,
                         name="tp-opt-train").start()


def predict_tp(
    regime: str = "UNKNOWN",
    pump_score: float = 0.0,
    momentum: str = "CALM",
    rsi: float = 50.0,
    atr_pct: float = 2.0,
    sm_score: float = 0.0,
    volume_ratio: float = 1.0,
    dca_entries: int = 1,
    hours_in_trade: float = 0.0,
    confidence: float = 0.5,
) -> dict:
    """
    Предсказывает оптимальный TP для текущих условий.

    Returns:
        {
          "tp_pct":       float,   # рекомендуемый TP в %
          "tp_min_pct":   float,   # минимально приемлемый TP
          "confidence":   float,   # уверенность модели
          "regime_label": str,     # человекочитаемый режим
          "source":       str,     # "model" / "regime_default" / "fallback"
          "features":     list,    # для record_trade_result
        }
    """
    feats = _build_features(regime, pump_score, momentum, rsi, atr_pct,
                            sm_score, volume_ratio, dca_entries,
                            hours_in_trade, confidence)

    # ── ML-предсказание ────────────────────────────────────────────────────────
    with _lock:
        model = _model

    if model is not None:
        try:
            import numpy as np
            raw_tp = float(model.predict([feats])[0])
            tp_pct = max(TP_MIN_PCT, min(raw_tp, TP_MAX_PCT))

            # Оценка уверенности через дисперсию деревьев
            tree_preds = np.array([t.predict([feats])[0]
                                   for t in model.estimators_])
            std = float(tree_preds.std())
            conf = max(0.3, 1.0 - min(std / max(raw_tp, 1.0), 0.7))

            regime_label = _regime_label(regime, tp_pct)
            log.debug(f"[TPOpt] ML: {tp_pct:.1f}% (raw={raw_tp:.1f} std={std:.1f})")
            return {
                "tp_pct":       round(tp_pct, 1),
                "tp_min_pct":   round(max(TP_MIN_PCT, tp_pct * 0.6), 1),
                "confidence":   round(conf, 3),
                "regime_label": regime_label,
                "source":       "model",
                "features":     feats,
            }
        except Exception as e:
            log.debug(f"[TPOpt] predict error: {e}")

    # ── Режимные дефолты ───────────────────────────────────────────────────────
    base_tp = _REGIME_DEFAULTS.get(regime, TP_DEFAULT_PCT)

    # Корректируем под текущие условия
    if pump_score > 0.6:  base_tp *= 1.4
    if pump_score < 0.2:  base_tp *= 0.85
    if momentum == "EXPLOSIVE": base_tp *= 1.3
    if momentum == "SURGE":     base_tp *= 1.15
    if sm_score > 0.3:    base_tp *= 1.1
    if sm_score < -0.2:   base_tp *= 0.8
    if dca_entries >= 3:  base_tp *= 1.2  # больше докупок → ждём отскок
    if atr_pct > 5.0:     base_tp *= 1.1  # волатильный рынок → дальше TP

    tp_pct = max(TP_MIN_PCT, min(round(base_tp, 1), TP_MAX_PCT))
    return {
        "tp_pct":       tp_pct,
        "tp_min_pct":   round(max(TP_MIN_PCT, tp_pct * 0.6), 1),
        "confidence":   0.5,
        "regime_label": _regime_label(regime, tp_pct),
        "source":       "regime_default",
        "features":     feats,
    }


def _regime_label(regime: str, tp_pct: float) -> str:
    emoji = {
        "UPTREND": "📈", "BREAKOUT": "🚀", "SQUEEZE": "🔁",
        "RANGING": "↔️", "DOWNTREND": "📉", "POST_PUMP": "🔻",
        "VOLATILE": "⚡", "TRANSITION": "🔄",
    }.get(regime, "❓")
    return f"{emoji} {regime} → TP {tp_pct:.1f}%"


def get_status() -> dict:
    with _lock:
        n = len(_samples)
        trained = _model is not None
        avg_tp = (sum(s[1] for s in _samples) / n) if n > 0 else 0.0
    return {
        "trained":     trained,
        "samples":     n,
        "avg_real_tp": round(avg_tp, 2),
        "model":       "ExtraTreesRegressor" if trained else "regime_defaults",
        "description": "Динамический предиктор Take-Profit",
    }
