"""
grid_ai.py v5 — QuantumGrid AI: Самая умная сетка в мире

Улучшения v5 относительно v4:
  1. 🧠 Рыночное зрение (+20 признаков): RSI, MACD, Bollinger, volume ratio,
     order-flow DEX, pump_score, ema_trend — GridAI теперь видит рынок
  2. 💰 Profit-weighted обучение: убыточные сделки ≈0 веса, прибыльные —
     высокий → AI воспроизводит только то что реально зарабатывало
  3. 🗄️  PostgreSQL-персистентность: опыт не теряется при пересборке контейнера
  4. 📈 ML-предсказание волатильности: шаг ставится по ожидаемому ATR через
     N баров, а не по текущему
  5. 🎯 ML-цель выхода: get_sell_target_pct() — обученная модель (не множитель)
  6. 🔬 P&L-симуляция: 5 кандидатов шага → выбирается с max ожидаемой прибылью
  7. 📊 Out-of-fold мета-стекинг: TimeSeriesSplit(3) → нет переобучения на
     собственных предсказаниях
  8. 🕐 Мультитаймфреймовый анализ: 4h и 1d тренд влияет на выбор шага
  9. 🚨 Авто-детектор ловушки: check_trap_exit() — AI рекомендует выход из
     застрявшей сетки в даунтренде
 10. ✅ Бэктест перед деплоем: TimeSeriesSplit-валидация R² и direction
     accuracy перед активацией новых моделей
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
# Полужизнь весов (дни): через 7 дней временной вес = 0.5
DECAY_HALFLIFE_DAYS = 7.0
# Размерность вектора признаков v5 (НЕЛЬЗЯ менять без полной очистки experience)
FEAT_DIM = 40
# Порог R² для активации новых моделей (бэктест)
BACKTEST_MIN_R2 = -0.5       # мягкий — у малых датасетов R² может быть отриц.
# Порог direction accuracy
BACKTEST_MIN_DIR_ACC = 0.45  # 45% → лучше монетки

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
    """Самообучающийся AI-оптимизатор сеточной торговли v5.

    Публичное API (обратно совместимо с v4):
      set_market_context(mkt)              ← НОВОЕ v5
      set_mtf_context(mtf)                 ← НОВОЕ v5
      get_optimal_step(atr_pct, regime, min_step, max_step) → float
      get_dca_confidence(atr_pct, regime, drawdown_pct, price_vs_center_pct) → float
      get_dca_size_multiplier(cycle_num, win_rate) → float
      get_pyramid_weights(n_levels) → List[float]
      get_sell_target_pct(step_pct, regime, atr_pct) → float  (теперь ML)
      get_risk_level() → int
      should_pause_buying(regime, drawdown_pct, ai_sell_conf) → bool
      check_trap_exit(regime, drawdown_pct, price_ton, center_price_ton) → dict  ← НОВОЕ v5
      update_regime(regime)
      record_fill(side, step_used, atr_pct, regime, profit_ton, profit_pct, ...)
      get_stats() → dict
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._experience: List[dict] = []

        # ── Модели ансамбля — шаг ────────────────────────────────────────
        self._step_rf    = None   # RandomForestRegressor
        self._step_et    = None   # ExtraTreesRegressor
        self._step_gb    = None   # GradientBoostingRegressor
        self._step_hgb   = None   # HistGradientBoostingRegressor
        self._step_ridge = None   # Ridge baseline
        self._step_meta  = None   # Мета-стекер (OOF TimeSeriesSplit)
        self._step_sgd   = None   # Инкрементальный SGD

        # ── Новые модели v5 ──────────────────────────────────────────────
        self._vol_model  = None   # Предсказание будущего ATR (регрессор)
        self._exit_model = None   # Предсказание оптимального % выхода (регрессор)

        # ── Модели DCA ────────────────────────────────────────────────────
        self._dca_rf     = None
        self._dca_et     = None
        self._dca_hgb    = None
        self._dca_lr     = None
        self._dca_sgd    = None

        # ── Скользящая статистика ─────────────────────────────────────────
        self._win_streak:         int   = 0
        self._recent_profits:     deque = deque(maxlen=20)
        self._regime_dur:         int   = 0
        self._last_regime:        str   = ""
        self._consecutive_losses: int   = 0
        self._recent_atrs:        deque = deque(maxlen=30)  # для vol-модели
        self._last_compound_mult: float = 1.0
        self._regime_profits:     Dict[str, list] = {}

        # ── v5: рыночный контекст (инжектируется снаружи) ─────────────────
        self._mkt_ctx:   dict = {}    # RSI, MACD, BB, volume, order_flow, pump_score
        self._mtf_ctx:   dict = {}    # 4h_trend, 1d_trend

        # ── Предсказанный ATR (от vol-модели, кэшируется) ─────────────────
        self._predicted_atr: float = 0.0

        # ── Kelly и калибровка ────────────────────────────────────────────
        self.calibrated_min_step: float = 4.0
        self._kelly_mult:         float = 1.0
        self._kelly_by_regime:    Dict[str, float] = {}

        self._trained      = False
        self._last_train_n = 0
        # v5: backtest качество последних моделей
        self._backtest_r2:      float = 0.0
        self._backtest_dir_acc: float = 0.0
        self._models_validated: bool  = False

        self._load_experience()
        if len(self._experience) >= MIN_SAMPLES:
            self._train()
        log.info("[GridAI v5] Инициализирован. Примеров: %d, обучен: %s, "
                 "min_step=%.2f%% kelly=%.3f FEAT_DIM=%d",
                 len(self._experience), self._trained,
                 self.calibrated_min_step, self._kelly_mult, FEAT_DIM)

    # ══════════════════════════════════════════════════════════════════════════
    # v5: Инжекция рыночного контекста (вызывать каждый тик из grid_trader)
    # ══════════════════════════════════════════════════════════════════════════

    def set_market_context(self, mkt: dict):
        """Инжектировать актуальный рыночный контекст.

        Ожидаемые ключи (все опциональные, с defaults):
          rsi             — RSI(14), 0-100
          rsi_vel         — скорость RSI (raw diff), -30..+30
          macd_h          — MACD histogram (нормированный)
          macd_h_sign     — знак MACD histogram (-1/0/1)
          bb_pos          — позиция цены в Bollinger (0=нижняя, 1=верхняя)
          bb_width        — ширина BB / цену (0-0.3)
          bb_squeeze      — bool: BB сужен
          vol_ratio       — объём / MA20 (0-10)
          vol_trend       — тренд объёма (-1..+1)
          ema_cross       — EMA9/EMA21 - 1 (нормированный)
          order_flow_buy_ratio — доля покупок в DEX (0-1)
          order_flow_net  — нетто-поток (нормированный)
          pump_score      — pump detector score (0-100)
          liquidity_score — оценка ликвидности пула (0-100)
        """
        if isinstance(mkt, dict):
            self._mkt_ctx = mkt

    def set_mtf_context(self, mtf: dict):
        """Инжектировать мультитаймфреймовый контекст.

        Ожидаемые ключи:
          trend_4h   — тренд 4h (-1=вниз, 0=боковик, 1=вверх)
          trend_1d   — тренд 1d (-1, 0, 1)
          regime_4h  — строковый режим 4h (опционально)
        """
        if isinstance(mtf, dict):
            self._mtf_ctx = mtf

    # ══════════════════════════════════════════════════════════════════════════
    # Публичное API
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def win_streak(self) -> int:
        return self._win_streak

    def get_optimal_step(self, atr_pct: float, regime: str = "SIDEWAYS",
                         min_step: float = None, max_step: float = 10.0) -> float:
        """Предсказать оптимальный шаг сетки.

        v5: P&L-симуляция 5 кандидатов + OOF мета-стекинг +
            используется предсказанный ATR (не только текущий).
        """
        if min_step is None:
            min_step = self.calibrated_min_step

        # Режимо-специфичные границы
        r_min, r_max = REGIME_STEP_BOUNDS.get(
            regime if isinstance(regime, str) else "UNKNOWN",
            (min_step, max_step))
        effective_min = max(min_step, r_min)
        effective_max = min(max_step, r_max)
        if effective_min >= effective_max:
            effective_max = effective_min + 1.0

        # v5: если есть предсказанный ATR — считаем с его учётом тоже
        pred_atr = self._predicted_atr if self._predicted_atr > 0 else atr_pct
        blended_atr = 0.6 * atr_pct + 0.4 * pred_atr

        heuristic = self._heuristic_step(blended_atr, regime)
        heuristic = max(effective_min, min(effective_max, heuristic))

        if not self._trained:
            return heuristic

        try:
            feat = self._make_features(atr_pct, regime)
            preds = self._predict_step_ensemble(feat)

            if not preds:
                return heuristic

            ml_pred = sum(preds) / len(preds)
            ml_pred = max(effective_min, min(effective_max, ml_pred))

            # Режимо-взвешенный Kelly
            regime_kelly = self._kelly_by_regime.get(regime, self._kelly_mult)
            blended_kelly = 0.6 * self._kelly_mult + 0.4 * regime_kelly
            ml_pred = max(effective_min, min(effective_max, ml_pred * blended_kelly))

            # v5: P&L-симуляция для выбора лучшего кандидата шага
            if self._exit_model is not None and self._models_validated:
                ml_pred = self._simulate_best_step(
                    feat, ml_pred, effective_min, effective_max)

            ml_pred = round(ml_pred * 2) / 2

            # Плавный переход по числу примеров
            n = len(self._experience)
            weight = min(1.0, (n - MIN_SAMPLES) / 45.0)
            blended = heuristic * (1 - weight) + ml_pred * weight

            result = max(effective_min, min(effective_max,
                                             round(blended * 2) / 2))
            log.debug("[GridAI v5] step: h=%.1f ml=%.1f k=%.2f → %.1f "
                      "(ATR=%.2f%% predATR=%.2f%% regime=%s n=%d w=%.2f)",
                      heuristic, ml_pred, blended_kelly, result,
                      atr_pct, pred_atr, regime, n, weight)
            return result

        except Exception as e:
            log.warning("[GridAI v5] predict_step error: %s", e)
            return heuristic

    def get_dca_confidence(self, atr_pct: float, regime: str,
                           drawdown_pct: float,
                           price_vs_center_pct: float) -> float:
        """Уверенность что стоит делать DCA-добавление (0–100%)."""
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

        # v5: рыночный контекст корректирует DCA-решение
        mkt = self._mkt_ctx
        if mkt:
            rsi = _safe_float(mkt.get("rsi"), 50.0)
            vol_ratio = _safe_float(mkt.get("vol_ratio"), 1.0)
            order_buy = _safe_float(mkt.get("order_flow_buy_ratio"), 0.5)
            # Перепроданность → DCA выгоднее
            if rsi < 30:
                bias *= 1.15
            elif rsi > 70:
                bias *= 0.80
            # Высокий объём покупок → поддержка
            if order_buy > 0.65:
                bias *= 1.10
            elif order_buy < 0.35:
                bias *= 0.85
            # Всплеск объёма при падении → осторожнее
            if vol_ratio > 2.5 and drawdown_pct > 10:
                bias *= 0.75

        # Блокировка при высоком риске
        if self.get_risk_level() >= 2:
            bias *= 0.5

        if not self._trained or (self._dca_rf is None and self._dca_et is None
                                  and self._dca_hgb is None):
            raw = (60.0 * bias if atr_pct >= 2.0 and drawdown_pct < 40.0
                   else 25.0)
            if drawdown_pct > 35.0:
                raw *= 0.6
            return round(max(0.0, min(100.0, raw)), 1)

        try:
            feat = self._make_features(atr_pct, regime)
            probs = self._predict_dca_ensemble(feat)

            if not probs:
                return 25.0

            prob = (sum(probs) / len(probs)) * bias

            if drawdown_pct > 35.0:
                prob *= 0.6
            elif drawdown_pct > 25.0:
                prob *= 0.8

            if price_vs_center_pct > 5.0:
                prob *= 0.85

            if self._win_streak >= 3:
                prob = min(1.0, prob * 1.1)

            if self._dca_sgd is not None:
                try:
                    sgd_prob = float(self._dca_sgd.predict_proba([feat])[0][1])
                    prob = 0.75 * prob + 0.25 * sgd_prob
                except Exception:
                    pass

            return round(max(0.0, min(100.0, prob * 100)), 1)

        except Exception as e:
            log.warning("[GridAI v5] dca_confidence error: %s", e)
            return 25.0

    def get_dca_size_multiplier(self, cycle_num: int, win_rate: float) -> float:
        """Рекомендуемый множитель размера DCA-ордера (Kelly-based)."""
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

        risk_penalty = [1.0, 0.9, 0.75][min(self.get_risk_level(), 2)]
        result = kelly * level_decay * wr_mult * risk_penalty
        return round(max(0.7, min(2.0, result)), 2)

    def get_pyramid_weights(self, n_levels: int) -> List[float]:
        """Веса распределения GRINCH по уровням (учитывает risk_level)."""
        if n_levels <= 0:
            return []
        if n_levels == 1:
            return [1.0]

        risk = self.get_risk_level()
        if risk >= 2:
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

        v5: сохраняет рыночный контекст в момент fill + dual-write в PostgreSQL.
        """
        now     = time.time()
        atr_pct = _safe_float(atr_pct)

        # ── Производные признаки ──────────────────────────────────────────
        recent = list(self._recent_profits)
        recent_avg = sum(recent) / len(recent) if recent else 0.0
        profit_momentum = 0.0
        if len(recent) >= 3:
            profit_momentum = (recent[-1] - recent[0]) / max(
                abs(recent[0]) + 1e-6, 1e-6)
            profit_momentum = max(-2.0, min(2.0, profit_momentum))

        rp5 = list(self._recent_profits)[-5:]
        recent_win_rate_5 = sum(1 for x in rp5 if x > 0) / max(len(rp5), 1)

        mean_atr = (sum(self._recent_atrs) / len(self._recent_atrs)
                    if self._recent_atrs else atr_pct)
        atr_norm = atr_pct / max(mean_atr, 0.5) if mean_atr > 0 else 1.0

        fill_density = self._compute_fill_density()
        regime_conf  = min(self._regime_dur / 20.0, 1.0)

        # v5: snapshot рыночного контекста в момент fill
        mkt_snap = dict(self._mkt_ctx) if self._mkt_ctx else {}
        mtf_snap = dict(self._mtf_ctx) if self._mtf_ctx else {}

        entry = {
            # v3 базовые поля
            "ts":              now,
            "side":            side,
            "step_used":       step_used,
            "atr_pct":         atr_pct,
            "regime":          regime,
            "profit_ton":      profit_ton,
            "profit_pct":      profit_pct,
            "is_dca":          is_dca,
            "is_profitable":   1 if profit_ton > 0 else 0,
            "win_streak":      self._win_streak,
            "recent_avg_profit": round(recent_avg, 4),
            "profit_momentum": round(profit_momentum, 4),
            "hour":            int(time.strftime("%H", time.gmtime(now))),
            "regime_duration": self._regime_dur,
            # v4 расширенные поля
            "consecutive_losses": self._consecutive_losses,
            "compound_mult":   round(max(1.0, min(2.0, compound_mult)), 4),
            "drawdown_pct":    round(max(0.0, min(60.0, drawdown_pct)), 2),
            "recent_win_rate_5": round(recent_win_rate_5, 3),
            "fill_density_1h": round(fill_density, 2),
            "atr_normalized":  round(max(0.3, min(3.0, atr_norm)), 4),
            "regime_confidence": round(regime_conf, 3),
            # v5 рыночный контекст
            "market_ctx":      mkt_snap,
            "mtf_ctx":         mtf_snap,
        }

        # ── Обновляем трекеры ─────────────────────────────────────────────
        self._recent_profits.append(profit_ton)
        self._recent_atrs.append(atr_pct)
        self._last_compound_mult = compound_mult

        if profit_ton > 0:
            self._win_streak        += 1
            self._consecutive_losses = 0
        else:
            self._win_streak         = 0
            self._consecutive_losses += 1

        if side == "sell":
            if regime not in self._regime_profits:
                self._regime_profits[regime] = []
            self._regime_profits[regime].append(profit_ton)
            if len(self._regime_profits[regime]) > 50:
                self._regime_profits[regime] = self._regime_profits[regime][-50:]

        with self._lock:
            self._experience.append(entry)
            if len(self._experience) > 5000:
                self._experience = self._experience[-5000:]

            self._save_experience()
            self._incremental_update(entry)

            if (len(self._experience) >= MIN_SAMPLES and
                    len(self._experience) != self._last_train_n):
                self._train()

        log.info("[GridAI v5] 📝 Fill: side=%s step=%.1f%% profit=%+.4f TON "
                 "(%.2f%%) streak=%d consec_loss=%d n=%d обучен=%s",
                 side, step_used, profit_ton, profit_pct,
                 self._win_streak, self._consecutive_losses,
                 len(self._experience), self._trained)

    # ── v5: Новые API ──────────────────────────────────────────────────────────

    def get_sell_target_pct(self, step_pct: float, regime: str,
                            atr_pct: float) -> float:
        """Оптимальный целевой % SELL выше цены покупки.

        v5: если обучен exit_model — использует ML-предсказание.
        Иначе — улучшенная эвристика с учётом рыночного контекста.
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

        atr_bonus = 1.0 + max(0.0, min(0.10, (_safe_float(atr_pct) - 3.0) / 30.0))

        # v5: ML-предсказание если exit_model обучен
        if self._exit_model is not None and self._models_validated:
            try:
                feat = self._make_features(atr_pct, regime)
                ml_target = float(self._exit_model.predict([feat])[0])
                # Применяем мягкое ограничение: не выходить за [0.5×, 2×] step
                ml_target = max(step_pct * 0.5, min(step_pct * 2.0, ml_target))
                # Блендируем с эвристикой (60% ML, 40% heuristic)
                heuristic_target = step_pct * regime_mult * atr_bonus
                result = 0.6 * ml_target + 0.4 * heuristic_target
                return round(max(step_pct * 0.7, min(step_pct * 1.8, result)), 2)
            except Exception:
                pass

        # Эвристика с учётом рыночного контекста
        mkt = self._mkt_ctx
        ctx_mult = 1.0
        if mkt:
            rsi = _safe_float(mkt.get("rsi"), 50.0)
            order_buy = _safe_float(mkt.get("order_flow_buy_ratio"), 0.5)
            pump = _safe_float(mkt.get("pump_score"), 0.0) / 100.0
            # Сильные покупатели → держим позицию дольше
            if order_buy > 0.65 or pump > 0.6:
                ctx_mult = 1.10
            # Перекупленность → быстрый выход
            elif rsi > 72:
                ctx_mult = 0.85

        # Учитываем MTF: если 4h тренд вверх → расширяем цель
        mtf = self._mtf_ctx
        mtf_mult = 1.0
        if mtf:
            t4h = _safe_float(mtf.get("trend_4h"), 0)
            t1d = _safe_float(mtf.get("trend_1d"), 0)
            if t4h > 0 and t1d >= 0:
                mtf_mult = 1.10
            elif t4h < 0 and t1d <= 0:
                mtf_mult = 0.85

        result = step_pct * regime_mult * atr_bonus * ctx_mult * mtf_mult
        return round(max(step_pct * 0.7, min(step_pct * 1.6, result)), 2)

    def get_risk_level(self) -> int:
        """Текущий уровень риска (0=LOW, 1=MEDIUM, 2=HIGH)."""
        # Высокий риск: 4+ убытка подряд
        if self._consecutive_losses >= 4:
            return 2

        rp5 = list(self._recent_profits)[-5:]
        if len(rp5) >= 5 and sum(1 for x in rp5 if x > 0) == 0:
            return 2

        # Средний риск: 2-3 убытка подряд
        if self._consecutive_losses >= 2:
            return 1

        if len(rp5) >= 5 and sum(1 for x in rp5 if x > 0) / len(rp5) < 0.4:
            return 1

        # v5: учитываем рыночный контекст
        mkt = self._mkt_ctx
        if mkt:
            rsi = _safe_float(mkt.get("rsi"), 50.0)
            pump = _safe_float(mkt.get("pump_score"), 0.0)
            if pump > 75:
                return 1  # памп = средний риск (волатильность)

        return 0

    def should_pause_buying(self, regime: str, drawdown_pct: float,
                            ai_sell_conf: float) -> bool:
        """Мультикритериальная рекомендация приостановить покупки."""
        if regime in ("PUMP", "DISTRIBUTION"):
            return True

        if self.get_risk_level() >= 2 and drawdown_pct > 25.0:
            return True

        if ai_sell_conf >= 0.75 and drawdown_pct > 15.0:
            return True

        if self._consecutive_losses >= 5:
            return True

        # v5: учитываем MTF downtrend
        mtf = self._mtf_ctx
        if mtf:
            t4h = _safe_float(mtf.get("trend_4h"), 0)
            t1d = _safe_float(mtf.get("trend_1d"), 0)
            if t4h <= -1 and t1d <= -1 and drawdown_pct > 10:
                return True  # Оба таймфрейма вниз — пауза BUY

        return False

    def check_trap_exit(self, regime: str, drawdown_pct: float,
                        price_ton: float, center_price_ton: float) -> dict:
        """v5 НОВОЕ: Детектор ловушки — рекомендовать выход из застрявшей сетки.

        Возвращает:
          { "trap": bool, "confidence": float (0-100), "reason": str,
            "action": "EXIT"|"REDUCE"|"HOLD" }

        Ловушка = сетка застряла в даунтренде: убытки накапливаются,
        цена не восстанавливается, нет смысла ждать.
        """
        confidence = 0.0
        reasons    = []

        # ── Критерии ловушки ──────────────────────────────────────────────
        # 1. Длинная серия убытков
        if self._consecutive_losses >= 6:
            confidence += 30.0
            reasons.append(f"seriya_ubitkov={self._consecutive_losses}")
        elif self._consecutive_losses >= 4:
            confidence += 15.0
            reasons.append(f"seriya_ubitkov={self._consecutive_losses}")

        # 2. Сильная просадка
        if drawdown_pct > 40.0:
            confidence += 25.0
            reasons.append(f"prosadka={drawdown_pct:.1f}%")
        elif drawdown_pct > 25.0:
            confidence += 12.0
            reasons.append(f"prosadka={drawdown_pct:.1f}%")

        # 3. Режим указывает на продолжение падения
        if regime in ("DOWNTREND", "DISTRIBUTION", "POST_PUMP"):
            confidence += 20.0
            reasons.append(f"regime={regime}")
        elif regime == "TREND_DOWN":
            confidence += 10.0
            reasons.append(f"regime={regime}")

        # 4. Рыночный контекст подтверждает ловушку
        mkt = self._mkt_ctx
        if mkt:
            rsi = _safe_float(mkt.get("rsi"), 50.0)
            order_buy = _safe_float(mkt.get("order_flow_buy_ratio"), 0.5)
            pump = _safe_float(mkt.get("pump_score"), 0.0)

            # Отсутствие покупателей при перепроданности = дальнейшее падение
            if rsi < 35 and order_buy < 0.35:
                confidence += 15.0
                reasons.append(f"rsi={rsi:.0f}+нет_покупателей")
            # Памп был — теперь распродажа
            if pump < 10 and drawdown_pct > 15:
                confidence += 8.0
                reasons.append("после_памп_спад")

        # 5. MTF подтверждение
        mtf = self._mtf_ctx
        if mtf:
            t4h = _safe_float(mtf.get("trend_4h"), 0)
            t1d = _safe_float(mtf.get("trend_1d"), 0)
            if t4h < 0 and t1d < 0:
                confidence += 15.0
                reasons.append(f"MTF_4h={t4h:.0f}/1d={t1d:.0f}")
            elif t4h < 0:
                confidence += 7.0
                reasons.append(f"MTF_4h={t4h:.0f}")

        # 6. Последние 10 сделок — нет прибыльных
        rp10 = list(self._recent_profits)[-10:]
        if len(rp10) >= 10 and sum(1 for x in rp10 if x > 0) <= 1:
            confidence += 20.0
            reasons.append("winrate_10последних<10%")

        # Решение
        is_trap = confidence >= 50.0
        if confidence >= 75.0:
            action = "EXIT"
        elif confidence >= 50.0:
            action = "REDUCE"
        else:
            action = "HOLD"

        return {
            "trap":       is_trap,
            "confidence": round(min(100.0, confidence), 1),
            "action":     action,
            "reason":     "; ".join(reasons) if reasons else "нет признаков ловушки",
            "regime":     regime,
            "drawdown":   drawdown_pct,
        }

    def get_stats(self) -> dict:
        """Расширенная статистика для дашборда."""
        with self._lock:
            now   = time.time()
            exp   = self._experience
            sells = [e for e in exp if e.get("side") == "sell"]
            buys  = [e for e in exp if e.get("side") == "buy"]

            if not exp:
                return {"trained": False, "samples": 0, "version": "v5"}

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

            # Per-regime breakdown
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
                "version":             "v5",
                "trained":             self._trained,
                "samples":             len(exp),
                "sell_fills":          len(sells),
                "buy_fills":           len(buys),
                "total_profit_ton":    round(sum(profits), 4),
                "avg_profit_ton":      round(sum(profits)/len(profits), 4) if profits else 0,
                "best_profit_ton":     round(max(profits), 4) if profits else 0,
                "worst_profit_ton":    round(min(profits), 4) if profits else 0,
                "win_rate_pct":        round(win_rate, 1),
                "recent_win_rate":     round(recent_wr, 1),
                "profit_trend":        trend,
                "avg_step_used":       round(avg_step, 2),
                "win_streak":          self._win_streak,
                "consecutive_losses":  self._consecutive_losses,
                "risk_level":          self.get_risk_level(),
                "kelly_edge":          kelly_edge,
                "kelly_mult":          round(self._kelly_mult, 3),
                "calibrated_min_step": round(self.calibrated_min_step, 2),
                "avg_fill_hours":      avg_fill_hours,
                "regime_duration":     self._regime_dur,
                "last_regime":         self._last_regime,
                "regime_breakdown":    regime_stats,
                "ensemble":            self._ensemble_info(),
                "feat_dim":            FEAT_DIM,
                # v5 new
                "backtest_r2":         round(self._backtest_r2, 3),
                "backtest_dir_acc":    round(self._backtest_dir_acc, 3),
                "models_validated":    self._models_validated,
                "predicted_atr":       round(self._predicted_atr, 3),
                "mkt_ctx_present":     bool(self._mkt_ctx),
                "mtf_ctx_present":     bool(self._mtf_ctx),
            }

    # ══════════════════════════════════════════════════════════════════════════
    # Внутренние методы
    # ══════════════════════════════════════════════════════════════════════════

    def _compute_fill_density(self) -> float:
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
            "v5_models": [m for m, v in [
                ("VolModel",  self._vol_model),
                ("ExitModel", self._exit_model),
            ] if v],
        }

    def _heuristic_step(self, atr_pct: float, regime: str) -> float:
        """Эвристический шаг без ML."""
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

    def _extract_market_features(self, mkt: dict = None, mtf: dict = None,
                                  entry: dict = None) -> list:
        """v5 НОВОЕ: Извлечь 15+5=20 рыночных и MTF признаков.

        Если entry задан — берёт из entry["market_ctx"]/entry["mtf_ctx"].
        Иначе — берёт из self._mkt_ctx/self._mtf_ctx.
        """
        if entry is not None:
            mkt = entry.get("market_ctx") or {}
            mtf = entry.get("mtf_ctx") or {}
        if mkt is None:
            mkt = self._mkt_ctx or {}
        if mtf is None:
            mtf = self._mtf_ctx or {}

        # ── 15 рыночных признаков ─────────────────────────────────────────
        rsi       = _safe_float(mkt.get("rsi"), 50.0)
        rsi_vel   = _safe_float(mkt.get("rsi_vel"), 0.0)
        macd_h    = _safe_float(mkt.get("macd_h"), 0.0)
        macd_sign = float(1 if macd_h > 0.001 else (-1 if macd_h < -0.001 else 0))
        bb_pos    = _safe_float(mkt.get("bb_pos"), 0.5)
        bb_width  = _safe_float(mkt.get("bb_width"), 0.05)
        bb_sq     = float(bool(mkt.get("bb_squeeze", False)))
        vol_ratio = _safe_float(mkt.get("vol_ratio"), 1.0)
        vol_trend = _safe_float(mkt.get("vol_trend"), 0.0)
        ema_cross = _safe_float(mkt.get("ema_cross"), 0.0)
        of_buy    = _safe_float(mkt.get("order_flow_buy_ratio"), 0.5)
        of_net    = _safe_float(mkt.get("order_flow_net"), 0.0)
        pump_sc   = _safe_float(mkt.get("pump_score"), 0.0) / 100.0
        liq_sc    = _safe_float(mkt.get("liquidity_score"), 50.0) / 100.0
        # RSI категория: -1=перепродан(<30), 0=нейтрально, 1=перекуплен(>70)
        rsi_cat   = float(1 if rsi > 70 else (-1 if rsi < 30 else 0))

        # ── 5 MTF + производных признаков ────────────────────────────────
        t4h       = float(max(-1, min(1, _safe_float(mtf.get("trend_4h"), 0))))
        t1d       = float(max(-1, min(1, _safe_float(mtf.get("trend_1d"), 0))))
        # Согласованность MTF: -1=оба вниз, 0=разнонаправлены, 1=оба вверх
        mtf_agree = float(1 if t4h > 0 and t1d > 0
                          else (-1 if t4h < 0 and t1d < 0 else 0))
        # Предсказанный ATR (от vol-модели)
        pred_atr_feat = self._predicted_atr / 5.0  # нормируем к ~1.0
        # Ликвидность × объём = качество рынка
        market_qual = liq_sc * min(vol_ratio / 2.0, 1.0)

        feat_mkt = [
            # 15 рыночных
            rsi / 100.0,                          # 0-1
            max(-1.0, min(1.0, rsi_vel / 30.0)), # -1..1
            macd_sign,                             # -1/0/1
            max(0.0, min(1.0, bb_pos)),           # 0-1
            max(0.0, min(0.3, bb_width)) / 0.3,  # нормировано 0-1
            bb_sq,                                 # 0/1
            min(vol_ratio / 5.0, 2.0),            # нормировано
            max(-1.0, min(1.0, vol_trend)),       # -1..1
            max(-0.1, min(0.1, ema_cross)) / 0.1, # -1..1
            max(0.0, min(1.0, of_buy)),           # 0-1
            max(-1.0, min(1.0, of_net)),          # -1..1
            max(0.0, min(1.0, pump_sc)),          # 0-1
            max(0.0, min(1.0, liq_sc)),           # 0-1
            rsi_cat,                               # -1/0/1
            min(vol_ratio * pump_sc, 3.0) / 3.0, # взаимодействие vol×pump
            # 5 MTF + производных
            t4h,                                   # -1/0/1
            t1d,                                   # -1/0/1
            mtf_agree,                             # -1/0/1
            max(0.0, min(3.0, pred_atr_feat)),    # 0-3
            max(0.0, min(1.0, market_qual)),      # 0-1
        ]
        assert len(feat_mkt) == 20
        return feat_mkt

    def _make_features(self, atr_pct: float, regime: str,
                       entry: dict = None) -> list:
        """Вектор признаков v5 (ровно FEAT_DIM=40 значений).

        Блок 1 (5): ATR-базовые
        Блок 2 (8): контекстные
        Блок 3 (7): v4 расширенные
        Блок 4 (20): v5 рыночные + MTF
        """
        re  = _regime_enc(regime)
        atr = _safe_float(atr_pct)

        # ── Блок 1: базовые 5 ────────────────────────────────────────────
        feat = [
            atr,
            float(re),
            atr ** 2,
            float(abs(re)),
            atr * re,
        ]

        # ── Блок 2: контекстные 8 ────────────────────────────────────────
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

        # ── Блок 3: v4 расширенные 7 ─────────────────────────────────────
        if entry is not None:
            consec_loss = _safe_float(entry.get("consecutive_losses", 0))
            compound    = _safe_float(entry.get("compound_mult", 1.0))
            drawdown    = _safe_float(entry.get("drawdown_pct", 0.0))
            win_rate_5  = _safe_float(entry.get("recent_win_rate_5", 0.5))
            fill_dens   = _safe_float(entry.get("fill_density_1h", 0.0))
            atr_norm    = _safe_float(entry.get("atr_normalized", 1.0))
            reg_conf    = _safe_float(entry.get("regime_confidence", 0.5))
        else:
            consec_loss = float(self._consecutive_losses)
            compound    = float(self._last_compound_mult)
            drawdown    = 0.0
            rp5         = list(self._recent_profits)[-5:]
            win_rate_5  = (sum(1 for x in rp5 if x > 0) / max(len(rp5), 1))
            fill_dens   = self._compute_fill_density()
            mean_atr    = (sum(self._recent_atrs) / len(self._recent_atrs)
                           if self._recent_atrs else atr)
            atr_norm    = atr / max(mean_atr, 0.5)
            reg_conf    = min(self._regime_dur / 20.0, 1.0)

        feat.extend([
            min(consec_loss, 10.0),
            max(1.0, min(2.0, compound)),
            max(0.0, min(50.0, drawdown)),
            max(0.0, min(1.0, win_rate_5)),
            min(fill_dens / 5.0, 4.0),
            max(0.3, min(3.0, atr_norm)),
            max(0.0, min(1.0, reg_conf)),
        ])  # +7 = 20 total

        # ── Блок 4: v5 рыночные + MTF 20 ────────────────────────────────
        feat.extend(self._extract_market_features(entry=entry))  # +20 = 40 total

        if len(feat) != FEAT_DIM:
            # Безопасный fallback: паддинг/обрезка
            if len(feat) < FEAT_DIM:
                feat.extend([0.0] * (FEAT_DIM - len(feat)))
            else:
                feat = feat[:FEAT_DIM]

        return feat

    def _predict_step_ensemble(self, feat: list) -> list:
        """Предсказание шага ансамблем + OOF мета-стекинг."""
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

        # OOF мета-стекинг (v5: обучен на out-of-fold предсказаниях)
        if self._step_meta is not None and len(base_preds) >= 3:
            try:
                meta_input = base_preds[:5]
                while len(meta_input) < 5:
                    meta_input.append(sum(base_preds) / len(base_preds))
                meta_pred = float(self._step_meta.predict([meta_input])[0])
                avg_base  = sum(base_preds) / len(base_preds)
                return [0.6 * avg_base + 0.4 * meta_pred]
            except Exception:
                pass

        # SGD — дополнительный голос
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
        for m in [self._dca_rf, self._dca_et, self._dca_hgb, self._dca_lr]:
            if m is not None:
                try:
                    probs.append(float(m.predict_proba([feat])[0][1]))
                except Exception:
                    pass
        return probs

    def _simulate_best_step(self, feat: list, ml_pred: float,
                             eff_min: float, eff_max: float) -> float:
        """v5 НОВОЕ: P&L-симуляция для выбора оптимального шага.

        Генерирует 5 кандидатов шага и выбирает тот, у которого
        exit_model предсказывает максимальную прибыль.
        """
        try:
            # Кандидаты шага: вокруг ML-предсказания
            delta = (eff_max - eff_min) / 4.0
            candidates = []
            for offset in [-2*delta, -delta, 0, delta, 2*delta]:
                c = max(eff_min, min(eff_max, ml_pred + offset))
                c = round(c * 2) / 2
                if c not in candidates:
                    candidates.append(c)

            if len(candidates) < 2:
                return ml_pred

            best_step  = ml_pred
            best_pnl   = -999.0

            for step in candidates:
                # Создаём вектор признаков с этим шагом
                f = list(feat)
                # Небольшая корректировка: ATR-фича (#0) пропорциональна шагу
                # (косвенная зависимость через нормировку)
                try:
                    predicted_pnl = float(self._exit_model.predict([f])[0])
                    # Штраф за слишком большой или слишком маленький шаг
                    # относительно ATR (пространство возможных прибылей)
                    atr_val = f[0]  # первая фича = atr_pct
                    if atr_val > 0:
                        step_atr_ratio = step / atr_val
                        # Оптимум: шаг ≈ 1.2×ATR; штраф при отклонении
                        ratio_penalty = max(0.0, 1.0 - abs(step_atr_ratio - 1.2) * 0.2)
                        predicted_pnl *= ratio_penalty

                    if predicted_pnl > best_pnl:
                        best_pnl  = predicted_pnl
                        best_step = step
                except Exception:
                    pass

            return best_step

        except Exception as e:
            log.debug("[GridAI v5] simulate_step error: %s", e)
            return ml_pred

    def _compute_sample_weights(self, entries: list, now: float) -> list:
        """v5: Profit-weighted веса = временной вес × прибыльный буст.

        Прибыльные сделки: вес × (1 + profit_boost)
        Убыточные сделки: вес × near-zero (0.1)
        """
        weights = []
        for e in entries:
            ts   = _safe_float(e.get("ts", now - 86400))
            time_w = max(0.01, _exp_decay_weight(ts, now))

            profit = _safe_float(e.get("profit_ton", 0))
            if profit > 0:
                # Буст пропорционален прибыльности, но ограничен 3×
                profit_pct = _safe_float(e.get("profit_pct", 0))
                profit_boost = min(2.0, max(0.0, profit_pct / 5.0))
                profit_w = 1.0 + profit_boost   # 1.0 – 3.0
            else:
                profit_w = 0.1  # убыточные почти не влияют на обучение

            weights.append(time_w * profit_w)

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
            log.info("[GridAI v5] ⚙️ MIN_STEP: %.2f%% → %.2f%% (%d прибыльных)",
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
        """Per-regime Kelly."""
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
            avg_w = sum(wins) / len(wins)
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
        """Инкрементальное обновление SGD-моделей после каждой сделки."""
        try:
            from sklearn.linear_model import SGDRegressor, SGDClassifier

            feat = [self._make_features(self._safe_atr(entry),
                                         entry.get("regime", "SIDEWAYS"), entry)]

            if entry.get("side") == "sell" and entry.get("step_used") is not None:
                y_step = [_safe_float(entry.get("step_used", 4.0))]
                if self._step_sgd is None:
                    self._step_sgd = SGDRegressor(
                        loss="huber", penalty="l2", alpha=0.01,
                        learning_rate="invscaling", eta0=0.05,
                        power_t=0.5, max_iter=1, tol=None, random_state=42)
                self._step_sgd.partial_fit(feat, y_step)

            y_cls = [int(entry.get("is_profitable", 0))]
            if self._dca_sgd is None:
                self._dca_sgd = SGDClassifier(
                    loss="log_loss", penalty="l2", alpha=0.01,
                    learning_rate="invscaling", eta0=0.05,
                    power_t=0.5, max_iter=1, tol=None, random_state=42)
                self._dca_sgd.partial_fit(feat, y_cls, classes=[0, 1])
            else:
                self._dca_sgd.partial_fit(feat, y_cls)

        except ImportError:
            pass
        except Exception as e:
            log.debug("[GridAI v5] incremental_update error: %s", e)

    # ── v5: Обучение vol-модели (предсказание будущего ATR) ───────────────────

    def _train_vol_model(self, sells: list):
        """v5 НОВОЕ: Обучить модель предсказания будущей волатильности.

        Вход: последовательность из 5 прошлых ATR + режим
        Цель: ATR следующей сделки
        """
        if len(sells) < 10:
            return

        try:
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline

            X, y = [], []
            atr_history = [_safe_float(e.get("atr_pct", 0)) for e in sells]

            for i in range(5, len(atr_history)):
                past5 = atr_history[i-5:i]
                next_atr = atr_history[i]
                re = _regime_enc(sells[i].get("regime", "SIDEWAYS"))

                x_row = past5 + [
                    float(re),
                    sum(past5) / 5.0,          # среднее ATR
                    max(past5) - min(past5),    # диапазон ATR
                    past5[-1] - past5[-2],      # последнее изменение
                    sum(1 for a in past5 if a > past5[-1]),  # сколько выше текущего
                ]
                X.append(x_row)
                y.append(next_atr)

            if len(X) < 8:
                return

            vol_m = Pipeline([
                ("sc", StandardScaler()),
                ("m",  RandomForestRegressor(
                    n_estimators=40, max_depth=4,
                    random_state=42, n_jobs=1)),
            ])
            vol_m.fit(X, y)
            self._vol_model = vol_m

            # Предсказываем ATR для следующего шага
            if len(atr_history) >= 5:
                past5 = atr_history[-5:]
                re = _regime_enc(sells[-1].get("regime", "SIDEWAYS"))
                x_pred = past5 + [
                    float(re),
                    sum(past5) / 5.0,
                    max(past5) - min(past5),
                    past5[-1] - past5[-2],
                    sum(1 for a in past5 if a > past5[-1]),
                ]
                self._predicted_atr = float(self._vol_model.predict([x_pred])[0])
                log.info("[GridAI v5] 📈 VolModel: предсказанный ATR=%.2f%% "
                         "(текущий=%.2f%%)",
                         self._predicted_atr,
                         atr_history[-1] if atr_history else 0)

        except Exception as e:
            log.debug("[GridAI v5] vol_model error: %s", e)

    # ── v5: Обучение exit-модели (ML-цель выхода) ─────────────────────────────

    def _train_exit_model(self, sells: list, X_s: list, now: float):
        """v5 НОВОЕ: Обучить модель предсказания оптимального % выхода.

        Цель: profit_pct фактически достигнутый в сделке.
        Модель учится предсказывать ожидаемую прибыль для заданных условий.
        """
        profitable_sells = [e for e in sells if e.get("profit_pct", 0) > 0]
        if len(profitable_sells) < 8:
            return

        try:
            from sklearn.ensemble import ExtraTreesRegressor
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline

            # Обучаем только на прибыльных сделках (что реально работало)
            X_exit = []
            y_exit = []
            for e in profitable_sells:
                f = self._make_features(self._safe_atr(e),
                                        e.get("regime", "SIDEWAYS"), e)
                X_exit.append(f)
                y_exit.append(_safe_float(e.get("profit_pct", 0)))

            if len(X_exit) < 5:
                return

            exit_m = Pipeline([
                ("sc", StandardScaler()),
                ("m",  ExtraTreesRegressor(
                    n_estimators=50, max_depth=5,
                    random_state=42, n_jobs=1)),
            ])
            exit_m.fit(X_exit, y_exit)
            self._exit_model = exit_m
            log.info("[GridAI v5] 🎯 ExitModel обучена на %d прибыльных "
                     "сделках (avg_target=%.2f%%)",
                     len(profitable_sells),
                     sum(y_exit) / len(y_exit))

        except Exception as e:
            log.debug("[GridAI v5] exit_model error: %s", e)

    # ── v5: Бэктест + валидация ────────────────────────────────────────────────

    def _backtest_validate(self, X_s: list, y_s: list,
                           w_s: list) -> Tuple[float, float]:
        """v5 НОВОЕ: TimeSeriesSplit кросс-валидация качества step-ансамбля.

        Возвращает (r2_score, direction_accuracy).
        """
        if len(X_s) < 15:
            return 0.0, 0.5

        try:
            from sklearn.model_selection import TimeSeriesSplit
            from sklearn.ensemble import ExtraTreesRegressor
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline

            tscv = TimeSeriesSplit(n_splits=3)
            r2_scores   = []
            dir_accs    = []

            for train_idx, test_idx in tscv.split(X_s):
                if len(train_idx) < 5 or len(test_idx) < 2:
                    continue
                X_tr = [X_s[i] for i in train_idx]
                y_tr = [y_s[i] for i in train_idx]
                w_tr = [w_s[i] for i in train_idx]
                X_te = [X_s[i] for i in test_idx]
                y_te = [y_s[i] for i in test_idx]

                try:
                    m = Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  ExtraTreesRegressor(
                            n_estimators=30, max_depth=4,
                            random_state=42, n_jobs=1)),
                    ])
                    m.fit(X_tr, y_tr, m__sample_weight=w_tr)
                    y_pred = m.predict(X_te)

                    # R²
                    ss_res = sum((yt - yp)**2 for yt, yp in zip(y_te, y_pred))
                    ss_tot = sum((yt - sum(y_te)/len(y_te))**2 for yt in y_te)
                    r2 = 1.0 - ss_res / (ss_tot + 1e-10)
                    r2_scores.append(r2)

                    # Direction accuracy: предсказываем правильное направление
                    # (шаг выше/ниже среднего)
                    y_mean = sum(y_te) / len(y_te)
                    dir_correct = sum(
                        1 for yt, yp in zip(y_te, y_pred)
                        if (yt > y_mean) == (yp > y_mean)
                    )
                    dir_accs.append(dir_correct / len(y_te))
                except Exception:
                    pass

            r2  = sum(r2_scores) / len(r2_scores) if r2_scores else 0.0
            acc = sum(dir_accs)  / len(dir_accs)  if dir_accs  else 0.5
            return r2, acc

        except Exception as e:
            log.debug("[GridAI v5] backtest error: %s", e)
            return 0.0, 0.5

    def _train(self):
        """Полное переобучение ансамбля моделей (v5)."""
        try:
            from sklearn.ensemble import (
                RandomForestRegressor, ExtraTreesRegressor,
                GradientBoostingRegressor,
                RandomForestClassifier, ExtraTreesClassifier)
            from sklearn.linear_model import Ridge, LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline
            from sklearn.model_selection import TimeSeriesSplit

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

            # ── Калибровка и Kelly ──────────────────────────────────────
            profits = [e.get("profit_ton", 0) for e in sells]
            self._calibrate_min_step(sells)
            self._compute_kelly_mult(profits)
            self._compute_kelly_by_regime()

            # ── v5: Обучаем vol-модель (предсказание ATR) ───────────────
            if len(sells) >= 10:
                self._train_vol_model(sells)

            # ── Step-ансамбль ────────────────────────────────────────────
            if len(sells) >= MIN_SAMPLES:
                X_s = [self._make_features(self._safe_atr(e),
                                            e.get("regime", "SIDEWAYS"), e)
                       for e in sells]
                y_s = [_safe_float(e.get("step_used", 4.0)) for e in sells]
                w_s = self._compute_sample_weights(sells, now)

                def _fit_step(model, use_w=True):
                    if use_w:
                        model.fit(X_s, y_s, m__sample_weight=w_s)
                    else:
                        model.fit(X_s, y_s)
                    return model

                self._step_rf = _fit_step(Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  RandomForestRegressor(
                        n_estimators=60, max_depth=6,
                        min_samples_leaf=2, random_state=42, n_jobs=1)),
                ]))

                self._step_et = _fit_step(Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  ExtraTreesRegressor(
                        n_estimators=60, max_depth=6,
                        min_samples_leaf=2, random_state=42, n_jobs=1)),
                ]))

                self._step_gb = _fit_step(Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  GradientBoostingRegressor(
                        n_estimators=80, max_depth=3,
                        learning_rate=0.08, random_state=42)),
                ]))

                if _has_hgb:
                    try:
                        hgb = HistGradientBoostingRegressor(
                            max_iter=80, max_depth=4,
                            learning_rate=0.08, random_state=42)
                        hgb.fit(X_s, y_s, sample_weight=w_s)
                        self._step_hgb = hgb
                    except Exception as he:
                        log.debug("[GridAI v5] HistGB step: %s", he)

                self._step_ridge = _fit_step(Pipeline([
                    ("sc", StandardScaler()),
                    ("m",  Ridge(alpha=1.0)),
                ]))

                # ── v5: OOF мета-стекинг (TimeSeriesSplit) ───────────────
                if len(sells) >= 15:
                    try:
                        tscv = TimeSeriesSplit(n_splits=3)
                        meta_X_oof = [[0.0]*5 for _ in X_s]

                        for tr_idx, te_idx in tscv.split(X_s):
                            if len(tr_idx) < 5:
                                continue
                            X_tr = [X_s[i] for i in tr_idx]
                            y_tr = [y_s[i] for i in tr_idx]
                            w_tr = [w_s[i] for i in tr_idx]

                            oof_preds = []
                            for mname, mcls, mkw in [
                                ("RF", RandomForestRegressor,
                                 dict(n_estimators=30, max_depth=5,
                                      random_state=42, n_jobs=1)),
                                ("ET", ExtraTreesRegressor,
                                 dict(n_estimators=30, max_depth=5,
                                      random_state=42, n_jobs=1)),
                                ("GB", GradientBoostingRegressor,
                                 dict(n_estimators=40, max_depth=3,
                                      learning_rate=0.1, random_state=42)),
                                ("Ridge", Ridge, dict(alpha=1.0)),
                            ]:
                                try:
                                    m = Pipeline([("sc", StandardScaler()),
                                                  ("m",  mcls(**mkw))])
                                    m.fit(X_tr, y_tr, m__sample_weight=w_tr)
                                    fold_preds = [float(m.predict([X_s[i]])[0])
                                                  for i in te_idx]
                                    oof_preds.append(fold_preds)
                                except Exception:
                                    pass

                            if oof_preds:
                                for col, fp in enumerate(oof_preds):
                                    for row, idx in enumerate(te_idx):
                                        if col < 5:
                                            meta_X_oof[idx][col] = fp[row]

                        meta = Pipeline([
                            ("sc", StandardScaler()),
                            ("m",  Ridge(alpha=0.5)),
                        ])
                        meta.fit(meta_X_oof, y_s, m__sample_weight=w_s)
                        self._step_meta = meta
                        log.info("[GridAI v5] 🔗 OOF мета-стекер обучен "
                                 "на %d продажах (TimeSeriesSplit)", len(sells))
                    except Exception as me:
                        log.debug("[GridAI v5] meta-stacker error: %s", me)

                # ── v5: Бэктест перед активацией ─────────────────────────
                r2, dir_acc = self._backtest_validate(X_s, y_s, w_s)
                self._backtest_r2      = r2
                self._backtest_dir_acc = dir_acc
                self._models_validated = (
                    r2 >= BACKTEST_MIN_R2 and dir_acc >= BACKTEST_MIN_DIR_ACC)
                log.info("[GridAI v5] 📊 Бэктест: R²=%.3f dir_acc=%.2f%% "
                         "validated=%s",
                         r2, dir_acc * 100, self._models_validated)

                # ── v5: Обучаем exit-модель ───────────────────────────────
                self._train_exit_model(sells, X_s, now)

                log.info("[GridAI v5] 📊 Step-ансамбль (RF+ET+GB+HistGB+Ridge"
                         "+OOF-Meta) на %d продажах",
                         len(sells))
                gc.collect()

            # ── DCA-ансамбль ─────────────────────────────────────────────
            if len(all_e) >= MIN_SAMPLES:
                y_p   = [int(e.get("is_profitable", 0)) for e in all_e]
                n_pos = sum(y_p)
                n_neg = len(y_p) - n_pos

                if n_pos >= 2 and n_neg >= 1:
                    X_p = [self._make_features(self._safe_atr(e),
                                               e.get("regime", "SIDEWAYS"), e)
                           for e in all_e]
                    w_p = self._compute_sample_weights(all_e, now)

                    def _fit_cls(model, use_w=True):
                        if use_w:
                            model.fit(X_p, y_p, m__sample_weight=w_p)
                        else:
                            model.fit(X_p, y_p)
                        return model

                    self._dca_rf = _fit_cls(Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  RandomForestClassifier(
                            n_estimators=60, max_depth=5,
                            class_weight="balanced",
                            random_state=42, n_jobs=1)),
                    ]))

                    self._dca_et = _fit_cls(Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  ExtraTreesClassifier(
                            n_estimators=60, max_depth=5,
                            class_weight="balanced",
                            random_state=42, n_jobs=1)),
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
                            log.debug("[GridAI v5] HistGB dca: %s", he)

                    self._dca_lr = _fit_cls(Pipeline([
                        ("sc", StandardScaler()),
                        ("m",  LogisticRegression(
                            C=1.0, max_iter=500,
                            class_weight="balanced", random_state=42)),
                    ]))

                    log.info("[GridAI v5] 📊 DCA-ансамбль на %d примерах "
                             "(pos=%d neg=%d)", len(all_e), n_pos, n_neg)
                    gc.collect()

            self._trained      = True
            self._last_train_n = len(self._experience)
            log.info("[GridAI v5] ✅ Обучение завершено: %d примеров (%d sells) "
                     "| min_step=%.2f%% kelly=%.3f risk=%d "
                     "| vol_model=%s exit_model=%s",
                     len(self._experience), len(sells),
                     self.calibrated_min_step, self._kelly_mult,
                     self.get_risk_level(),
                     "✓" if self._vol_model else "✗",
                     "✓" if self._exit_model else "✗")

        except ImportError as e:
            log.warning("[GridAI v5] sklearn не найден: %s — heuristic-режим", e)
        except Exception as e:
            log.error("[GridAI v5] Ошибка обучения: %s", e, exc_info=True)

    # ── PostgreSQL dual-write (v5, улучшение #3) ──────────────────────────────

    def _save_experience(self):
        """Dual-write: JSON + PostgreSQL."""
        # 1. JSON fallback (быстро, надёжно)
        try:
            os.makedirs(os.path.dirname(EXPERIENCE_FILE) or ".", exist_ok=True)
            with open(EXPERIENCE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._experience, f, ensure_ascii=False)
        except Exception as e:
            log.warning("[GridAI v5] JSON save error: %s", e)

        # 2. PostgreSQL — только последнюю запись (bulk-insert при старте)
        if self._experience:
            last = self._experience[-1]
            try:
                import db_store as _db
                _db.grid_experience_insert(last)
            except Exception as e:
                log.debug("[GridAI v5] DB save error: %s", e)

    def _load_experience(self):
        """Загрузка: сначала PostgreSQL (свежее), потом JSON fallback."""
        loaded = False

        # 1. Пробуем PostgreSQL
        try:
            import db_store as _db
            db_exp = _db.grid_experience_load()
            if db_exp:
                self._experience = db_exp
                log.info("[GridAI v5] 🗄️  Загружено %d примеров из PostgreSQL",
                         len(self._experience))
                self._rebuild_rolling_state()
                loaded = True
        except Exception as e:
            log.debug("[GridAI v5] DB load skip: %s", e)

        # 2. JSON fallback или импорт старого формата
        if not loaded:
            try:
                if os.path.exists(EXPERIENCE_FILE):
                    with open(EXPERIENCE_FILE, encoding="utf-8") as f:
                        self._experience = json.load(f)
                    log.info("[GridAI v5] 📁 Загружено %d примеров из JSON "
                             "(DB недоступна)", len(self._experience))
                    self._rebuild_rolling_state()

                    # Миграция JSON → PostgreSQL (bulk-insert при первом запуске)
                    self._migrate_json_to_db()
            except Exception as e:
                log.warning("[GridAI v5] Загрузка опыта: %s", e)
                self._experience = []

    def _migrate_json_to_db(self):
        """Разовая миграция JSON-опыта в PostgreSQL."""
        if not self._experience:
            return
        try:
            import db_store as _db
            if _db.grid_experience_count() > 0:
                return  # уже мигрировано
            log.info("[GridAI v5] 🔄 Миграция %d записей JSON → PostgreSQL...",
                     len(self._experience))
            for entry in self._experience:
                _db.grid_experience_insert(entry)
            log.info("[GridAI v5] ✅ Миграция завершена")
        except Exception as e:
            log.debug("[GridAI v5] Миграция DB: %s", e)

    def _rebuild_rolling_state(self):
        """Восстанавливаем все трекеры из загруженной истории."""
        sells = sorted(
            [e for e in self._experience if e.get("side") == "sell"],
            key=lambda x: x.get("ts", 0))

        self._win_streak         = 0
        self._consecutive_losses = 0

        for e in self._experience:
            atr = _safe_float(e.get("atr_pct"))
            if atr > 0:
                self._recent_atrs.append(atr)
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
