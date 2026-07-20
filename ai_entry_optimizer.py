"""
AI Entry Optimizer — умный оптимизатор момента входа в DCA.

Когда цена падает и DCA-логика хочет войти, этот модуль решает:
  - Входить СЕЙЧАС (дно рядом, объём подтверждает, умные деньги покупают)
  - Или ПОДОЖДАТЬ более глубокого дипа (продавцы ещё сильны)

Модель: градиентный бустинг (fast, low-RAM, без GPU).
Обучается онлайн на каждой закрытой сделке: если после входа цена упала
ещё глубже — следующий раз подождём.

Интеграция: вызывается из trader.py в момент DCA-сигнала.
"""

import time
import math
import logging
import threading
from typing import Optional

log = logging.getLogger("ai_entry_optimizer")

# ─── Константы ────────────────────────────────────────────────────────────────
WAIT_DEEPER_DEFAULT  = 3.0   # если модель говорит «ждать» — ждать ещё N% падения
MIN_CONFIDENCE_ENTER = 0.55  # порог уверенности для «входить сейчас»
MEMORY_MAX_SAMPLES   = 500   # максимум обучающих примеров

# ─── Обучающий буфер ──────────────────────────────────────────────────────────
_lock    = threading.Lock()
_samples = []          # [(features_dict, label)]  label=1→вход был хорошим
_model   = None        # sklearn GBM или None до первого обучения
_model_trained_at = 0.0
_RETRAIN_EVERY = 20    # обучать каждые N новых сэмплов


def _build_features(drop_pct: float, rsi: float, volume_ratio: float,
                    momentum: str, regime: str, sm_score: float,
                    atr_pct: float, pump_score: float) -> list:
    """Вектор признаков для предсказания."""
    mom_enc = {"EXPLOSIVE": 3, "SURGE": 2, "BUILDING": 1, "CALM": 0}.get(momentum, 0)
    reg_enc = {
        "DOWNTREND": -2, "VOLATILE": -1, "RANGING": 0,
        "TRANSITION": 0, "SQUEEZE": 1, "UPTREND": 2,
        "BREAKOUT": 3, "POST_PUMP": -2,
    }.get(regime, 0)
    return [
        min(drop_pct, 40.0),          # % падения от пика (cap 40)
        max(0.0, min(rsi, 100.0)),     # RSI
        min(volume_ratio, 5.0),        # отношение объёма к среднему
        mom_enc,                       # моментум
        reg_enc,                       # режим
        max(-1.0, min(sm_score, 1.0)), # умные деньги (-1..+1)
        min(atr_pct, 10.0),            # волатильность
        min(pump_score, 1.0),          # pump score
        drop_pct * volume_ratio,       # взаимодействие: сильный дроп + объём
        drop_pct / max(atr_pct, 0.1),  # дроп в единицах ATR
    ]


def _try_retrain():
    """Переобучает модель если накопилось достаточно сэмплов."""
    global _model, _model_trained_at
    with _lock:
        n = len(_samples)
        if n < 15:
            return
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            import numpy as np
            X = np.array([s[0] for s in _samples])
            y = np.array([s[1] for s in _samples])
            clf = GradientBoostingClassifier(
                n_estimators=60, max_depth=3,
                learning_rate=0.12, subsample=0.8,
                random_state=42
            )
            clf.fit(X, y)
            _model = clf
            _model_trained_at = time.time()
            log.info(f"[EntryOpt] ✅ Модель обучена на {n} примерах "
                     f"(positive={y.sum():.0f}/{n})")
        except Exception as e:
            log.warning(f"[EntryOpt] retrain error: {e}")


def record_outcome(features: list, entry_was_good: bool):
    """
    После закрытия сделки — записываем был ли вход хорошим.
    entry_was_good=True если цена после входа не упала ещё >2% перед ростом.
    """
    global _samples
    with _lock:
        _samples.append((features, int(entry_was_good)))
        if len(_samples) > MEMORY_MAX_SAMPLES:
            _samples = _samples[-MEMORY_MAX_SAMPLES:]
        should_retrain = len(_samples) % _RETRAIN_EVERY == 0
    if should_retrain:
        threading.Thread(target=_try_retrain, daemon=True,
                         name="entry-opt-train").start()


def should_enter_now(
    drop_pct: float,
    rsi: float = 50.0,
    volume_ratio: float = 1.0,
    momentum: str = "CALM",
    regime: str = "UNKNOWN",
    sm_score: float = 0.0,
    atr_pct: float = 2.0,
    pump_score: float = 0.0,
) -> dict:
    """
    Главная функция: стоит ли входить в DCA прямо сейчас?

    Returns:
        {
          "enter":           bool,   # True = входить сейчас
          "confidence":      float,  # 0.0-1.0
          "wait_drop_pct":   float,  # сколько ещё ждать падения если enter=False
          "reason":          str,    # объяснение
          "features":        list,   # вектор признаков (для record_outcome)
        }
    """
    feats = _build_features(drop_pct, rsi, volume_ratio, momentum,
                            regime, sm_score, atr_pct, pump_score)

    # ── Правило 1: паника (RSI < 20) → умные деньги уже скупают → ВХОДИТЬ ─────
    if rsi < 20 and sm_score > 0.1:
        return {"enter": True, "confidence": 0.82, "wait_drop_pct": 0.0,
                "reason": "RSI паника + умные деньги покупают",
                "features": feats}

    # ── Правило 2: распродажа умных денег → ЖДАТЬ ─────────────────────────────
    if sm_score <= -0.3 and drop_pct < 5.0:
        return {"enter": False, "confidence": 0.75,
                "wait_drop_pct": WAIT_DEEPER_DEFAULT,
                "reason": "умные деньги продают — ждём дна",
                "features": feats}

    # ── ML-предсказание ────────────────────────────────────────────────────────
    with _lock:
        model = _model

    if model is not None:
        try:
            import numpy as np
            prob = model.predict_proba([feats])[0]
            p_good = float(prob[1])  # вероятность что вход хороший

            if p_good >= MIN_CONFIDENCE_ENTER:
                return {"enter": True, "confidence": p_good,
                        "wait_drop_pct": 0.0,
                        "reason": f"ML: вход выгоден (p={p_good:.0%})",
                        "features": feats}
            else:
                # Ждать тем больше, чем меньше уверенность
                extra_wait = WAIT_DEEPER_DEFAULT * (1.0 - p_good)
                extra_wait = round(max(1.5, min(extra_wait, 8.0)), 1)
                return {"enter": False, "confidence": 1 - p_good,
                        "wait_drop_pct": extra_wait,
                        "reason": f"ML: рано входить (p={p_good:.0%}), ждём -{extra_wait}%",
                        "features": feats}
        except Exception as e:
            log.debug(f"[EntryOpt] predict error: {e}")

    # ── Эвристика до первого обучения ─────────────────────────────────────────
    # Простые правила на основе RSI + объёма + умных денег
    score = 0.5
    if rsi < 35:   score += 0.15
    if rsi < 50:   score += 0.05
    if volume_ratio > 2.0: score += 0.10   # объём подтверждает
    if volume_ratio < 0.5: score -= 0.10   # слабый объём = нет спроса
    if sm_score > 0.2: score += 0.12
    if sm_score < -0.1: score -= 0.10
    if momentum in ("SURGE", "EXPLOSIVE"): score -= 0.08  # ещё падает
    if drop_pct > 15: score += 0.10        # глубокий дип = интереснее

    score = max(0.1, min(score, 0.95))
    enter = score >= MIN_CONFIDENCE_ENTER
    wait  = 0.0 if enter else round(WAIT_DEEPER_DEFAULT * (1 - score / 0.55), 1)
    return {
        "enter": enter,
        "confidence": score,
        "wait_drop_pct": wait,
        "reason": f"эвристика (нет модели): score={score:.0%}",
        "features": feats,
    }


def get_status() -> dict:
    with _lock:
        n = len(_samples)
        trained = _model is not None
    return {
        "trained":     trained,
        "samples":     n,
        "model":       "GradientBoosting" if trained else "heuristic",
        "trained_at":  int(_model_trained_at) if trained else 0,
        "description": "Оптимизатор момента DCA-входа",
    }
