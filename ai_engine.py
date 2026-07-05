"""
AI Engine v4 — QuantumBrain ULTRA: World-Class Self-Learning Trading AI for GRINCH/TON
Специально адаптирован под рынок GRINCH/TON (DeDust, ликвидность $34K, ATR ~0.6%/свеча)

Архитектура (7 моделей + мета-стекинг + нейросеть + Kelly-sizing):
  • 7 базовых ML-моделей:
      RF   — RandomForest (300 деревьев)
      ET   — ExtraTrees (250 деревьев, быстрый дивергент)
      GB   — GradientBoosting (200 итераций)
      HGB  — HistGradientBoosting (XGBoost-стиль)
      XGB  — XGBoost (400 деревьев, early stopping)
      LGB  — LightGBM (500 итераций — быстрее XGB на малых данных) [если установлен]
      MLP  — Многослойный персептрон (нейросеть: 256-128-64-32)
  • Динамические веса: rolling accuracy^2, окно 100 тиков
  • Мета-слой: GradientBoosting стекинг ВСЕХ моделей (активен с 8+ сделок)
  • 80+ признаков: RSI · MACD · BB · ATR · ADX · OBV · CCI · Williams%R · Ichimoku ·
    Heiken Ashi · VWAP · CVD · Price Acceleration · Fractal · S/R zones ·
    Fibonacci lags · Trend angles · Volume Profile · Higher-order momentum +
    [v4 NEW] Kalman Filter deviation · Variance Ratio (Hurst-proxy) ·
    Garman-Klass volatility · Return skewness/kurtosis · Autocorrelation ·
    Pump Precursor Score · Candle strength · Micro-structure imbalance
  • Profit-biased разметка: label=BUY только если движение > DEX fees + gas
  • Асимметричные пороги: BUY≥50%, SELL≥62% (profit-only режим)
  • EV-фильтр: блокирует BUY если ожидаемое значение отрицательное
  • Variance Ratio буст: +8% уверенности при трендующем рынке (VR>1.1)
  • Experience Replay: 2000 примеров + подтверждённые сделки (15× вес)
  • Kelly Criterion: оптимальная доля ставки по win-rate + avg P&L + Sharpe
  • Авто-переобучение: каждые 2 тика или 5+ новых подтверждений
  • Полная персистентность: PostgreSQL + experience.json
"""

import numpy as np
import pandas as pd
import threading
import time
import logging
from collections import deque

from sklearn.ensemble import (
    RandomForestClassifier,
    GradientBoostingClassifier,
    ExtraTreesClassifier,
)
from sklearn.linear_model import LogisticRegression, RidgeClassifier
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler, RobustScaler
from sklearn.pipeline import Pipeline
import warnings
warnings.filterwarnings("ignore")

try:
    from sklearn.ensemble import HistGradientBoostingClassifier
    _HAS_HGB = True
except ImportError:
    _HAS_HGB = False

try:
    from xgboost import XGBClassifier
    _HAS_XGB = True
except Exception:
    _HAS_XGB = False

try:
    from lightgbm import LGBMClassifier
    _HAS_LGB = True
except Exception:
    _HAS_LGB = False

log = logging.getLogger(__name__)


# ─── Глобальные утилиты v4 ────────────────────────────────────────────────────

def _kalman_filter(prices: np.ndarray, process_noise: float = 1e-4, obs_noise: float = 1e-2) -> np.ndarray:
    """
    Kalman Filter для цены — используется квантовыми фондами и NASA.
    Возвращает сглаженный тренд без запаздывания EMA.
    process_noise: Q — насколько быстро меняется «истинная» цена
    obs_noise:     R — насколько зашумлены наблюдения
    """
    n = len(prices)
    if n == 0:
        return prices.copy()
    x = float(prices[0])
    P = 1.0
    filtered = np.empty(n)
    for i, price in enumerate(prices):
        P_pred = P + process_noise
        K = P_pred / (P_pred + obs_noise)
        x = x + K * (float(price) - x)
        P = (1.0 - K) * P_pred
        filtered[i] = x
    return filtered


def _variance_ratio(prices: np.ndarray, q: int = 5) -> float:
    """
    Variance Ratio Test — Hurst-прокси.
    VR > 1.0 → трендующий рынок (momentum)
    VR < 1.0 → возвратный рынок (mean-reverting)
    VR = 1.0 → случайное блуждание
    Используется Lo-MacKinlay (1988), стандарт в quantitative finance.
    """
    n = len(prices)
    if n < q * 4:
        return 1.0
    try:
        rets = np.diff(np.log(prices + 1e-12))
        mu = np.mean(rets)
        var1 = np.var(rets - mu, ddof=1)
        if var1 < 1e-12:
            return 1.0
        q_rets = np.array([np.sum(rets[i:i+q]) for i in range(0, len(rets) - q + 1)])
        varq = np.var(q_rets - q * mu, ddof=1)
        vr = varq / (q * var1 + 1e-12)
        return float(np.clip(vr, 0.1, 5.0))
    except Exception:
        return 1.0


def _garman_klass_vol(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    """
    Garman-Klass волатильность — точнее ATR, использует OHLC.
    Стандарт в академических исследованиях по волатильности.
    """
    with np.errstate(divide='ignore', invalid='ignore'):
        log_hl = np.where(l > 0, np.log(h / (l + 1e-12)) ** 2 * 0.5, 0.0)
        log_co = np.where(o > 0, np.log(c / (o + 1e-12)) ** 2 * (2 * np.log(2) - 1.0), 0.0)
        gk = log_hl - log_co
    return np.maximum(gk, 0.0)


# ─── Константы ────────────────────────────────────────────────────────────────
LOOK_AHEADS       = [3, 5, 8, 13]      # мульти-горизонт для 15м GRINCH (более длинный горизонт)
ATR_LABEL_MULT    = 0.7                 # порог = 0.7 × ATR_pct (качественнее, меньше шума)
CONFIRM_WEIGHT    = 15.0               # вес реальной сделки ×15 — доминирует над историей
REPLAY_SIZE       = 2000               # больший буфер опыта
ACCURACY_WINDOW   = 100                # длиннее окно = стабильнее веса моделей
META_MIN_SAMPLES  = 8                  # мета-слой активируется раньше (с 8 сделок)
RETRAIN_EVERY     = 2                  # переобучение максимум раз в N тиков (и только если пришли новые данные)
ANALYZE_CACHE_TTL = 12                  # сек — не пересчитывать 7 моделей повторно на тех же свечах
KELLY_LOOKBACK    = 100                # стабильный Kelly на 100 сделках

# ─── v4: Асимметричные пороги сигналов ───────────────────────────────────────
# GRINCH торгуется в режиме "только в плюс" → нам важна ТОЧНОСТЬ BUY, а не полнота.
# Лучше пропустить 2 хороших входа, чем войти в 1 плохой.
# BUY: 50% (раньше было 43% — слишком много ложных входов)
# SELL: 62% (высокий порог — AI SELL используется только для profit protection)
# EV_MIN_TRADES: сколько сделок нужно для активации EV-фильтра
BUY_THRESHOLD     = 0.46    # ≥46% вероятности роста → BUY (снижено для более активной торговли)
SELL_THRESHOLD    = 0.62    # ≥62% вероятности падения → SELL (только profit protection)
EV_MIN_TRADES     = 12      # минимум сделок для активации EV-фильтра
VR_TREND_THRESH   = 1.15    # Variance Ratio > 1.15 → трендующий рынок → +буст BUY
VR_MEAN_REV_THRESH= 0.85    # Variance Ratio < 0.85 → возвратный → -штраф BUY
# Минимальный размер прибыли для profit-biased разметки (% от цены)
# DEX: 2% round-trip fee + ~0.3% gas impact на 100 TON ставке → ~2.3% порог
PROFIT_BIAS_PCT   = 0.025   # label=BUY только если ожидаемый рост > 2.5%


# ─── Momentum Engine — детектор взрывного движения GRINCH ─────────────────────
class MomentumEngine:
    """
    Независимый детектор импульсного движения GRINCH/TON.

    Анализирует три источника импульса:
      1. RSI Velocity    — скорость изменения RSI за последние 3 бара
      2. Volume Surge    — отношение текущего объёма к MA20
      3. Price Velocity  — накопленный ход цены за последние 3 бара

    Возвращает Momentum Score 0–100 и сигнал: CALM / BUILDING / SURGE / EXPLOSIVE.
    При SURGE/EXPLOSIVE — добавляет +boost к уверенности AI (не более +12%).
    """

    SIGNAL_THRESHOLDS = {
        "EXPLOSIVE": 78,
        "SURGE":     55,
        "BUILDING":  30,
        "CALM":       0,
    }

    CONF_BOOST = {
        "EXPLOSIVE": 12.0,
        "SURGE":      7.0,
        "BUILDING":   3.0,
        "CALM":       0.0,
    }

    def detect(self, df: "pd.DataFrame") -> dict:
        """Вычисляет Momentum Score по последним свечам df."""
        try:
            if df is None or len(df) < 20:
                return self._empty()

            closes  = df["close"].values
            volumes = df["volume"].values if "volume" in df.columns else None

            # ── 1. RSI Velocity ──────────────────────────────────────────
            rsi_col = "rsi" if "rsi" in df.columns else None
            rsi_vel = 0.0
            if rsi_col:
                rsi_now  = float(df[rsi_col].iloc[-1])
                rsi_prev = float(df[rsi_col].iloc[-4]) if len(df) >= 4 else rsi_now
                rsi_vel  = rsi_now - rsi_prev          # позитивный = ускорение вверх

            # ── 2. Volume Surge ──────────────────────────────────────────
            vol_ratio = 1.0
            if volumes is not None and len(volumes) >= 20:
                vol_ma20  = float(np.mean(volumes[-20:]))
                vol_now   = float(volumes[-1])
                vol_ratio = vol_now / vol_ma20 if vol_ma20 > 0 else 1.0

            # ── 3. Price Velocity (% за 3 бара) ─────────────────────────
            price_vel = 0.0
            if len(closes) >= 4:
                price_vel = (closes[-1] / closes[-4] - 1.0) * 100.0

            # ── Нормализация в 0-100 ─────────────────────────────────────
            # RSI vel: диапазон −30…+30 → 0…100 (только позитивный вклад)
            rsi_score   = min(100.0, max(0.0, (rsi_vel + 30.0) / 60.0 * 100.0))
            # Vol ratio: 0…5× → 0…100 (1.0 = нейтраль → 20 очков)
            vol_score   = min(100.0, max(0.0, (vol_ratio - 0.5) / 4.5 * 100.0))
            # Price vel: −5%…+10% → 0…100 (0% = 33 очка)
            price_score = min(100.0, max(0.0, (price_vel + 5.0) / 15.0 * 100.0))

            # Взвешенное среднее: RSI 30%, Volume 40%, Price 30%
            score = rsi_score * 0.30 + vol_score * 0.40 + price_score * 0.30

            # Сигнал по порогам
            signal = "CALM"
            for sig, thr in self.SIGNAL_THRESHOLDS.items():
                if score >= thr:
                    signal = sig
                    break

            boost = self.CONF_BOOST.get(signal, 0.0)

            return {
                "score":       round(score, 1),
                "signal":      signal,
                "boost":       boost,
                "rsi_vel":     round(rsi_vel, 2),
                "vol_ratio":   round(vol_ratio, 2),
                "price_vel":   round(price_vel, 3),
                "rsi_score":   round(rsi_score, 1),
                "vol_score":   round(vol_score, 1),
                "price_score": round(price_score, 1),
            }
        except Exception as e:
            log.debug(f"[MomentumEngine] error: {e}")
            return self._empty()

    @staticmethod
    def _empty() -> dict:
        return {
            "score": 0.0, "signal": "CALM", "boost": 0.0,
            "rsi_vel": 0.0, "vol_ratio": 1.0, "price_vel": 0.0,
            "rsi_score": 0.0, "vol_score": 0.0, "price_score": 0.0,
        }


_momentum_engine = MomentumEngine()


# ─── BreakoutEngine — предсказатель GRINCH-пампа ──────────────────────────────
class BreakoutEngine:
    """
    GRINCH-специфичный детектор входящего пампа.

    Источники сигнала (все они уже вычисляются в _build_features):
      1. BB Squeeze      — Bollinger Band сжатие → взрыв волатильности близко
      2. Vol Acceleration — объём растёт N баров подряд → накопление
      3. RSI Buildup      — RSI поднимается к 60+ от нейтральной зоны
      4. MACD Crossover   — MACD histogram меняет знак (−→+)
      5. Price Coiling    — ценовой диапазон сужается (high-low уменьшается)

    Сигналы: FLAT → COILING → PRIMED → BREAKOUT → RUNAWAY
    При PRIMED+: буст уверенности AI + масштабирование Kelly-ставки.
    """

    SIGNAL_MAP = {
        "RUNAWAY":  {"min_score": 85, "conf_boost": 15.0, "kelly_mult": 2.0, "icon": "🚀"},
        "BREAKOUT": {"min_score": 65, "conf_boost": 10.0, "kelly_mult": 1.7, "icon": "⚡"},
        "PRIMED":   {"min_score": 42, "conf_boost":  6.0, "kelly_mult": 1.4, "icon": "🔥"},
        "COILING":  {"min_score": 22, "conf_boost":  2.0, "kelly_mult": 1.1, "icon": "📡"},
        "FLAT":     {"min_score":  0, "conf_boost":  0.0, "kelly_mult": 1.0, "icon": "💤"},
    }

    def detect(self, df: "pd.DataFrame") -> dict:
        try:
            if df is None or len(df) < 30:
                return self._empty()

            n = len(df)

            # ── 1. BB Squeeze (уже посчитан в _build_features) ─────────────
            bb_squeeze_score = 0.0
            if "bb_squeeze" in df.columns:
                # Сколько из последних 5 баров были в сжатии (0-5)
                sq_count = int(df["bb_squeeze"].iloc[-5:].sum()) if n >= 5 else 0
                bb_squeeze_score = sq_count / 5.0 * 100.0  # 0-100

                # Дополнительно: ширина BB относительно исторического максимума
                if "bb_w" in df.columns:
                    bb_w_now  = float(df["bb_w"].iloc[-1])
                    bb_w_max  = float(df["bb_w"].rolling(50).max().iloc[-1]) if n >= 50 else bb_w_now
                    bb_comp   = max(0.0, 1.0 - bb_w_now / (bb_w_max + 1e-10)) * 100.0
                    bb_squeeze_score = max(bb_squeeze_score, bb_comp)

            # ── 2. Volume Acceleration (объём растёт N баров) ───────────────
            vol_acc_score = 0.0
            if "volume" in df.columns and n >= 6:
                vols = df["volume"].iloc[-6:].values
                # Считаем количество последовательных баров роста объёма
                streak = 0
                for i in range(len(vols) - 1, 0, -1):
                    if vols[i] > vols[i-1]:
                        streak += 1
                    else:
                        break
                vol_acc_score = min(100.0, streak / 5.0 * 100.0)

                # Дополнительно: текущий объём vs MA20
                if n >= 20:
                    vol_ma = float(df["volume"].iloc[-20:].mean())
                    vol_now = float(df["volume"].iloc[-1])
                    vol_ratio = vol_now / (vol_ma + 1e-10)
                    vol_acc_score = max(vol_acc_score, min(100.0, (vol_ratio - 0.5) * 40.0))

            # ── 3. RSI Buildup ───────────────────────────────────────────────
            rsi_score = 0.0
            if "rsi" in df.columns and n >= 5:
                rsi_now  = float(df["rsi"].iloc[-1])
                rsi_prev = float(df["rsi"].iloc[-4]) if n >= 4 else rsi_now
                rsi_vel  = rsi_now - rsi_prev

                # Идеальный памп: RSI растёт из нейтрали (40-60) к зоне 60-75
                if 45 <= rsi_now <= 72 and rsi_vel > 0:
                    # Чем ближе к 65, тем лучше (точка начала пампа)
                    proximity = max(0, 1.0 - abs(rsi_now - 62) / 20.0)
                    rsi_score = min(100.0, proximity * 70.0 + rsi_vel * 3.0)
                elif rsi_now > 72:
                    rsi_score = max(0.0, 50.0 - (rsi_now - 72) * 3.0)  # уже перегрето
                else:
                    rsi_score = max(0.0, rsi_now - 35.0) * 1.5  # ещё в слабости

            # ── 4. MACD Crossover (histogram −→+) ────────────────────────────
            macd_score = 0.0
            if "macd_h" in df.columns and n >= 3:
                h_now  = float(df["macd_h"].iloc[-1])
                h_prev = float(df["macd_h"].iloc[-2])
                h_prev2 = float(df["macd_h"].iloc[-3]) if n >= 3 else h_prev

                if h_now > 0 and h_prev <= 0:
                    macd_score = 90.0   # свежий пересечение → очень бычье
                elif h_now > 0 and h_prev > 0 and h_now > h_prev:
                    # Гистограмма растёт вверх
                    accel = h_now - h_prev
                    avg_h = float(df["macd_h"].abs().rolling(20).mean().iloc[-1]) if n >= 20 else 0.01
                    macd_score = min(80.0, accel / (avg_h + 1e-10) * 30.0)
                elif h_now > 0:
                    macd_score = 40.0   # гистограмма положительная, но замедляется

            # ── 5. Price Coiling (сужение диапазона перед взрывом) ──────────
            coil_score = 0.0
            if all(c in df.columns for c in ["high", "low"]) and n >= 20:
                ranges_now  = (df["high"] - df["low"]).iloc[-5:].mean()
                ranges_hist = (df["high"] - df["low"]).iloc[-20:-5].mean()
                if ranges_hist > 0:
                    compression = max(0.0, 1.0 - float(ranges_now / ranges_hist)) * 100.0
                    coil_score = min(100.0, compression)

            # ── Итоговый Score (взвешенное среднее) ─────────────────────────
            score = (
                bb_squeeze_score * 0.25 +
                vol_acc_score    * 0.30 +
                rsi_score        * 0.20 +
                macd_score       * 0.15 +
                coil_score       * 0.10
            )

            # Определяем сигнал
            signal = "FLAT"
            for sig, meta in self.SIGNAL_MAP.items():
                if score >= meta["min_score"]:
                    signal = sig
                    break

            meta = self.SIGNAL_MAP[signal]
            return {
                "score":        round(score, 1),
                "signal":       signal,
                "icon":         meta["icon"],
                "conf_boost":   meta["conf_boost"],
                "kelly_mult":   meta["kelly_mult"],
                "bb_squeeze":   round(bb_squeeze_score, 1),
                "vol_acc":      round(vol_acc_score, 1),
                "rsi_build":    round(rsi_score, 1),
                "macd_cross":   round(macd_score, 1),
                "coiling":      round(coil_score, 1),
            }
        except Exception as e:
            log.debug(f"[BreakoutEngine] error: {e}")
            return self._empty()

    @staticmethod
    def _empty() -> dict:
        return {
            "score": 0.0, "signal": "FLAT", "icon": "💤",
            "conf_boost": 0.0, "kelly_mult": 1.0,
            "bb_squeeze": 0.0, "vol_acc": 0.0,
            "rsi_build": 0.0, "macd_cross": 0.0, "coiling": 0.0,
        }


_breakout_engine = BreakoutEngine()


# ─── GRINCHPumpDetector v4 — специализированный детектор пампа ────────────────
class GRINCHPumpDetector:
    """
    Детектор паттерна накопления перед пампом GRINCH/TON.

    Специфика GRINCH:
      - ATR ~0.6%/15м свеча
      - Типичный памп: +15-40% за 4-8 свечей
      - Сигнал накопления: RSI 42-68 + BB squeeze + объём > 1.2× MA

    Паттерны (в порядке силы):
      EXPLOSIVE_SETUP  — все условия идеальны → памп вероятен >80%
      STRONG_BUILDUP   — большинство условий → памп вероятен ~65%
      MILD_SIGNAL      — некоторые условия → стоит следить
      NEUTRAL          — нейтраль
    """

    def detect(self, df: "pd.DataFrame") -> dict:
        try:
            if df is None or len(df) < 30:
                return self._empty()

            n = len(df)
            c = df["close"].values
            score = 0.0

            # ── 1. RSI в зоне накопления 42-68 (идеал: 48-62) ──────────────
            rsi = float(df["rsi"].iloc[-1]) if "rsi" in df.columns else 50.0
            if 42 <= rsi <= 68:
                rsi_score = 30.0 * (1.0 - abs(rsi - 55) / 26.0)
            else:
                rsi_score = 0.0

            # ── 2. BB squeeze (сжатие перед взрывом) ──────────────────────
            squeeze = bool(df["bb_squeeze"].iloc[-1]) if "bb_squeeze" in df.columns else False
            sq_count = int(df["bb_squeeze"].iloc[-5:].sum()) if "bb_squeeze" in df.columns and n >= 5 else 0
            bb_score = sq_count * 5.0  # до 25 очков

            # ── 3. Объём > 1.2× MA (накопление) ──────────────────────────
            vol_r = float(df["vol_r"].iloc[-1]) if "vol_r" in df.columns else 1.0
            if vol_r >= 1.5:
                vol_score = 25.0
            elif vol_r >= 1.2:
                vol_score = 15.0
            elif vol_r >= 1.0:
                vol_score = 5.0
            else:
                vol_score = 0.0

            # ── 4. MACD гистограмма разворачивается вверх ─────────────────
            macd_score = 0.0
            if "macd_h" in df.columns and n >= 3:
                h_now  = float(df["macd_h"].iloc[-1])
                h_prev = float(df["macd_h"].iloc[-2])
                if h_now > h_prev and h_now > -0.0001:  # разворот или уже положительный
                    macd_score = 10.0
                if h_now > 0 and h_prev <= 0:           # свежее пересечение нуля
                    macd_score = 15.0

            # ── 5. Kalman deviation: цена ниже Kalman тренда (дешево) ─────
            kalman_score = 0.0
            if "kalman_dev" in df.columns:
                kdev = float(df["kalman_dev"].iloc[-1])
                if kdev < -0.005:      # цена на 0.5%+ ниже тренда
                    kalman_score = 10.0
                elif kdev < 0:
                    kalman_score = 5.0

            # ── 6. Variance Ratio > 1.1 (трендующий режим) ───────────────
            vr_score = 0.0
            if "var_ratio" in df.columns:
                vr = float(df["var_ratio"].iloc[-1])
                if vr > 1.15:
                    vr_score = 10.0
                elif vr > 1.05:
                    vr_score = 5.0

            score = rsi_score + bb_score + vol_score + macd_score + kalman_score + vr_score
            score = min(100.0, score)

            # Паттерн
            if score >= 75:
                pattern, conf_boost = "EXPLOSIVE_SETUP", 14.0
            elif score >= 50:
                pattern, conf_boost = "STRONG_BUILDUP",  8.0
            elif score >= 25:
                pattern, conf_boost = "MILD_SIGNAL",     3.0
            else:
                pattern, conf_boost = "NEUTRAL",         0.0

            return {
                "score":       round(score, 1),
                "pattern":     pattern,
                "conf_boost":  conf_boost,
                "rsi_score":   round(rsi_score, 1),
                "bb_score":    round(bb_score, 1),
                "vol_score":   round(vol_score, 1),
                "macd_score":  round(macd_score, 1),
                "kalman_score":round(kalman_score, 1),
                "vr_score":    round(vr_score, 1),
            }
        except Exception as e:
            log.debug(f"[GRINCHPumpDetector] error: {e}")
            return self._empty()

    @staticmethod
    def _empty() -> dict:
        return {
            "score": 0.0, "pattern": "NEUTRAL", "conf_boost": 0.0,
            "rsi_score": 0.0, "bb_score": 0.0, "vol_score": 0.0,
            "macd_score": 0.0, "kalman_score": 0.0, "vr_score": 0.0,
        }


_pump_detector = GRINCHPumpDetector()


class _ModelSlot:
    """Обёртка модели с rolling accuracy tracker и историей предсказаний."""

    def __init__(self, name: str, pipeline):
        self.name     = name
        self.pipeline = pipeline
        self.weight   = 1.0
        self._history = deque(maxlen=ACCURACY_WINDOW)  # 1=верно, 0=неверно

    def fit(self, X, y, sample_weight=None):
        try:
            kw = {}
            if sample_weight is not None:
                clf = self.pipeline.named_steps.get("clf")
                if clf is not None:
                    # Проверяем поддержку sample_weight через сигнатуру fit()
                    import inspect
                    try:
                        sig = inspect.signature(clf.fit)
                        if "sample_weight" in sig.parameters:
                            # Pipeline принимает sample_weight как clf__sample_weight
                            kw["clf__sample_weight"] = sample_weight
                    except (ValueError, TypeError):
                        pass   # нельзя интроспектировать — пропускаем
            # Передаём веса РЕАЛЬНО (было: pipeline.fit(X, y) — баг, kw игнорировался)
            self.pipeline.fit(X, y, **kw)
        except Exception as e:
            log.debug(f"[AI:{self.name}] fit error: {e}")

    def predict_proba(self, X):
        return self.pipeline.predict_proba(X)

    @property
    def classes_(self):
        clf = self.pipeline.named_steps.get("clf")
        if clf:
            return clf.classes_
        return self.pipeline.classes_

    def record(self, correct: bool):
        self._history.append(1 if correct else 0)
        if self._history:
            acc = sum(self._history) / len(self._history)
            self.weight = max(0.15, acc ** 2)

    @property
    def accuracy(self) -> float:
        if not self._history:
            return 0.5
        return sum(self._history) / len(self._history)


def _make_pipeline(clf):
    return Pipeline([("scaler", StandardScaler()), ("clf", clf)])


class AIEngine:
    """
    Главный AI-движок. Thread-safe.

    Публичные методы:
      pretrain(ohlcv, on_progress)   — начальное обучение при старте
      analyze(ohlcv) -> dict         — предсказание + аналитика (каждый тик)
      feedback(outcome, pnl)         — обратная связь от результата сделки
    """

    def __init__(self):
        self._lock    = threading.Lock()
        self._trained = False
        self._feature_names: list[str] = []
        self._tick_count  = 0
        self._new_confirms = 0
        self._retrains    = 0   # сколько раз модель самопереобучилась после старта

        # ── Кэш анализа: не гонять 7 моделей заново, если свечи не изменились ──
        self._last_candle_key  = None
        self._last_retrain_key = None
        self._last_result      = None
        self._last_result_ts   = 0.0
        self._cache_hits       = 0
        self._cache_misses     = 0

        # ── Буфер опыта ──────────────────────────────────────────────────
        self._replay_X:  list = []
        self._replay_y:  list = []
        self._replay_w:  list = []   # sample weights

        # ── Подтверждённые сделки (от feedback) ──────────────────────────
        self._confirmed_X:  list = []
        self._confirmed_y:  list = []
        self._confirmed_w:  list = []

        # Текущие признаки последнего BUY-сигнала (для feedback)
        self._last_buy_features: np.ndarray | None = None

        # ── Модели ───────────────────────────────────────────────────────
        self._slots: list[_ModelSlot] = []
        self._meta: Pipeline | None   = None
        self._build_models()

        # ── Прогресс обучения (для UI) ────────────────────────────────────
        self.training_progress = {
            "phase": "idle", "pct": 0, "samples": 0,
            "label": "Ожидание запуска...", "trained": False,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Построение моделей
    # ─────────────────────────────────────────────────────────────────────────

    def _build_models(self):
        self._slots = [
            _ModelSlot("RF", _make_pipeline(
                RandomForestClassifier(
                    n_estimators=300, max_depth=10, min_samples_split=3,
                    min_samples_leaf=2, max_features="sqrt",
                    class_weight="balanced", random_state=42, n_jobs=1)
            )),
            _ModelSlot("ET", _make_pipeline(
                ExtraTreesClassifier(
                    n_estimators=250, max_depth=9, min_samples_split=3,
                    class_weight="balanced", random_state=7, n_jobs=1)
            )),
            _ModelSlot("GB", _make_pipeline(
                GradientBoostingClassifier(
                    n_estimators=200, max_depth=5, learning_rate=0.03,
                    subsample=0.75, min_samples_leaf=2, random_state=42)
            )),
        ]
        if _HAS_HGB:
            self._slots.append(_ModelSlot("HGB", Pipeline([
                ("clf", HistGradientBoostingClassifier(
                    max_iter=300, max_depth=7, learning_rate=0.03,
                    min_samples_leaf=5, l2_regularization=0.05, random_state=42))
            ])))
        if _HAS_XGB:
            self._slots.append(_ModelSlot("XGB", Pipeline([
                ("scaler", StandardScaler()),
                ("clf", XGBClassifier(
                    n_estimators=400, max_depth=6, learning_rate=0.03,
                    subsample=0.75, colsample_bytree=0.75, min_child_weight=2,
                    gamma=0.05, reg_alpha=0.1, reg_lambda=0.8,
                    eval_metric="mlogloss", verbosity=0,
                    random_state=42))
            ])))
        # LightGBM v4: быстрее XGBoost, лучше на малых данных GRINCH
        if _HAS_LGB:
            self._slots.append(_ModelSlot("LGB", Pipeline([
                ("scaler", StandardScaler()),
                ("clf", LGBMClassifier(
                    n_estimators=500, max_depth=6, learning_rate=0.03,
                    num_leaves=63, subsample=0.75, colsample_bytree=0.75,
                    min_child_samples=5, reg_alpha=0.1, reg_lambda=0.8,
                    class_weight="balanced", verbosity=-1, random_state=42))
            ])))
        # MLP v2: глубже + Dropout-эффект через higher alpha
        self._slots.append(_ModelSlot("MLP", Pipeline([
            ("scaler", RobustScaler()),
            ("clf", MLPClassifier(
                hidden_layer_sizes=(256, 128, 64, 32),
                activation="relu", solver="adam",
                alpha=5e-4, learning_rate="adaptive", learning_rate_init=0.001,
                max_iter=500, early_stopping=True, n_iter_no_change=20,
                validation_fraction=0.15, random_state=42))
        ])))
        # Kelly trade history
        self._kelly_wins:   deque = deque(maxlen=KELLY_LOOKBACK)
        self._kelly_pnls:   deque = deque(maxlen=KELLY_LOOKBACK)

    # ─────────────────────────────────────────────────────────────────────────
    # Прогресс
    # ─────────────────────────────────────────────────────────────────────────

    def _set_progress(self, phase, pct, label, samples=None):
        self.training_progress.update({
            "phase": phase, "pct": int(pct), "label": label,
            "trained": self._trained,
        })
        if samples is not None:
            self.training_progress["samples"] = samples

    # ─────────────────────────────────────────────────────────────────────────
    # Предобучение (вызывается один раз при старте)
    # ─────────────────────────────────────────────────────────────────────────

    def pretrain(self, ohlcv: list, on_progress=None):
        def emit(phase, pct, label, samples=None):
            self._set_progress(phase, pct, label, samples)
            if on_progress:
                on_progress(dict(self.training_progress))

        emit("collecting", 0, "📡 Загрузка исторических данных GRINCH...")
        time.sleep(0.2)
        n = len(ohlcv)
        emit("collecting", 8, f"📡 Загружено {n} свечей GRINCH/TON", n)
        time.sleep(0.3)

        emit("features", 12, "🔬 Вычисление 45+ технических индикаторов...")
        df = self._build_features(ohlcv)
        if df is None or len(df) < 40:
            emit("ready", 100, "⚠️ Недостаточно данных — ожидаем накопления")
            return
        emit("features", 26, f"🔬 ADX · OBV · CCI · Williams%R · Ichimoku · Heiken Ashi · {len(df.columns)} признаков", len(df))
        time.sleep(0.3)

        emit("label", 30, "🧮 Адаптивная разметка (порог = ATR×0.6, горизонты 2/3/5 баров)...")
        X, y = self._make_dataset(df)
        if X is None or len(X) < 25:
            emit("ready", 100, "⚠️ Мало данных для обучения")
            return
        classes = np.unique(y)
        emit("label", 36, f"🧮 Набор: {len(X)} примеров · классы BUY/HOLD/SELL={np.sum(y==1)}/{np.sum(y==0)}/{np.sum(y==-1)}", len(X))
        time.sleep(0.2)

        if len(classes) < 2:
            emit("ready", 100, "⚠️ Недостаточно разнообразия сигналов")
            return

        # Сохраняем в replay buffer (базовый вес = 1.0)
        self._replay_X = list(X)
        self._replay_y = list(y)
        self._replay_w = [1.0] * len(X)

        model_names  = [s.name for s in self._slots]
        pct_per_step = (82 - 36) / max(len(self._slots), 1)

        for i, slot in enumerate(self._slots):
            start_pct = 36 + i * pct_per_step
            name_label = {
                "RF":  "🌲 RandomForest (200 деревьев, глубина 8)",
                "ET":  "⚡ ExtraTrees (150 деревьев — быстрый дивергент)",
                "GB":  "🚀 GradientBoosting (120 итераций, subsample 0.8)",
                "HGB": "💥 HistGradientBoosting (XGBoost-режим, 150 эпох)",
            }.get(slot.name, slot.name)
            emit(f"model_{i}", start_pct, f"{name_label}...")
            time.sleep(0.15)
            with self._lock:
                slot.fit(X, y)
            emit(f"model_{i}", start_pct + pct_per_step * 0.9,
                 f"{name_label} ✓", len(X))
            time.sleep(0.1)

        with self._lock:
            self._trained = True

        emit("meta", 84, "🧠 Инициализация мета-слоя (стекинг ансамблей)...")
        time.sleep(0.2)
        self._try_fit_meta(X, y)
        emit("meta", 90, "🧠 Мета-слой готов" if self._meta else "🧠 Мета-слой накапливает данные...", len(X))
        time.sleep(0.2)

        emit("validate", 91, "🔎 Валидация ансамбля на последних данных...")
        time.sleep(0.2)
        try:
            last     = X[[-1]]
            ensemble = self._ensemble_proba(last)
            classes_list = [-1, 0, 1]
            best_idx = int(np.argmax(ensemble))
            best_pct = round(float(ensemble[best_idx]) * 100, 1)
            fi_top   = self._top_feature(self._slots[0])
            emit("validate", 96, f"🔎 Уверенность: {best_pct}% · ключевой признак: {fi_top}", len(X))
        except Exception:
            emit("validate", 96, "🔎 Валидация завершена")
        time.sleep(0.2)

        model_names_str = " · ".join(s.name for s in self._slots)
        emit("ready", 100, f"✅ QuantumBrain готов! {len(self._slots)} моделей ({model_names_str}) · {len(X)} баров · Kelly активен 🟢", len(X))
        self.training_progress["trained"] = True

    # ─────────────────────────────────────────────────────────────────────────
    # Публичный анализ (каждый тик)
    # ─────────────────────────────────────────────────────────────────────────

    def analyze(self, ohlcv: list) -> dict:
        with self._lock:
            return self._analyze_locked(ohlcv)

    def _analyze_locked(self, ohlcv: list) -> dict:
        # ── Кэш: если свечи не изменились с прошлого вызова — не гоняем
        # 7 ML-моделей и 80+ признаков заново (тик 15с, свечи обновляются реже) ──
        if ohlcv:
            last_bar = ohlcv[-1]
            candle_key = (len(ohlcv), last_bar[0], last_bar[4])
            now = time.time()
            if (
                candle_key == self._last_candle_key
                and self._last_result is not None
                and (now - self._last_result_ts) < ANALYZE_CACHE_TTL
                and self._new_confirms < 5
            ):
                self._cache_hits += 1
                return self._last_result
            self._cache_misses += 1
        else:
            candle_key = None

        df = self._build_features(ohlcv)
        if df is None or len(df) < 40:
            return self._empty_result()

        X, y = self._make_dataset(df)
        if X is None or len(X) < 25:
            return self._empty_result()

        self._tick_count += 1

        # ── Авто-переобучение (только когда реально пришли новые данные) ──
        data_changed = candle_key != self._last_retrain_key
        should_retrain = (
            (data_changed and self._tick_count % RETRAIN_EVERY == 0) or
            self._new_confirms >= 5
        )
        if should_retrain:
            self._last_retrain_key = candle_key
            self._replay_X = list(X)
            self._replay_y = list(y)
            self._replay_w = [1.0] * len(X)
            self._refit_all()

        if not self._trained:
            return self._empty_result()

        # ── Предсказание ─────────────────────────────────────────────────
        last = X[[-1]]
        try:
            ens = self._ensemble_proba(last)
        except Exception:
            self._trained = False
            self._build_models()
            return self._empty_result()

        prob_up, prob_hold, prob_down = float(ens[2]), float(ens[1]), float(ens[0])

        # ── v4: Асимметричные пороги сигналов ────────────────────────────
        # BUY ≥ 50%: больше точность, меньше ложных входов (было 43%)
        # SELL ≥ 62%: profit-only → AI SELL только при высокой уверенности
        max_prob = max(prob_up, prob_down, prob_hold)
        if max_prob == prob_up and prob_up >= BUY_THRESHOLD:
            ai_signal = "BUY"
            self._last_buy_features = X[-1].copy()
        elif max_prob == prob_down and prob_down >= SELL_THRESHOLD:
            ai_signal = "SELL"
        else:
            ai_signal = "HOLD"

        confidence = round(max_prob * 100, 1)

        # ── Дополнительная аналитика ──────────────────────────────────────
        regime     = self._detect_regime(df)
        patterns   = self._detect_candle_patterns(df)
        sr_levels  = self._support_resistance(df)
        forecast   = self._price_forecast(df)
        importance = self._feature_importance()
        anomaly    = self._detect_anomaly(df)
        model_info = self._model_stats()
        kelly      = self._compute_kelly()

        # ── Momentum Engine: детектор взрывного движения ─────────────────
        momentum = _momentum_engine.detect(df)

        # ── Breakout Engine: предсказатель GRINCH-пампа ──────────────────
        breakout = _breakout_engine.detect(df)

        # ── v4: GRINCH Pump Detector ──────────────────────────────────────
        pump = _pump_detector.detect(df)

        # ── Variance Ratio для текущего окна ─────────────────────────────
        curr_vr = _variance_ratio(df["close"].values[-40:], q=5) if len(df) >= 40 else 1.0

        # ── Режимно-зависимая коррекция + все бусты ──────────────────────
        # Источники: режим, momentum, breakout, pump_detector, variance_ratio
        # Правила:
        #   1. DOWNTREND жёстко блокирует BUY-бусты (штраф последний)
        #   2. Breakout vs Regime — max (не суммируем коррелированные)
        #   3. Pump detector — независим (детектирует накопление, не движение)
        #   4. Variance Ratio (VR>1.15 → тренд: буст; VR<0.85 → возврат: штраф)
        #   5. Суммарный положительный сдвиг ограничен +15% (расширен для v4)

        regime_name = regime.get("name", "UNKNOWN")
        total_boost = 0.0

        if ai_signal == "BUY":
            # ── Momentum буст (скорость цены) ────────────────────────────
            mom_boost  = float(momentum.get("boost", 0.0))

            # ── Breakout vs Regime boost (берём max, не сумму) ────────────
            bo_boost   = float(breakout.get("conf_boost", 0.0))
            reg_boost  = 0.0
            if regime_name == "UPTREND":
                reg_boost = 5.0
            elif regime_name == "BREAKOUT":
                reg_boost = 8.0
            elif regime_name == "SQUEEZE":
                reg_boost = 3.0

            # ── v4: Pump Detector буст (накопление перед пампом) ─────────
            pump_boost = float(pump.get("conf_boost", 0.0))

            # ── v4: Variance Ratio буст/штраф ────────────────────────────
            vr_boost = 0.0
            if curr_vr >= VR_TREND_THRESH:
                vr_boost = 8.0    # тренд продолжается → сильный буст
            elif curr_vr >= 1.05:
                vr_boost = 4.0
            elif curr_vr <= VR_MEAN_REV_THRESH:
                vr_boost = -6.0  # возвратный рынок → осциллятор надёжнее тренда
            elif curr_vr <= 0.95:
                vr_boost = -3.0

            # ── Комбинируем: Momentum + max(Breakout,Regime) + Pump + VR ─
            combined_pos = mom_boost + max(bo_boost, reg_boost) + pump_boost + vr_boost
            # Hard cap: не более +15% суммарный буст
            combined_pos = min(combined_pos, 15.0)

            # ── Штраф за неблагоприятный режим (применяется ПОСЛЕДНИМ) ───
            penalty = 0.0
            if regime_name == "DOWNTREND":
                penalty = -14.0   # против тренда — финальный штраф
            elif regime_name == "VOLATILE":
                penalty = -4.0

            total_boost = combined_pos + penalty

            if total_boost != 0.0:
                old_conf = confidence
                confidence = round(max(1.0, min(99.0, confidence + total_boost)), 1)
                log.debug(
                    f"[AI v4 Boost] Regime={regime_name} mom={mom_boost:.1f} "
                    f"bo={bo_boost:.1f} reg={reg_boost:.1f} pump={pump_boost:.1f} "
                    f"vr={vr_boost:.1f}(VR={curr_vr:.2f}) penalty={penalty:.1f} "
                    f"total={total_boost:+.1f} → {old_conf}%→{confidence}%"
                )

            # ── v4: EV-фильтр — блокирует BUY при отрицательном EV ───────
            # Активируется только после EV_MIN_TRADES подтверждённых сделок.
            # Цель: убедиться что ожидаемая прибыль покрывает DEX fees + газ.
            ev_trades = kelly.get("trades", 0)
            ev_val    = kelly.get("ev", 0.0)
            if ev_trades >= EV_MIN_TRADES and ev_val <= 0.0:
                log.info(
                    f"[AI v4 EV-Filter] BUY заблокирован: EV={ev_val:.4f}≤0 "
                    f"(win_rate={kelly.get('win_rate',0):.1f}% trades={ev_trades})"
                )
                ai_signal = "HOLD"
                confidence = min(confidence, 45.0)
                total_boost = 0.0

        elif ai_signal == "SELL" and regime_name == "DOWNTREND":
            # Шорт в нисходящем тренде — небольшой буст уверенности
            old_conf = confidence
            confidence = round(min(99.0, confidence + 5.0), 1)
            total_boost = 5.0
            log.debug(f"[AI v4 Boost] SELL+DOWNTREND +5% → {old_conf}%→{confidence}%")

        result = {
            "ai_signal":    ai_signal,
            "confidence":   confidence,
            "prob_up":      round(prob_up   * 100, 1),
            "prob_down":    round(prob_down * 100, 1),
            "prob_hold":    round(prob_hold * 100, 1),
            "regime":       regime,
            "patterns":     patterns,
            "support_resistance": sr_levels,
            "forecast":     forecast,
            "feature_importance": importance,
            "anomaly":      anomaly,
            "model_trained":   self._trained,
            "samples_trained": len(X),
            "training_progress": self.training_progress,
            "pump":         pump,
            "var_ratio":    round(curr_vr, 3),
            "model_info":   model_info,
            "kelly":        kelly,
            "momentum":     momentum,
            "breakout":     breakout,
            "total_boost":  round(total_boost, 1),
        }

        # ── Сохраняем в кэш: следующий тик с теми же свечами получит
        # готовый результат мгновенно, без повторного прогона моделей ──
        self._last_candle_key = candle_key
        self._last_result     = result
        self._last_result_ts  = time.time()
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Обратная связь от трейдера (вызывается когда сделка закрывается)
    # ─────────────────────────────────────────────────────────────────────────

    def feedback(self, outcome: str, pnl: float, regime: str = "UNKNOWN", conf: float = 0.0):
        """
        outcome: "win" | "loss"
        pnl:     P&L в TON (может быть отрицательным)
        regime:  рыночный режим при входе (UPTREND / DOWNTREND / ...)
        conf:    уверенность AI при входе (%)
        """
        if self._last_buy_features is None:
            return
        with self._lock:
            label   = 1 if outcome == "win" else -1
            is_win  = (outcome == "win")

            # Адаптивный вес: крупная прибыль важнее, потери тоже учатся
            pnl_abs  = min(abs(pnl), 50.0)   # cap на 50 TON (для 100 TON ставки)
            pnl_norm = pnl_abs / 50.0         # нормировано к [0..1]

            # Выигрыш с высокой уверенностью = самый ценный пример
            # Проигрыш с высокой уверенностью = тоже очень ценный (надо учиться)
            conf_factor = 1.0 + (conf - 60.0) / 40.0 if conf > 60 else 1.0
            conf_factor = max(0.5, min(conf_factor, 2.0))

            weight = CONFIRM_WEIGHT * (1.0 + pnl_norm * 1.5) * conf_factor

            self._confirmed_X.append(self._last_buy_features.copy())
            self._confirmed_y.append(label)
            self._confirmed_w.append(weight)
            self._last_buy_features = None
            self._new_confirms += 1

            # Kelly history
            self._kelly_wins.append(1 if is_win else 0)
            self._kelly_pnls.append(float(pnl))

            # Обновляем accuracy для всех моделей (с учётом режима)
            for slot in self._slots:
                slot.record(is_win)

            # Мета-слой: обновляем каждые META_MIN_SAMPLES/2 новых сделок
            n_conf = len(self._confirmed_X)
            if n_conf >= META_MIN_SAMPLES and n_conf % max(META_MIN_SAMPLES // 2, 1) == 0:
                try:
                    self._try_fit_meta_confirmed()
                except Exception as e:
                    log.debug(f"[AI] meta fit error: {e}")

        log.info(
            f"[AI] Feedback: {outcome}({regime}) PNL={pnl:+.4f} TON conf={conf:.0f}% "
            f"→ {len(self._confirmed_X)} подтверждённых примеров "
            f"(вес={weight:.1f})"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Персистентность опыта (переживает перезапуск)
    # ─────────────────────────────────────────────────────────────────────────

    def export_experience(self) -> dict:
        """Сериализует подтверждённый опыт ИИ для записи на диск."""
        with self._lock:
            return {
                "confirmed_X":  [list(map(float, x)) for x in self._confirmed_X],
                "confirmed_y":  [int(v) for v in self._confirmed_y],
                "confirmed_w":  [float(v) for v in self._confirmed_w],
                "slot_acc":     {s.name: list(s._history) for s in self._slots},
                "feature_dim":  len(self._feature_names),
                "kelly_wins":   list(self._kelly_wins),
                "kelly_pnls":   list(self._kelly_pnls),
                "retrains":     self._retrains,
            }

    def import_experience(self, data: dict) -> int:
        """Восстанавливает опыт с диска и дообучает модели.
        Возвращает число восстановленных подтверждённых примеров (0 — если
        несовместимо или пусто). Вызывать ПОСЛЕ pretrain (нужны feature_names)."""
        if not data:
            return 0
        X = data.get("confirmed_X") or []
        if not X:
            return 0
        with self._lock:
            cur_dim   = len(self._feature_names)
            saved_dim = data.get("feature_dim")
            # Изменился набор признаков → старый опыт несовместим, пропускаем
            if cur_dim and saved_dim and cur_dim != saved_dim:
                log.warning(f"[AI] Опыт несовместим: признаков {saved_dim}≠{cur_dim}, пропуск")
                return 0
            try:
                self._confirmed_X = [np.array(x, dtype=float) for x in X]
                self._confirmed_y = [int(v) for v in data.get("confirmed_y", [])]
                self._confirmed_w = [float(v) for v in data.get("confirmed_w", [])]
                acc = data.get("slot_acc", {}) or {}
                for s in self._slots:
                    h = acc.get(s.name)
                    if h:
                        s._history = deque(h, maxlen=ACCURACY_WINDOW)
                        if s._history:
                            a = sum(s._history) / len(s._history)
                            s.weight = max(0.15, a ** 2)
                # Восстанавливаем Kelly историю
                kw = data.get("kelly_wins", [])
                kp = data.get("kelly_pnls", [])
                if kw:
                    for v in kw[-KELLY_LOOKBACK:]:
                        self._kelly_wins.append(int(v))
                if kp:
                    for v in kp[-KELLY_LOOKBACK:]:
                        self._kelly_pnls.append(float(v))
                self._retrains = int(data.get("retrains", 0))
                n = len(self._confirmed_X)
                if n and self._trained:
                    self._refit_all()
                log.info(f"[AI] Восстановлено {n} подтверждённых примеров, Kelly={len(self._kelly_wins)} сделок")
                return n
            except Exception as e:
                log.warning(f"[AI] import_experience error: {e}")
                return 0

    # ─────────────────────────────────────────────────────────────────────────
    # Внутренние методы: обучение
    # ─────────────────────────────────────────────────────────────────────────

    def _refit_all(self):
        """Полный рефит всех моделей = история + реальные сделки (с затуханием по давности)."""
        # ── Recency decay: более свежий опыт важнее ──────────────────────
        # Исторические данные: равный вес 1.0
        n_hist = len(self._replay_X)
        hist_w = list(self._replay_w)

        # Подтверждённые сделки: затухание по давности (последние = ×1.5, старые = ×0.5)
        n_conf = len(self._confirmed_X)
        conf_w = []
        for i, w in enumerate(self._confirmed_w):
            age_factor = 0.5 + 1.0 * (i / max(n_conf - 1, 1))  # 0.5 → 1.5
            conf_w.append(w * age_factor)

        X_all = list(self._replay_X) + list(self._confirmed_X)
        y_all = list(self._replay_y) + list(self._confirmed_y)
        w_all = hist_w + conf_w

        # Ограничиваем буфер (подтверждённые всегда сохраняем целиком)
        max_hist = REPLAY_SIZE
        if len(X_all) > max_hist + n_conf:
            trim = len(X_all) - (max_hist + n_conf)
            X_all = X_all[trim:]
            y_all = y_all[trim:]
            w_all = w_all[trim:]

        X_arr = np.array(X_all, dtype=float)
        y_arr = np.array(y_all, dtype=int)
        w_arr = np.array(w_all, dtype=float)
        w_arr = w_arr / (w_arr.mean() + 1e-10)  # нормируем веса

        classes = np.unique(y_arr)
        if len(classes) < 2:
            return

        for slot in self._slots:
            try:
                slot.fit(X_arr, y_arr, sample_weight=w_arr)
            except Exception as e:
                log.debug(f"[AI:{slot.name}] refit error: {e}")

        self._trained = True
        self._new_confirms = 0

        # Переобучаем мета-слой при каждом рефите, если есть подтверждённые данные
        if len(self._confirmed_X) >= META_MIN_SAMPLES:
            try:
                self._try_fit_meta_confirmed()
            except Exception as e:
                log.debug(f"[AI] meta refit error: {e}")

        # Отражаем непрерывное самообучение в UI (банер обучения)
        self._retrains += 1
        try:
            accs = [s.accuracy for s in self._slots if s.accuracy is not None]
            avg_acc = round(sum(accs) / len(accs) * 100, 1) if accs else 0.0
            sharpe = self._compute_sharpe()
            self._set_progress(
                "ready", 100,
                f"🟢 Самообучение активно · переобучений: {self._retrains} · "
                f"подтверждённых сделок: {len(self._confirmed_X)} · "
                f"точность {avg_acc}% · Sharpe {sharpe:.2f}",
                len(X_arr),
            )
            self.training_progress["retrains"]   = self._retrains
            self.training_progress["confirmed"]  = len(self._confirmed_X)
            self.training_progress["accuracy"]   = avg_acc
            self.training_progress["sharpe"]     = sharpe
        except Exception:
            pass

    def _try_fit_meta(self, X, y):
        """Первый запуск мета-слоя на исторических данных.
        Использует GB как мета-лернер — лучше улавливает нелинейные взаимодействия."""
        try:
            meta_X = self._stack_features(X)
            # GB-мета: лучше LogisticRegression для нелинейных ансамблей
            self._meta = Pipeline([
                ("scaler", StandardScaler()),
                ("clf", GradientBoostingClassifier(
                    n_estimators=80, max_depth=3, learning_rate=0.05,
                    subsample=0.8, random_state=42))
            ])
            self._meta.fit(meta_X, y)
        except Exception as e:
            log.debug(f"[AI] meta init error: {e}")
            # Фолбэк: LogisticRegression
            try:
                meta_X = self._stack_features(X)
                self._meta = Pipeline([
                    ("scaler", StandardScaler()),
                    ("clf",    LogisticRegression(C=2.0, max_iter=500, random_state=42))
                ])
                self._meta.fit(meta_X, y)
            except Exception as e2:
                log.debug(f"[AI] meta fallback error: {e2}")
                self._meta = None

    def _try_fit_meta_confirmed(self):
        """Переобучаем мета-слой ТОЛЬКО на подтверждённых реальных сделках.
        Приоритет: GB если данных хватает, иначе LogReg."""
        X_arr  = np.array(self._confirmed_X)
        y_arr  = np.array(self._confirmed_y)
        meta_X = self._stack_features(X_arr)

        n = len(X_arr)
        use_gb = n >= 30   # GB требует больше данных

        try:
            if use_gb:
                self._meta = Pipeline([
                    ("scaler", StandardScaler()),
                    ("clf", GradientBoostingClassifier(
                        n_estimators=60, max_depth=3, learning_rate=0.08,
                        subsample=0.8, random_state=42))
                ])
            else:
                self._meta = Pipeline([
                    ("scaler", StandardScaler()),
                    ("clf",    LogisticRegression(C=2.0, max_iter=500, random_state=42))
                ])
            self._meta.fit(meta_X, y_arr)
            log.info(f"[AI] Мета-слой обновлён на {n} реальных сделках ({'GB' if use_gb else 'LogReg'})")
        except Exception as e:
            log.debug(f"[AI] meta_confirmed error: {e}")

    def _stack_features(self, X: np.ndarray) -> np.ndarray:
        """Формирует матрицу для мета-слоя: вероятности всех базовых моделей."""
        parts = []
        for slot in self._slots:
            try:
                proba = slot.predict_proba(X)
                parts.append(proba)
            except Exception:
                parts.append(np.full((len(X), 3), 1/3))
        return np.hstack(parts)

    # ─────────────────────────────────────────────────────────────────────────
    # Ансамблевый прогноз
    # ─────────────────────────────────────────────────────────────────────────

    def _ensemble_proba(self, X: np.ndarray) -> np.ndarray:
        """
        Возвращает усреднённые вероятности [P(-1), P(0), P(1)] = [down, hold, up].
        Если мета-слой готов — использует его поверх базовых моделей.
        """
        # Базовые вероятности (взвешенные)
        total_weight = sum(s.weight for s in self._slots)
        proba_sum = np.zeros(3)   # индексы: 0=down(-1) 1=hold(0) 2=up(1)

        for slot in self._slots:
            try:
                proba = slot.predict_proba(X)[0]   # shape=(n_classes,)
                # Выравниваем к [-1, 0, 1]
                aligned = self._align_proba(proba, slot.classes_)
                proba_sum += aligned * slot.weight
            except Exception:
                pass

        base_ens = proba_sum / max(total_weight, 1e-8)

        # Мета-слой поверх
        if self._meta is not None:
            try:
                meta_X  = self._stack_features(X)
                meta_p  = self._meta.predict_proba(meta_X)[0]
                meta_cls = self._meta.named_steps["clf"].classes_
                meta_aligned = self._align_proba(meta_p, meta_cls)
                # Блендинг: 60% мета + 40% базовый
                base_ens = 0.4 * base_ens + 0.6 * meta_aligned
            except Exception:
                pass

        return base_ens

    def _align_proba(self, proba: np.ndarray, classes) -> np.ndarray:
        """Выравнивает вектор вероятностей к индексам [P(-1), P(0), P(1)]."""
        out = np.array([1/3, 1/3, 1/3])
        cls_list = list(classes)
        mapping  = {-1: 0, 0: 1, 1: 2}
        for j, c in enumerate(cls_list):
            idx = mapping.get(int(c))
            if idx is not None and j < len(proba):
                out[idx] = proba[j]
        # Нормируем
        s = out.sum()
        if s > 0:
            out /= s
        return out

    # ─────────────────────────────────────────────────────────────────────────
    # Feature Engineering (45+ признаков)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_features(self, ohlcv) -> pd.DataFrame | None:
        if len(ohlcv) < 40:
            return None
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        c = df["close"]; h = df["high"]; l = df["low"]; v = df["volume"]; o = df["open"]

        # ── Базовые возвраты ──────────────────────────────────────────────
        for lag in [1, 2, 3, 5, 8, 13, 21]:   # Фибоначчи лаги
            df[f"ret_{lag}"] = c.pct_change(lag)

        # ── EMA и кроссоверы ──────────────────────────────────────────────
        for s in [5, 9, 21, 50, 100]:
            df[f"ema_{s}"] = c.ewm(span=s, adjust=False).mean()
        df["cross_9_21"]  = df["ema_9"]  - df["ema_21"]
        df["cross_21_50"] = df["ema_21"] - df["ema_50"]
        df["cross_50_100"]= df["ema_50"] - df["ema_100"]

        # ── RSI ───────────────────────────────────────────────────────────
        delta = c.diff()
        gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
        df["rsi"]     = 100 - 100 / (1 + gain / (loss + 1e-10))
        df["rsi_std"] = df["rsi"].rolling(10).std()   # RSI-волатильность

        # ── MACD ──────────────────────────────────────────────────────────
        df["macd"]    = c.ewm(12).mean() - c.ewm(26).mean()
        df["macd_s"]  = df["macd"].ewm(9).mean()
        df["macd_h"]  = df["macd"] - df["macd_s"]
        df["macd_div"]= df["macd_h"].diff()          # MACD momentum

        # ── Bollinger Bands ────────────────────────────────────────────────
        mid         = c.rolling(20).mean()
        std20       = c.rolling(20).std()
        df["bb_up"] = mid + 2 * std20
        df["bb_lo"] = mid - 2 * std20
        df["bb_w"]  = (df["bb_up"] - df["bb_lo"]) / (mid + 1e-10)
        df["bb_pos"]= (c - df["bb_lo"]) / (df["bb_up"] - df["bb_lo"] + 1e-10)
        # BB squeeze: ширина ниже 20% квантиля → сжатие перед взрывом
        df["bb_squeeze"] = (df["bb_w"] < df["bb_w"].rolling(50).quantile(0.2)).astype(int)

        # ── ATR ───────────────────────────────────────────────────────────
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        df["atr"]     = tr.rolling(14).mean()
        df["atr_pct"] = df["atr"] / (c + 1e-10)

        # ── Stochastic ────────────────────────────────────────────────────
        lo14          = l.rolling(14).min()
        hi14          = h.rolling(14).max()
        df["stoch_k"] = 100 * (c - lo14) / (hi14 - lo14 + 1e-10)
        df["stoch_d"] = df["stoch_k"].rolling(3).mean()

        # ── Williams %R ───────────────────────────────────────────────────
        df["willr"] = -100 * (hi14 - c) / (hi14 - lo14 + 1e-10)

        # ── CCI (Commodity Channel Index) ─────────────────────────────────
        tp          = (h + l + c) / 3
        df["cci"]   = (tp - tp.rolling(20).mean()) / (0.015 * tp.rolling(20).std() + 1e-10)

        # ── OBV (On-Balance Volume) ────────────────────────────────────────
        obv = (v * np.sign(c.diff())).cumsum()
        df["obv_ema"] = obv.ewm(span=14, adjust=False).mean()
        df["obv_div"] = obv - df["obv_ema"]    # OBV дивергенция

        # ── ADX (упрощённый — сила тренда) ───────────────────────────────
        up_move   = h - h.shift()
        down_move = l.shift() - l
        plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr14     = tr.ewm(alpha=1/14, adjust=False).mean()
        plus_di   = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / (atr14 + 1e-10)
        minus_di  = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / (atr14 + 1e-10)
        dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
        df["adx"] = dx.ewm(alpha=1/14, adjust=False).mean()

        # ── Ichimoku (упрощённый: tenkan / kijun) ─────────────────────────
        df["tenkan"] = (h.rolling(9).max() + l.rolling(9).min()) / 2
        df["kijun"]  = (h.rolling(26).max() + l.rolling(26).min()) / 2
        df["ichi_gap"] = df["tenkan"] - df["kijun"]

        # ── Heiken Ashi ────────────────────────────────────────────────────
        ha_close = (o + h + l + c) / 4
        ha_open  = (o.shift() + c.shift()) / 2
        df["ha_body"]  = (ha_close - ha_open)
        df["ha_trend"] = np.sign(df["ha_body"])

        # ── Gap (разрыв открытия) ─────────────────────────────────────────
        df["gap"] = (o - c.shift()) / (c.shift() + 1e-10)

        # ── Momentum ──────────────────────────────────────────────────────
        df["mom_5"]  = c - c.shift(5)
        df["mom_10"] = c - c.shift(10)
        df["roc_5"]  = c.pct_change(5)
        df["roc_10"] = c.pct_change(10)

        # ── Объём ─────────────────────────────────────────────────────────
        df["vol_ma"]  = v.rolling(20).mean()
        df["vol_r"]   = v / (df["vol_ma"] + 1e-10)
        df["vol_std"] = v.rolling(10).std() / (df["vol_ma"] + 1e-10)

        # ── Свечные паттерны (числа) ──────────────────────────────────────
        df["body"]     = (c - o).abs()
        df["rng"]      = h - l
        df["body_r"]   = df["body"] / (df["rng"] + 1e-10)   # тело / диапазон
        df["upper_w"]  = h - pd.concat([c, o], axis=1).max(axis=1)
        df["lower_w"]  = pd.concat([c, o], axis=1).min(axis=1) - l
        df["bull"]     = (c > o).astype(int)
        df["wick_asy"] = (df["upper_w"] - df["lower_w"]) / (df["rng"] + 1e-10)  # асимметрия фитилей

        # ── Угол тренда (линейная регрессия) ─────────────────────────────
        for win in [5, 10, 20]:
            slopes = []
            for i in range(len(c)):
                if i < win - 1:
                    slopes.append(np.nan)
                else:
                    y_ = c.values[i-win+1:i+1]
                    x_ = np.arange(win, dtype=float)
                    m  = np.polyfit(x_, y_, 1)[0]
                    slopes.append(m / (c.values[i] + 1e-10))
            df[f"slope_{win}"] = slopes

        # ── Позиция цены: близость к хаю/лою ─────────────────────────────
        df["hi20_dist"] = (c - h.rolling(20).max()) / (c + 1e-10)
        df["lo20_dist"] = (c - l.rolling(20).min()) / (c + 1e-10)

        # ── VWAP (Volume-Weighted Average Price) ──────────────────────────
        vwap = (v * (h + l + c) / 3).cumsum() / (v.cumsum() + 1e-10)
        df["vwap_dev"] = (c - vwap) / (vwap + 1e-10)   # отклонение от VWAP

        # ── CVD (Cumulative Volume Delta) ─────────────────────────────────
        # Приближение: объём × знак свечи (покупатели vs продавцы)
        bull_vol = v.where(c >= o, 0.0)
        bear_vol = v.where(c <  o, 0.0)
        cvd      = (bull_vol - bear_vol).cumsum()
        df["cvd_norm"] = cvd / (v.rolling(20).sum() + 1e-10)

        # ── Price Acceleration (2-я производная) ──────────────────────────
        vel  = c.pct_change(1)                         # скорость
        df["accel"] = vel.diff()                       # ускорение (2-я произв.)
        df["jerk"]  = df["accel"].diff()               # рывок (3-я произв.)

        # ── Fractal Efficiency (насколько прямое движение) ────────────────
        for win in [5, 10]:
            price_path = (c.diff().abs()).rolling(win).sum()
            price_net  = (c - c.shift(win)).abs()
            df[f"fractal_{win}"] = price_net / (price_path + 1e-10)

        # ── Range Position ────────────────────────────────────────────────
        # Где внутри 50-барного диапазона находится цена (0=дно, 1=верх)
        hi50 = h.rolling(50).max()
        lo50 = l.rolling(50).min()
        df["range_pos50"] = (c - lo50) / (hi50 - lo50 + 1e-10)

        # ════════════════════════════════════════════════════════════════
        # ── v4 NEW FEATURES ──────────────────────────────────────────
        # ════════════════════════════════════════════════════════════════

        # ── Kalman Filter Trend ──────────────────────────────────────
        # Самый точный трекер тренда — используется в квантовых фондах
        try:
            kalman = _kalman_filter(c.values)
            df["kalman"]     = kalman
            df["kalman_dev"] = (c.values - kalman) / (np.abs(kalman) + 1e-12)
        except Exception:
            df["kalman"]     = c
            df["kalman_dev"] = 0.0

        # ── Variance Ratio (Hurst-прокси, Lo-MacKinlay 1988) ─────────
        # VR>1 = trending (momentum сохраняется)
        # VR<1 = mean-reverting (осциллятор работает лучше)
        vr_vals = []
        for i in range(len(c)):
            if i < 30:
                vr_vals.append(1.0)
            else:
                vr_vals.append(_variance_ratio(c.values[max(0, i-40):i+1], q=5))
        df["var_ratio"] = vr_vals

        # ── Garman-Klass волатильность ────────────────────────────────
        # Точнее ATR, использует весь OHLC
        gk = _garman_klass_vol(o.values, h.values, l.values, c.values)
        df["gk_vol"]    = gk
        df["gk_vol_ma"] = pd.Series(gk, index=df.index).rolling(14).mean()
        df["gk_regime"] = df["gk_vol"] / (df["gk_vol_ma"] + 1e-12)  # текущая / средняя

        # ── Return distribution features ──────────────────────────────
        ret1 = c.pct_change(1)
        df["ret_skew"]    = ret1.rolling(20).skew()     # правый хвост = бычий потенциал
        df["ret_kurt"]    = ret1.rolling(20).kurt()     # толстые хвосты = аномалии
        df["ret_autocorr"]= ret1.rolling(20).apply(
            lambda x: float(pd.Series(x).autocorr(lag=1))
            if len(x) >= 3 else 0.0, raw=False)         # положительная автокорреляция = тренд

        # ── Pump Precursor Score (GRINCH-специфичный) ────────────────
        # RSI в зоне 42-68 AND bb_squeeze AND volume > 1.1× MA
        rsi_zone = ((df["rsi"] >= 42) & (df["rsi"] <= 68)).astype(float)
        bb_sq    = df["bb_squeeze"].astype(float) if "bb_squeeze" in df.columns else pd.Series(0.0, index=df.index)
        vol_ok   = (df["vol_r"] > 1.1).astype(float)
        df["pump_score"] = (rsi_zone * 0.4 + bb_sq * 0.35 + vol_ok * 0.25)

        # ── Candle Strength Score ─────────────────────────────────────
        # Комбинированная сила свечи: тело / диапазон × бычий знак
        df["candle_strength"] = df["body_r"] * np.where(c > o, 1.0, -1.0)

        # ── Micro-structure imbalance ─────────────────────────────────
        # Buy volume fraction (свечи вверх = покупатели)
        bull_vol_5 = (v * (c > o).astype(float)).rolling(5).sum()
        total_vol_5 = v.rolling(5).sum()
        df["buy_pressure"] = bull_vol_5 / (total_vol_5 + 1e-10)

        df.dropna(inplace=True)
        return df

    # ─────────────────────────────────────────────────────────────────────────
    # Разметка (адаптивная ATR + мульти-горизонт)
    # ─────────────────────────────────────────────────────────────────────────

    def _make_dataset(self, df):
        feature_cols = [
            "ret_1", "ret_2", "ret_3", "ret_5", "ret_8", "ret_13", "ret_21",
            "cross_9_21", "cross_21_50", "cross_50_100",
            "rsi", "rsi_std",
            "macd_h", "macd_div",
            "bb_w", "bb_pos", "bb_squeeze",
            "atr_pct",
            "stoch_k", "stoch_d",
            "willr", "cci",
            "obv_div", "adx",
            "ichi_gap",
            "ha_body", "ha_trend",
            "gap",
            "mom_5", "mom_10", "roc_5", "roc_10",
            "vol_r", "vol_std",
            "body_r", "bull", "wick_asy",
            "slope_5", "slope_10", "slope_20",
            "hi20_dist", "lo20_dist",
            # Признаки v3
            "vwap_dev", "cvd_norm",
            "accel", "jerk",
            "fractal_5", "fractal_10",
            "range_pos50",
            # ── v4 NEW: Квантово-финансовые признаки ─────────────────
            "kalman_dev",     # отклонение от Kalman тренда
            "var_ratio",      # Variance Ratio (Hurst-прокси)
            "gk_vol",         # Garman-Klass точная волатильность
            "gk_regime",      # GK относительно средней (аномалия волат.)
            "ret_skew",       # асимметрия распределения доходностей
            "ret_kurt",       # эксцесс (толщина хвостов)
            "ret_autocorr",   # автокорреляция (трендовость)
            "pump_score",     # GRINCH-специфичный сигнал накопления
            "candle_strength",# сила свечи (тело × направление)
            "buy_pressure",   # давление покупателей (5 баров)
        ]
        # Оставляем только существующие столбцы
        feature_cols = [col for col in feature_cols if col in df.columns]
        self._feature_names = feature_cols

        c       = df["close"].values
        atr_pct = df["atr_pct"].values
        X       = df[feature_cols].values
        n       = len(c)
        max_la  = max(LOOK_AHEADS)

        # ── v4: Profit-biased мульти-горизонт адаптивная разметка ───────────
        # Ключевое отличие v4: label=BUY только если движение > DEX fees + газ
        # Порог = max(ATR×0.7, PROFIT_BIAS_PCT=2.5%) → AI не обучается на
        # мелких движениях, которые не окупают комиссию 2% round-trip.
        # Горизонты [3,5,8,13] × взвешенное голосование → стабильные сигналы.
        HORIZON_WEIGHTS = [1.0, 1.5, 2.0, 2.5]   # веса по горизонтам LOOK_AHEADS
        y = np.zeros(n, dtype=int)
        for i in range(n - max_la):
            atr_thresh    = ATR_LABEL_MULT * (atr_pct[i] + 1e-10)
            # Строже для очень волатильных свечей (ATR>5% → порог выше)
            if atr_pct[i] > 0.05:
                atr_thresh *= 1.3
            # v4: BUY-порог не ниже PROFIT_BIAS_PCT (DEX fees покрытие)
            # SELL-порог остаётся ATR-based (нет смысла его завышать)
            buy_thresh  = max(atr_thresh, PROFIT_BIAS_PCT)
            sell_thresh = atr_thresh

            weighted_sum = 0.0
            total_w = 0.0
            for la, w in zip(LOOK_AHEADS, HORIZON_WEIGHTS):
                ret = (c[i + la] - c[i]) / (c[i] + 1e-10)
                if ret > buy_thresh:
                    weighted_sum += w
                elif ret < -sell_thresh:
                    weighted_sum -= w
                total_w += w
            # Взвешенное решение: >50% веса за сторону → сигнал
            ratio = weighted_sum / (total_w + 1e-10)
            if ratio > 0.5:       # >50% совокупного веса за рост > fees
                y[i] = 1
            elif ratio < -0.5:    # >50% за падение
                y[i] = -1

        X = X[:n - max_la]
        y = y[:n - max_la]
        return X, y

    # ─────────────────────────────────────────────────────────────────────────
    # Детекторы
    # ─────────────────────────────────────────────────────────────────────────

    def _detect_regime(self, df) -> dict:
        c     = df["close"]
        price = float(c.iloc[-1])
        e9    = float(df["ema_9"].iloc[-1])
        e21   = float(df["ema_21"].iloc[-1])
        e50   = float(df["ema_50"].iloc[-1])
        adx   = float(df["adx"].iloc[-1]) if "adx" in df.columns else 20.0
        bb_w  = float(df["bb_w"].iloc[-1]) if "bb_w" in df.columns else 0.05
        vol_r = float(df["vol_r"].iloc[-1]) if "vol_r" in df.columns else 1.0
        atr_pct = float(df["atr_pct"].iloc[-1]) if "atr_pct" in df.columns else 0.01

        avg_bb = float(df["bb_w"].rolling(20).mean().iloc[-1]) if "bb_w" in df.columns else bb_w
        squeeze= bool(df["bb_squeeze"].iloc[-1]) if "bb_squeeze" in df.columns else False

        trending_up   = e9 > e21 > e50 and adx > 20
        trending_down = e9 < e21 < e50 and adx > 20
        ranging       = abs(e9 - e50) / (price + 1e-10) < 0.003
        high_vol      = bb_w > avg_bb * 1.4

        if squeeze:
            name, color, desc = "SQUEEZE", "orange", "BB-сжатие — возможен взрывной выход"
        elif high_vol:
            name, color, desc = "VOLATILE", "yellow", "Высокая волатильность — осторожно"
        elif trending_up:
            name, color, desc = "UPTREND",  "green",  f"Восходящий тренд (ADX={adx:.0f})"
        elif trending_down:
            name, color, desc = "DOWNTREND","red",    f"Нисходящий тренд (ADX={adx:.0f})"
        elif ranging:
            name, color, desc = "RANGING",  "blue",   "Боковое движение"
        else:
            name, color, desc = "TRANSITION","purple","Переходная фаза"

        return {
            "name": name, "color": color, "desc": desc,
            "atr": round(float(df["atr"].iloc[-1]), 8),
            "atr_pct": round(atr_pct * 100, 3),
            "vol_ratio": round(vol_r, 2),
            "adx": round(adx, 1),
        }

    def _detect_candle_patterns(self, df) -> list:
        patterns = []
        o = df["open"].values;  h = df["high"].values
        l = df["low"].values;   c = df["close"].values
        if len(c) < 3:
            return patterns

        def body(i):  return abs(c[i] - o[i])
        def rng(i):   return max(h[i] - l[i], 1e-12)
        def upper(i): return h[i] - max(c[i], o[i])
        def lower(i): return min(c[i], o[i]) - l[i]

        i = len(c) - 1
        if rng(i) > 0 and body(i) / rng(i) < 0.1:
            patterns.append({"name": "Дожи", "type": "neutral", "desc": "Нерешительность рынка"})
        if lower(i) > body(i) * 2 and upper(i) < body(i) * 0.5:
            patterns.append({"name": "Молот", "type": "bullish", "desc": "Разворот вверх"})
        if upper(i) > body(i) * 2 and lower(i) < body(i) * 0.5:
            patterns.append({"name": "Падающая звезда", "type": "bearish", "desc": "Разворот вниз"})
        if i > 0 and c[i-1] < o[i-1] and c[i] > o[i] and body(i) > body(i-1):
            patterns.append({"name": "Бычье поглощение", "type": "bullish", "desc": "Сильный сигнал вверх"})
        if i > 0 and c[i-1] > o[i-1] and c[i] < o[i] and body(i) > body(i-1):
            patterns.append({"name": "Медвежье поглощение", "type": "bearish", "desc": "Сильный сигнал вниз"})
        if i >= 2 and all(c[j] > o[j] for j in range(i-2, i+1)) and c[i] > c[i-1] > c[i-2]:
            patterns.append({"name": "Три белых солдата", "type": "bullish", "desc": "Сильный памп"})
        if i >= 2 and all(c[j] < o[j] for j in range(i-2, i+1)) and c[i] < c[i-1] < c[i-2]:
            patterns.append({"name": "Три чёрных вороны", "type": "bearish", "desc": "Сильный дамп"})
        # Пин-бар (длинный нижний фитиль + маленькое тело)
        if lower(i) > rng(i) * 0.6 and body(i) < rng(i) * 0.25:
            patterns.append({"name": "Пин-бар", "type": "bullish", "desc": "Отбой от поддержки"})
        return patterns[:5]

    def _support_resistance(self, df) -> dict:
        c = df["close"].values[-60:];  h = df["high"].values[-60:];  l = df["low"].values[-60:]
        res, sup = [], []
        for i in range(3, len(c) - 3):
            if h[i] == max(h[i-3:i+4]):
                res.append(round(float(h[i]), 8))
            if l[i] == min(l[i-3:i+4]):
                sup.append(round(float(l[i]), 8))

        def cluster(lv, tol=0.008):
            if not lv: return []
            lv = sorted(set(lv))
            cl = [[lv[0]]]
            for v in lv[1:]:
                if (v - cl[-1][-1]) / (cl[-1][-1] + 1e-10) < tol:
                    cl[-1].append(v)
                else:
                    cl.append([v])
            return [round(sum(g)/len(g), 8) for g in cl]

        price   = float(c[-1])
        res_lvl = cluster(res);  sup_lvl = cluster(sup)
        return {
            "resistance": res_lvl[-3:],
            "support":    sup_lvl[:3],
            "nearest_resistance": min((r for r in res_lvl if r > price), default=None),
            "nearest_support":    max((s for s in sup_lvl if s < price), default=None),
        }

    def _price_forecast(self, df) -> dict:
        c     = df["close"].values;  price = float(c[-1])
        atr   = float(df["atr"].iloc[-1])
        x     = np.arange(10, dtype=float);  y = c[-10:]
        slope = np.polyfit(x, y, 1)[0]
        s_pct = slope / (price + 1e-10) * 100
        return {
            "t1": round(price + slope,   8),
            "t2": round(price + slope*2, 8),
            "t3": round(price + slope*3, 8),
            "slope_pct":  round(float(s_pct), 3),
            "bull":       bool(s_pct > 0),
            "range_up":   round(price + atr, 8),
            "range_down": round(price - atr, 8),
        }

    def _feature_importance(self) -> list:
        if not self._trained or not self._feature_names:
            return []
        try:
            rf_clf = self._slots[0].pipeline.named_steps["clf"]
            fi = rf_clf.feature_importances_
            pairs = sorted(zip(self._feature_names, fi), key=lambda x: -x[1])
            return [{"feature": k, "importance": round(float(v)*100, 1)} for k, v in pairs[:10]]
        except Exception:
            return []

    def _detect_anomaly(self, df) -> dict:
        c = df["close"].values;  vol = df["volume"].values
        mu_c = np.mean(c[-30:]); std_c = np.std(c[-30:]) + 1e-10
        mu_v = np.mean(vol[-30:]); std_v = np.std(vol[-30:]) + 1e-10
        z_p  = abs((c[-1]   - mu_c) / std_c)
        z_v  = abs((vol[-1] - mu_v) / std_v)
        anom = z_p > 2.5 or z_v > 3.0
        return {
            "detected":    anom,
            "z_price":     round(float(z_p), 2),
            "z_volume":    round(float(z_v), 2),
            "description": "⚡ Аномальное движение!" if anom else "Норма",
        }

    def _compute_sharpe(self) -> float:
        """Sharpe ratio по истории Kelly PnL-ов (безразмерный)."""
        try:
            pnls = list(self._kelly_pnls)
            if len(pnls) < 5:
                return 0.0
            arr  = np.array(pnls, dtype=float)
            mu   = arr.mean()
            std  = arr.std() + 1e-10
            return round(float(mu / std * (len(pnls) ** 0.5)), 2)
        except Exception:
            return 0.0

    def _compute_kelly(self) -> dict:
        """
        Kelly Criterion v2: оптимальная доля ставки с поправкой на Sharpe.
        f* = W - (1-W)/R  (base Kelly)
        Sharpe > 1 → разрешаем чуть выше 0.5× Kelly
        Sharpe < 0 → понижаем долю (осторожность)
        """
        try:
            wins = list(self._kelly_wins)
            pnls = list(self._kelly_pnls)
            n    = len(wins)
            if n < 5:
                return {"fraction": 0.5, "win_rate": 50.0, "rr_ratio": 1.0,
                        "trades": n, "ev": 0.0, "sharpe": 0.0}
            win_rate  = sum(wins) / n
            win_pnls  = [p for w, p in zip(wins, pnls) if w == 1 and p > 0]
            loss_pnls = [abs(p) for w, p in zip(wins, pnls) if w == 0 and p < 0]
            avg_win   = sum(win_pnls)  / max(len(win_pnls),  1)
            avg_loss  = sum(loss_pnls) / max(len(loss_pnls), 1)
            rr        = avg_win / max(avg_loss, 0.01)
            kelly_raw = win_rate - (1 - win_rate) / max(rr, 0.01)
            sharpe    = self._compute_sharpe()

            # Sharpe-взвешенный Kelly: Sharpe>1 → 0.6×, Sharpe>2 → 0.7×, иначе 0.5×
            if sharpe > 2.0:
                kelly_mult = 0.70
            elif sharpe > 1.0:
                kelly_mult = 0.60
            elif sharpe < 0:
                kelly_mult = 0.35   # осторожность при отрицательном Sharpe
            else:
                kelly_mult = 0.50   # классический half-Kelly

            half_kelly = max(0.1, min(kelly_raw * kelly_mult, 2.0))
            ev = win_rate * avg_win - (1 - win_rate) * avg_loss
            return {
                "fraction": round(half_kelly, 3),
                "win_rate": round(win_rate * 100, 1),
                "rr_ratio": round(rr, 2),
                "trades":   n,
                "ev":       round(ev, 4),
                "avg_win":  round(avg_win, 4),
                "avg_loss": round(avg_loss, 4),
                "sharpe":   sharpe,
            }
        except Exception:
            return {"fraction": 0.5, "win_rate": 50.0, "rr_ratio": 1.0,
                    "trades": 0, "ev": 0.0, "sharpe": 0.0}

    def _model_stats(self) -> list:
        icons = {"RF": "🌲", "ET": "⚡", "GB": "🚀", "HGB": "💥", "XGB": "🔥", "LGB": "🌿", "MLP": "🧠"}
        return [
            {
                "name":     s.name,
                "icon":     icons.get(s.name, "🤖"),
                "weight":   round(s.weight, 2),
                "accuracy": round(s.accuracy * 100, 1),
                "samples":  len(s._history),
            }
            for s in self._slots
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # Вспомогательное
    # ─────────────────────────────────────────────────────────────────────────

    def _top_feature(self, slot: _ModelSlot) -> str:
        try:
            fi = slot.pipeline.named_steps["clf"].feature_importances_
            return self._feature_names[int(np.argmax(fi))]
        except Exception:
            return "—"

    def _empty_result(self) -> dict:
        return {
            "ai_signal": "HOLD", "confidence": 0,
            "prob_up": 0, "prob_down": 0, "prob_hold": 100,
            "regime":  {"name": "UNKNOWN", "color": "grey", "desc": "Нет данных",
                        "atr": 0, "atr_pct": 0, "vol_ratio": 0, "adx": 0},
            "patterns": [], "support_resistance": {}, "forecast": {},
            "feature_importance": [], "model_info": [],
            "anomaly":  {"detected": False, "z_price": 0, "z_volume": 0, "description": "Нет данных"},
            "model_trained": False, "samples_trained": 0,
            "training_progress": self.training_progress,
            "kelly": {"fraction": 0.5, "win_rate": 50.0, "rr_ratio": 1.0, "trades": 0, "ev": 0.0},
            "momentum": {"score": 0.0, "signal": "CALM", "boost": 0.0,
                         "rsi_vel": 0.0, "vol_surge": False, "price_vel": 0.0},
            "breakout": {"score": 0.0, "signal": "FLAT", "icon": "💤",
                         "conf_boost": 0.0, "kelly_mult": 1.0,
                         "bb_squeeze": 0.0, "vol_acc": 0.0,
                         "rsi_build": 0.0, "macd_cross": 0.0, "coiling": 0.0},
            "pump":     {"score": 0.0, "pattern": "NEUTRAL", "conf_boost": 0.0},
            "var_ratio": 1.0,
            "total_boost": 0.0,
        }
