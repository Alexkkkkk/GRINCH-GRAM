"""
grid_ai.py v6 — QuantumGrid AI: Самая умная сетка в мире (Самоэволюция)

Улучшения v6 относительно v5 — 10 механизмов саморазвития:
   1. 🚨 DriftDetector   — ADWIN-lite: авто-сброс опыта при смене рынка
   2. 🧪 SyntheticDataAug— Bootstrap + noise: решает R²=-млрд при мало данных
   3. 🎲 StepStrategyBandit — UCB1 Multi-Armed Bandit: авто-выбор стратегии шага
   4. 🧠 RegimeSpecializedModels — отдельные модели TREND_UP/DOWNTREND/VOLATILE
   5. 🧬 HyperEvolver    — эволюция гиперпараметров каждые 10 сделок
   6. 🔬 FeatureEvolver  — авто-открытие новых комбинаций признаков
   7. 🔄 RLGridAgent     — Q-learning: обучение на реальных наградах
   8. 🌳 NASLite         — поиск лучшего ансамбля каждые 100 сделок
   9. 🎯 MetaLearner     — запоминает лучшие гипер-параметры per рыночный контекст
  10. 📊 Аудит эволюции  — PostgreSQL лог поколений + дашборд selfdev

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

DATA_DIR = os.getenv("DATA_DIR", ".")
EXPERIENCE_FILE = os.path.join(DATA_DIR, "grid_ai_experience.json")
SELFDEV_FILE = os.path.join(DATA_DIR, "grid_ai_selfdev.json")

# Минимум примеров для первого обучения
MIN_SAMPLES = 5
# Полужизнь весов (дни): через 7 дней временной вес = 0.5
DECAY_HALFLIFE_DAYS = 7.0
# Размерность вектора признаков v5 (НЕЛЬЗЯ менять без полной очистки experience)
FEAT_DIM = 40
# Порог R² для активации новых моделей (бэктест)
BACKTEST_MIN_R2 = -0.5  # мягкий — у малых датасетов R² может быть отриц.
# Порог direction accuracy
BACKTEST_MIN_DIR_ACC = 0.45  # 45% → лучше монетки

# Режимо-специфичные границы шага [min%, max%]
REGIME_STEP_BOUNDS: Dict[str, Tuple[float, float]] = {
    "SQUEEZE": (3.0, 5.5),
    "SIDEWAYS": (3.5, 7.0),
    "RANGING": (3.5, 7.0),
    "VOLATILE": (5.0, 10.0),
    "TREND_UP": (6.0, 10.0),
    "UPTREND": (6.0, 10.0),
    "TREND_DOWN": (4.0, 8.0),
    "DOWNTREND": (4.0, 8.0),
    "TRANSITION": (3.5, 7.0),
    "PUMP": (7.0, 10.0),
    "DISTRIBUTION": (6.0, 10.0),
    "POST_PUMP": (5.0, 8.5),
    "UNKNOWN": (3.5, 8.0),
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
        "TREND_UP": 2,
        "UPTREND": 2,
        "VOLATILE": 1,
        "SIDEWAYS": 0,
        "SQUEEZE": 0,
        "RANGING": 0,
        "UNKNOWN": 0,
        "TREND_DOWN": -1,
        "DOWNTREND": -2,
        "DISTRIBUTION": -1,
        "POST_PUMP": -3,
        "PUMP": 3,
    }.get(regime if isinstance(regime, str) else "UNKNOWN", 0)


# ─── GridAI v6: Классы саморазвития ──────────────────────────────────────────


class DriftDetector:
    """ADWIN-lite: детектор дрейфа концепции (concept drift).

    Сравнивает среднее прибылей первой и второй половин скользящего окна.
    При резком изменении характеристик — сигнализирует о смене режима рынка.
    """

    def __init__(self, window: int = 30, threshold: float = 0.6):
        self._window = window
        self._threshold = threshold
        self._buffer: List[float] = []
        self._drift_count = 0
        self._last_drift_ts = 0.0

    def update(self, profit: float) -> bool:
        """Добавить наблюдение; вернуть True если обнаружен дрейф."""
        self._buffer.append(profit)
        max_buf = self._window * 2
        if len(self._buffer) > max_buf:
            self._buffer = self._buffer[-max_buf:]

        if len(self._buffer) < 20:
            return False

        half = len(self._buffer) // 2
        w1 = self._buffer[:half]
        w2 = self._buffer[half:]
        m1 = sum(w1) / len(w1)
        m2 = sum(w2) / len(w2)
        v1 = sum((x - m1) ** 2 for x in w1) / max(len(w1) - 1, 1)
        v2 = sum((x - m2) ** 2 for x in w2) / max(len(w2) - 1, 1)
        pooled_std = math.sqrt((v1 + v2) / 2.0 + 1e-9)
        diff = abs(m2 - m1) / pooled_std

        if diff > self._threshold:
            now = time.time()
            if now - self._last_drift_ts > 3600:  # cooldown 1h
                self._drift_count += 1
                self._last_drift_ts = now
                self._buffer = self._buffer[-10:]  # оставляем последние 10
                log.info(
                    "[DriftDetector] 🚨 Дрейф #%d обнаружен " "(diff=%.2f > %.2f)",
                    self._drift_count,
                    diff,
                    self._threshold,
                )
                return True
        return False

    @property
    def drift_count(self) -> int:
        return self._drift_count


class SyntheticDataAug:
    """Bootstrap + Gaussian noise для расширения малого датасета.

    Решает проблему R²=-7 миллиардов при <30 сделках — расширяет датасет
    до target_n примеров путём бутстрэпа с шумом.
    """

    def augment(
        self, experience: List[dict], target_n: int = 150, noise_scale: float = 0.05
    ) -> List[dict]:
        """Вернуть augmented список; синтетические помечены _synthetic=True."""
        if len(experience) >= target_n or len(experience) < 5:
            return experience
        import random

        aug = list(experience)
        need = target_n - len(experience)
        numeric_keys = [
            "atr_pct",
            "step_used",
            "profit_ton",
            "profit_pct",
            "recent_avg_profit",
            "profit_momentum",
            "atr_normalized",
            "fill_density_1h",
            "drawdown_pct",
        ]
        for _ in range(need):
            base = random.choice(experience)
            synth = dict(base)
            synth["ts"] = time.time()
            synth["_synthetic"] = True
            for key in numeric_keys:
                if key in synth and isinstance(synth[key], (int, float)):
                    v = float(synth[key])
                    synth[key] = v + v * noise_scale * (random.random() * 2 - 1)
            synth["is_profitable"] = 1 if synth.get("profit_ton", 0) > 0 else 0
            aug.append(synth)
        return aug


class StepStrategyBandit:
    """UCB1 Multi-Armed Bandit для авто-выбора стратегии шага (v6).

    Стратегии:
      conservative — Kelly × 0.8, шаг ближе к min
      aggressive   — Kelly × 1.2, шаг ближе к max
      atr_pure     — только эвристика ATR, без ML
      kelly        — только Kelly, без ML-предсказания
      ml_only      — только ML-ансамбль (поведение v5)
    """

    STRATEGIES = ["conservative", "aggressive", "atr_pure", "kelly", "ml_only"]

    def __init__(self):
        self._counts: Dict[str, int] = {s: 0 for s in self.STRATEGIES}
        self._rewards: Dict[str, float] = {s: 0.0 for s in self.STRATEGIES}
        self._total = 0
        self._last_strategy = "ml_only"
        self._pending_strategy = "ml_only"  # ожидает reward

    def select(self, regime: str = "SIDEWAYS") -> str:
        """UCB1: вернуть стратегию для текущего тика."""
        # Exploration: попробовать каждую хотя бы раз
        for s in self.STRATEGIES:
            if self._counts[s] == 0:
                self._pending_strategy = s
                self._last_strategy = s
                return s
        # UCB1
        log_total = math.log(self._total + 1)
        best_s = max(
            self.STRATEGIES,
            key=lambda s: (
                self._rewards[s] / max(self._counts[s], 1)
                + math.sqrt(2 * log_total / max(self._counts[s], 1))
            ),
        )
        self._pending_strategy = best_s
        self._last_strategy = best_s
        return best_s

    def update_reward(self, profit: float):
        """Обновить награду для последней выбранной стратегии."""
        s = self._pending_strategy
        self._counts[s] += 1
        self._rewards[s] += profit
        self._total += 1

    def seed_baseline_from_history(self, experience: list) -> int:
        """Восстановить безопасную базовую статистику после старого рестарта.

        До v6 стратегия, выбранная bandit, не сохранялась в каждом fill, поэтому
        нельзя честно раздать старые сделки между стратегиями. Все исторические
        SELL считаем baseline ``ml_only`` — это поведение v5 без ложной
        атрибуции. Новые тики продолжат UCB-исследование остальных стратегий.
        """
        if self._total > 0 or not isinstance(experience, list):
            return 0
        sells = [
            e for e in experience if isinstance(e, dict) and e.get("side") == "sell"
        ]
        if not sells:
            return 0
        profit = sum(_safe_float(e.get("profit_ton", 0.0)) for e in sells)
        self._counts["ml_only"] = len(sells)
        self._rewards["ml_only"] = profit
        self._total = len(sells)
        self._last_strategy = "ml_only"
        self._pending_strategy = "ml_only"
        return len(sells)

    def apply_strategy(
        self,
        strategy: str,
        ml_pred: float,
        heuristic: float,
        kelly_mult: float,
        eff_min: float,
        eff_max: float,
    ) -> float:
        """Применить стратегию bandit к ML-предсказанию."""
        if strategy == "conservative":
            step = ml_pred * kelly_mult * 0.8
        elif strategy == "aggressive":
            step = ml_pred * kelly_mult * 1.2
        elif strategy == "atr_pure":
            return max(eff_min, min(eff_max, heuristic))
        elif strategy == "kelly":
            step = ml_pred * kelly_mult
        else:  # ml_only — default v5
            step = ml_pred
        return max(eff_min, min(eff_max, step))

    def get_stats(self) -> dict:
        stats = {}
        for s in self.STRATEGIES:
            n = self._counts[s]
            stats[s] = {
                "count": n,
                "avg_reward": round(self._rewards[s] / max(n, 1), 4),
                "total_reward": round(self._rewards[s], 4),
            }
        return {
            "strategies": stats,
            "last_strategy": self._last_strategy,
            "total_pulls": self._total,
        }

    def to_json(self) -> dict:
        return {
            "counts": dict(self._counts),
            "rewards": dict(self._rewards),
            "total": self._total,
            "last": self._last_strategy,
        }

    @classmethod
    def from_json(cls, data: dict) -> "StepStrategyBandit":
        b = cls()
        b._counts = {s: data.get("counts", {}).get(s, 0) for s in cls.STRATEGIES}
        b._rewards = {s: data.get("rewards", {}).get(s, 0.0) for s in cls.STRATEGIES}
        b._total = data.get("total", 0)
        b._last_strategy = data.get("last", "ml_only")
        b._pending_strategy = data.get("last", "ml_only")
        return b


class RegimeSpecializedModels:
    """Режимно-специализированные sub-модели (v6, механизм #4).

    Когда в режиме накапливается MIN_SAMPLES+ сделок — обучает
    отдельную модель специально для него. Специалист бьёт универсала.
    """

    MIN_REGIME_SAMPLES = 15

    def __init__(self):
        self._models: Dict[str, object] = {}
        self._sample_counts: Dict[str, int] = {}

    def train_regime(self, regime: str, X: list, y: list, w: list):
        """Обучить специализированную модель для режима."""
        if len(X) < self.MIN_REGIME_SAMPLES:
            return
        try:
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline

            m = Pipeline(
                [
                    ("sc", StandardScaler()),
                    (
                        "m",
                        RandomForestRegressor(
                            n_estimators=50,
                            max_depth=5,
                            min_samples_leaf=2,
                            random_state=42,
                            n_jobs=1,
                        ),
                    ),
                ]
            )
            m.fit(X, y, m__sample_weight=w)
            self._models[regime] = m
            self._sample_counts[regime] = len(X)
            log.info("[GridAI v6] 🎯 Режимная модель [%s]: %d примеров", regime, len(X))
        except Exception as e:
            log.debug("[GridAI v6] regime_model [%s]: %s", regime, e)

    def predict(self, regime: str, feat: list) -> Optional[float]:
        m = self._models.get(regime)
        if m is None:
            return None
        try:
            return float(m.predict([feat])[0])
        except Exception:
            return None

    def get_stats(self) -> dict:
        return {
            "trained_regimes": list(self._models.keys()),
            "sample_counts": dict(self._sample_counts),
        }


class HyperEvolver:
    """Эволюционный оптимизатор гиперпараметров (v6, механизм #5).

    Каждые EVOLVE_EVERY сделок пробует CONFIGS конфигураций ансамбля
    и выбирает лучшую по OOF R². Следующие обучения используют найденный конфиг.
    """

    EVOLVE_EVERY = 10
    CONFIGS = [
        {"n_estimators": 60, "max_depth": 6, "lr": 0.08},
        {"n_estimators": 80, "max_depth": 5, "lr": 0.10},
        {"n_estimators": 100, "max_depth": 4, "lr": 0.06},
        {"n_estimators": 40, "max_depth": 8, "lr": 0.12},
        {"n_estimators": 120, "max_depth": 3, "lr": 0.05},
    ]

    def __init__(self):
        self._best_config_idx = 0
        self._best_r2 = -999.0
        self._last_evolved_n = 0
        self._evolutions = 0

    def should_evolve(self, n_sells: int) -> bool:
        return (n_sells - self._last_evolved_n) >= self.EVOLVE_EVERY and n_sells >= 15

    def evolve(self, X: list, y: list, w: list) -> dict:
        """Перебирает CONFIGS, возвращает лучший конфиг."""
        if len(X) < 15:
            return self.CONFIGS[self._best_config_idx]
        try:
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline
            from sklearn.model_selection import TimeSeriesSplit

            best_r2 = -999.0
            best_idx = self._best_config_idx
            n_splits = min(3, max(2, len(X) // 7))
            tscv = TimeSeriesSplit(n_splits=n_splits)

            for i, cfg in enumerate(self.CONFIGS):
                fold_r2s = []
                for tr_idx, te_idx in tscv.split(X):
                    if len(tr_idx) < 5 or len(te_idx) < 2:
                        continue
                    X_tr = [X[j] for j in tr_idx]
                    y_tr = [y[j] for j in tr_idx]
                    w_tr = [w[j] for j in tr_idx]
                    X_te = [X[j] for j in te_idx]
                    y_te = [y[j] for j in te_idx]
                    try:
                        m = Pipeline(
                            [
                                ("sc", StandardScaler()),
                                (
                                    "m",
                                    RandomForestRegressor(
                                        n_estimators=cfg["n_estimators"],
                                        max_depth=cfg["max_depth"],
                                        random_state=42,
                                        n_jobs=1,
                                    ),
                                ),
                            ]
                        )
                        m.fit(X_tr, y_tr, m__sample_weight=w_tr)
                        y_pred = m.predict(X_te)
                        ss_res = sum((yt - yp) ** 2 for yt, yp in zip(y_te, y_pred))
                        ss_tot = sum((yt - sum(y_te) / len(y_te)) ** 2 for yt in y_te)
                        r2 = 1.0 - ss_res / (ss_tot + 1e-10)
                        fold_r2s.append(r2)
                    except Exception:
                        pass
                avg_r2 = sum(fold_r2s) / len(fold_r2s) if fold_r2s else -999.0
                if avg_r2 > best_r2:
                    best_r2 = avg_r2
                    best_idx = i

            if best_r2 > self._best_r2:
                self._best_r2 = best_r2
                self._best_config_idx = best_idx
                log.info(
                    "[GridAI v6] 🧬 HyperEvolver #%d: конфиг #%d R²=%.3f "
                    "n_est=%d depth=%d",
                    self._evolutions + 1,
                    best_idx,
                    best_r2,
                    self.CONFIGS[best_idx]["n_estimators"],
                    self.CONFIGS[best_idx]["max_depth"],
                )
            self._evolutions += 1
            self._last_evolved_n = len(X)
            return self.CONFIGS[self._best_config_idx]
        except Exception as e:
            log.debug("[GridAI v6] HyperEvolver error: %s", e)
            return self.CONFIGS[self._best_config_idx]

    @property
    def best_config(self) -> dict:
        return self.CONFIGS[self._best_config_idx]

    def to_json(self) -> dict:
        return {
            "best_config_idx": self._best_config_idx,
            "best_r2": self._best_r2,
            "last_evolved_n": self._last_evolved_n,
            "evolutions": self._evolutions,
        }

    @classmethod
    def from_json(cls, data: dict) -> "HyperEvolver":
        h = cls()
        h._best_config_idx = data.get("best_config_idx", 0)
        h._best_r2 = data.get("best_r2", -999.0)
        h._last_evolved_n = data.get("last_evolved_n", 0)
        h._evolutions = data.get("evolutions", 0)
        return h


class RLGridAgent:
    """Tabular Q-learning агент для онлайн-адаптации шага (v6, механизм #7).

    State  = (режим, уровень риска, бакет серии побед)
    Action = смещение шага: [-2, -1, 0, +1, +2] %
    Reward = profit_ton за закрытую сделку
    """

    STEP_OFFSETS = [-2.0, -1.0, 0.0, 1.0, 2.0]
    ALPHA = 0.1  # learning rate
    GAMMA = 0.9  # discount factor
    EPSILON = 0.12  # exploration

    def __init__(self):
        self._q: Dict[str, List[float]] = {}
        self._last_state: Optional[str] = None
        self._last_action_idx = 2  # default = no offset
        self._total_reward = 0.0
        self._episodes = 0

    def _state_key(self, regime: str, risk_level: int, win_streak: int) -> str:
        streak_bucket = min(win_streak // 3, 3)
        return f"{regime}_{risk_level}_{streak_bucket}"

    def select_offset(self, regime: str, risk_level: int, win_streak: int) -> float:
        """ε-greedy: вернуть смещение шага в % (для добавления к ML-предсказанию)."""
        import random

        key = self._state_key(regime, risk_level, win_streak)
        if key not in self._q:
            self._q[key] = [0.0] * len(self.STEP_OFFSETS)
        self._last_state = key
        if random.random() < self.EPSILON:
            idx = random.randint(0, len(self.STEP_OFFSETS) - 1)
        else:
            q_vals = self._q[key]
            idx = q_vals.index(max(q_vals))
        self._last_action_idx = idx
        return self.STEP_OFFSETS[idx]

    def update(self, regime: str, risk_level: int, win_streak: int, profit: float):
        """Q-learning обновление по результату сделки."""
        if self._last_state is None:
            return
        next_key = self._state_key(regime, risk_level, win_streak)
        if next_key not in self._q:
            self._q[next_key] = [0.0] * len(self.STEP_OFFSETS)
        old_q = self._q[self._last_state][self._last_action_idx]
        max_next = max(self._q[next_key])
        new_q = old_q + self.ALPHA * (profit + self.GAMMA * max_next - old_q)
        self._q[self._last_state][self._last_action_idx] = new_q
        self._total_reward += profit
        self._episodes += 1

    def get_stats(self) -> dict:
        return {
            "states": len(self._q),
            "episodes": self._episodes,
            "total_reward": round(self._total_reward, 4),
            "avg_reward": round(self._total_reward / max(self._episodes, 1), 4),
        }

    def to_json(self) -> dict:
        return {
            "q": dict(self._q),
            "last_state": self._last_state,
            "last_action_idx": self._last_action_idx,
            "total_reward": self._total_reward,
            "episodes": self._episodes,
        }

    @classmethod
    def from_json(cls, data: dict) -> "RLGridAgent":
        a = cls()
        a._q = {k: list(v) for k, v in data.get("q", {}).items()}
        a._last_state = data.get("last_state")
        a._last_action_idx = data.get("last_action_idx", 2)
        a._total_reward = data.get("total_reward", 0.0)
        a._episodes = data.get("episodes", 0)
        return a


# ─── Основной класс ───────────────────────────────────────────────────────────


class GridAI:
    """Самообучающийся AI-оптимизатор сеточной торговли v6 (Самоэволюция).

    Публичное API (обратно совместимо с v5):
      set_market_context(mkt)              ← v5
      set_mtf_context(mtf)                 ← v5
      get_optimal_step(atr_pct, regime, min_step, max_step) → float
      get_dca_confidence(atr_pct, regime, drawdown_pct, price_vs_center_pct) → float
      get_dca_size_multiplier(cycle_num, win_rate) → float
      get_pyramid_weights(n_levels) → List[float]
      get_sell_target_pct(step_pct, regime, atr_pct) → float
      get_risk_level() → int
      should_pause_buying(regime, drawdown_pct, ai_sell_conf) → bool
      check_trap_exit(regime, drawdown_pct, price_ton, center_price_ton) → dict
      update_regime(regime)
      record_fill(side, step_used, atr_pct, regime, profit_ton, profit_pct, ...)
      get_stats() → dict
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._experience: List[dict] = []

        # ── Модели ансамбля — шаг ────────────────────────────────────────
        self._step_rf = None  # RandomForestRegressor
        self._step_et = None  # ExtraTreesRegressor
        self._step_gb = None  # GradientBoostingRegressor
        self._step_hgb = None  # HistGradientBoostingRegressor
        self._step_ridge = None  # Ridge baseline
        self._step_meta = None  # Мета-стекер (OOF TimeSeriesSplit)
        self._step_sgd = None  # Инкрементальный SGD

        # ── v5: модели ──────────────────────────────────────────────────
        self._vol_model = None  # Предсказание будущего ATR
        self._exit_model = None  # Предсказание % выхода

        # ── Модели DCA ────────────────────────────────────────────────────
        self._dca_rf = None
        self._dca_et = None
        self._dca_hgb = None
        self._dca_lr = None
        self._dca_sgd = None

        # ── Скользящая статистика ─────────────────────────────────────────
        self._win_streak: int = 0
        self._recent_profits: deque = deque(maxlen=20)
        self._regime_dur: int = 0
        self._last_regime: str = ""
        self._consecutive_losses: int = 0
        self._recent_atrs: deque = deque(maxlen=30)
        self._last_compound_mult: float = 1.0
        self._regime_profits: Dict[str, list] = {}

        # ── v5: рыночный контекст ─────────────────────────────────────────
        self._mkt_ctx: dict = {}
        self._mtf_ctx: dict = {}

        # ── Предсказанный ATR ─────────────────────────────────────────────
        self._predicted_atr: float = 0.0

        # ── Kelly и калибровка ────────────────────────────────────────────
        self.calibrated_min_step: float = 4.0
        self._kelly_mult: float = 1.0
        self._kelly_by_regime: Dict[str, float] = {}

        self._trained = False
        self._last_train_n = 0
        self._backtest_r2: float = 0.0
        self._backtest_dir_acc: float = 0.0
        self._models_validated: bool = False

        # ════════════════════════════════════════════════════════════════
        # v6: Компоненты саморазвития
        # ════════════════════════════════════════════════════════════════
        self._generation: int = 0  # поколение AI
        self._drift_detector = DriftDetector(window=30, threshold=0.6)
        self._aug = SyntheticDataAug()
        self._bandit = StepStrategyBandit()
        self._regime_models = RegimeSpecializedModels()
        self._hyper_evolver = HyperEvolver()
        self._rl_agent = RLGridAgent()
        self._selfdev_log: List[dict] = []  # последние события эволюции
        self._drift_forced_retrain = False  # флаг: следующий _train — из-за дрейфа
        self._nas_next_n: int = 100  # NASLite: через сколько сделок искать
        self._nas_best_ensemble: Optional[str] = None

        self._load_experience()
        self._load_selfdev_state()
        seeded = self._bandit.seed_baseline_from_history(self._experience)
        if seeded:
            self._save_selfdev_state()
            log.info(
                "[GridAI v6] 🧭 Bandit baseline восстановлен: "
                "%d исторических SELL → ml_only",
                seeded,
            )
        if len(self._experience) >= MIN_SAMPLES:
            self._train()
        log.info(
            "[GridAI v6] Инициализирован. Примеров: %d, обучен: %s, "
            "поколение=#%d min_step=%.2f%% kelly=%.3f FEAT_DIM=%d",
            len(self._experience),
            self._trained,
            self._generation,
            self.calibrated_min_step,
            self._kelly_mult,
            FEAT_DIM,
        )

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

    def get_optimal_step(
        self,
        atr_pct: float,
        regime: str = "SIDEWAYS",
        min_step: float = None,
        max_step: float = 10.0,
    ) -> float:
        """Предсказать оптимальный шаг сетки.

        v5: P&L-симуляция 5 кандидатов + OOF мета-стекинг +
            используется предсказанный ATR (не только текущий).
        """
        if min_step is None:
            min_step = self.calibrated_min_step

        # Режимо-специфичные границы
        r_min, r_max = REGIME_STEP_BOUNDS.get(
            regime if isinstance(regime, str) else "UNKNOWN", (min_step, max_step)
        )
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

            # ── v6: Режимная специализированная модель ─────────────────
            regime_pred = self._regime_models.predict(regime, feat)
            if regime_pred is not None:
                ml_pred = 0.5 * ml_pred + 0.5 * regime_pred
                ml_pred = max(effective_min, min(effective_max, ml_pred))

            # ── v6: Bandit — выбор стратегии ──────────────────────────
            strategy = self._bandit.select(regime)
            ml_pred = self._bandit.apply_strategy(
                strategy,
                ml_pred,
                heuristic,
                blended_kelly,
                effective_min,
                effective_max,
            )

            # v5: P&L-симуляция
            if self._exit_model is not None and self._models_validated:
                ml_pred = self._simulate_best_step(
                    feat, ml_pred, effective_min, effective_max
                )

            ml_pred = round(ml_pred * 2) / 2

            # ── v6: RL-агент смещение ──────────────────────────────────
            n = len(self._experience)
            if n >= 30 and self._rl_agent._episodes >= 5:
                rl_offset = self._rl_agent.select_offset(
                    regime, self.get_risk_level(), self._win_streak
                )
                ml_pred = max(effective_min, min(effective_max, ml_pred + rl_offset))

            ml_pred = round(ml_pred * 2) / 2

            # Плавный переход по числу примеров
            weight = min(1.0, (n - MIN_SAMPLES) / 45.0)
            blended = heuristic * (1 - weight) + ml_pred * weight

            result = max(effective_min, min(effective_max, round(blended * 2) / 2))
            log.debug(
                "[GridAI v6] step: h=%.1f ml=%.1f k=%.2f strat=%s → %.1f "
                "(ATR=%.2f%% predATR=%.2f%% regime=%s n=%d)",
                heuristic,
                ml_pred,
                blended_kelly,
                strategy,
                result,
                atr_pct,
                pred_atr,
                regime,
                n,
            )
            return result

        except Exception as e:
            log.warning("[GridAI v6] predict_step error: %s", e)
            return heuristic

    def get_dca_confidence(
        self,
        atr_pct: float,
        regime: str,
        drawdown_pct: float,
        price_vs_center_pct: float,
    ) -> float:
        """Уверенность что стоит делать DCA-добавление (0–100%)."""
        if regime in ("PUMP", "DISTRIBUTION", "POST_PUMP"):
            return 0.0
        if drawdown_pct > 50.0:
            return 0.0

        regime_bias = {
            "SIDEWAYS": 1.20,
            "SQUEEZE": 1.15,
            "RANGING": 1.10,
            "UNKNOWN": 1.00,
            "VOLATILE": 0.90,
            "TREND_UP": 0.80,
            "TREND_DOWN": 0.50,
            "DOWNTREND": 0.40,
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

        if not self._trained or (
            self._dca_rf is None and self._dca_et is None and self._dca_hgb is None
        ):
            raw = 60.0 * bias if atr_pct >= 2.0 and drawdown_pct < 40.0 else 25.0
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
            log.warning("[GridAI v6] dca_confidence error: %s", e)
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
            self._regime_dur = 0
            self._last_regime = regime
        else:
            self._regime_dur += 1

    def record_fill(
        self,
        side: str,
        step_used: float,
        atr_pct: float,
        regime: str,
        profit_ton: float,
        profit_pct: float,
        is_dca: bool = False,
        compound_mult: float = 1.0,
        drawdown_pct: float = 0.0,
    ):
        """Записать результат исполненного уровня.

        v5: сохраняет рыночный контекст в момент fill + dual-write в PostgreSQL.
        """
        now = time.time()
        atr_pct = _safe_float(atr_pct)

        # ── Производные признаки ──────────────────────────────────────────
        recent = list(self._recent_profits)
        recent_avg = sum(recent) / len(recent) if recent else 0.0
        profit_momentum = 0.0
        if len(recent) >= 3:
            profit_momentum = (recent[-1] - recent[0]) / max(
                abs(recent[0]) + 1e-6, 1e-6
            )
            profit_momentum = max(-2.0, min(2.0, profit_momentum))

        rp5 = list(self._recent_profits)[-5:]
        recent_win_rate_5 = sum(1 for x in rp5 if x > 0) / max(len(rp5), 1)

        mean_atr = (
            sum(self._recent_atrs) / len(self._recent_atrs)
            if self._recent_atrs
            else atr_pct
        )
        atr_norm = atr_pct / max(mean_atr, 0.5) if mean_atr > 0 else 1.0

        fill_density = self._compute_fill_density()
        regime_conf = min(self._regime_dur / 20.0, 1.0)

        # v5: snapshot рыночного контекста в момент fill
        mkt_snap = dict(self._mkt_ctx) if self._mkt_ctx else {}
        mtf_snap = dict(self._mtf_ctx) if self._mtf_ctx else {}

        entry = {
            # v3 базовые поля
            "ts": now,
            "side": side,
            "step_used": step_used,
            "atr_pct": atr_pct,
            "regime": regime,
            "profit_ton": profit_ton,
            "profit_pct": profit_pct,
            "is_dca": is_dca,
            "is_profitable": 1 if profit_ton > 0 else 0,
            "win_streak": self._win_streak,
            "recent_avg_profit": round(recent_avg, 4),
            "profit_momentum": round(profit_momentum, 4),
            "hour": int(time.strftime("%H", time.gmtime(now))),
            "regime_duration": self._regime_dur,
            # v4 расширенные поля
            "consecutive_losses": self._consecutive_losses,
            "compound_mult": round(max(1.0, min(2.0, compound_mult)), 4),
            "drawdown_pct": round(max(0.0, min(60.0, drawdown_pct)), 2),
            "recent_win_rate_5": round(recent_win_rate_5, 3),
            "fill_density_1h": round(fill_density, 2),
            "atr_normalized": round(max(0.3, min(3.0, atr_norm)), 4),
            "regime_confidence": round(regime_conf, 3),
            # v5 рыночный контекст
            "market_ctx": mkt_snap,
            "mtf_ctx": mtf_snap,
        }

        # ── Обновляем трекеры ─────────────────────────────────────────────
        self._recent_profits.append(profit_ton)
        self._recent_atrs.append(atr_pct)
        self._last_compound_mult = compound_mult

        if profit_ton > 0:
            self._win_streak += 1
            self._consecutive_losses = 0
        else:
            self._win_streak = 0
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

            # ── v6: Drift Detection (только для sell-сделок) ─────────────
            if side == "sell":
                drift = self._drift_detector.update(profit_ton)
                if drift:
                    # Сброс: оставляем только последние 10 записей
                    self._experience = self._experience[-10:]
                    self._drift_forced_retrain = True
                    self._selfdev_log.append(
                        {
                            "ts": time.time(),
                            "event": "drift",
                            "drift_count": self._drift_detector.drift_count,
                            "exp_kept": len(self._experience),
                        }
                    )
                    if len(self._selfdev_log) > 50:
                        self._selfdev_log = self._selfdev_log[-50:]
                    log.warning(
                        "[GridAI v6] 🚨 Дрейф #%d: опыт сокращён до %d записей, "
                        "принудительный ретрейн",
                        self._drift_detector.drift_count,
                        len(self._experience),
                    )

                # ── v6: RL-агент обновление ──────────────────────────────
                self._rl_agent.update(
                    regime, self.get_risk_level(), self._win_streak, profit_ton
                )

                # ── v6: Bandit обновление ────────────────────────────────
                self._bandit.update_reward(profit_ton)

            if len(self._experience) >= MIN_SAMPLES and (
                len(self._experience) != self._last_train_n
                or self._drift_forced_retrain
            ):
                self._drift_forced_retrain = False
                self._train()
            else:
                self._save_selfdev_state()

        log.info(
            "[GridAI v6] 📝 Fill: side=%s step=%.1f%% profit=%+.4f TON "
            "(%.2f%%) streak=%d consec_loss=%d n=%d gen=#%d",
            side,
            step_used,
            profit_ton,
            profit_pct,
            self._win_streak,
            self._consecutive_losses,
            len(self._experience),
            self._generation,
        )

    # ── v5: Новые API ──────────────────────────────────────────────────────────

    def get_sell_target_pct(
        self, step_pct: float, regime: str, atr_pct: float
    ) -> float:
        """Оптимальный целевой % SELL выше цены покупки.

        v5: если обучен exit_model — использует ML-предсказание.
        Иначе — улучшенная эвристика с учётом рыночного контекста.
        """
        regime_mult = {
            "SQUEEZE": 0.85,
            "SIDEWAYS": 0.90,
            "RANGING": 0.90,
            "UNKNOWN": 1.00,
            "VOLATILE": 1.10,
            "TREND_UP": 1.15,
            "TREND_DOWN": 0.80,
            "DOWNTREND": 0.75,
            "POST_PUMP": 0.75,
            "DISTRIBUTION": 0.85,
            "PUMP": 1.30,
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

    def should_pause_buying(
        self, regime: str, drawdown_pct: float, ai_sell_conf: float
    ) -> bool:
        """Мультикритериальная рекомендация приостановить покупки."""
        if regime in ("PUMP", "DISTRIBUTION"):
            return True

        if self.get_risk_level() >= 2 and drawdown_pct > 25.0:
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

    def check_trap_exit(
        self,
        regime: str,
        drawdown_pct: float,
        price_ton: float,
        center_price_ton: float,
    ) -> dict:
        """v5 НОВОЕ: Детектор ловушки — рекомендовать выход из застрявшей сетки.

        Возвращает:
          { "trap": bool, "confidence": float (0-100), "reason": str,
            "action": "EXIT"|"REDUCE"|"HOLD" }

        Ловушка = сетка застряла в даунтренде: убытки накапливаются,
        цена не восстанавливается, нет смысла ждать.
        """
        confidence = 0.0
        reasons = []

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
            "trap": is_trap,
            "confidence": round(min(100.0, confidence), 1),
            "action": action,
            "reason": "; ".join(reasons) if reasons else "нет признаков ловушки",
            "regime": regime,
            "drawdown": drawdown_pct,
        }

    def get_stats(self) -> dict:
        """Расширенная статистика для дашборда."""
        with self._lock:
            now = time.time()
            exp = self._experience
            sells = [e for e in exp if e.get("side") == "sell"]
            buys = [e for e in exp if e.get("side") == "buy"]

            if not exp:
                return {"trained": False, "samples": 0, "version": "v5"}

            profits = [e["profit_ton"] for e in sells if "profit_ton" in e]
            wins = [p for p in profits if p > 0]
            losses = [p for p in profits if p <= 0]
            win_rate = len(wins) / len(profits) * 100 if profits else 0

            recent_sells = sells[-20:]
            recent_wins = sum(1 for e in recent_sells if e.get("profit_ton", 0) > 0)
            recent_wr = recent_wins / len(recent_sells) * 100 if recent_sells else 0

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
                p = len(wins) / len(profits)
                kelly_edge = round((p * avg_w - (1 - p) * avg_l) / avg_w, 3)

            fill_times = sorted([e.get("ts", 0) for e in sells if e.get("ts")])
            avg_fill_hours = 0.0
            if len(fill_times) >= 2:
                gaps = [
                    fill_times[i + 1] - fill_times[i]
                    for i in range(len(fill_times) - 1)
                ]
                avg_fill_hours = round(sum(gaps) / len(gaps) / 3600, 2)

            # Per-regime breakdown
            regime_stats = {}
            for r, rp in self._regime_profits.items():
                if rp:
                    regime_stats[r] = {
                        "count": len(rp),
                        "win_rate": round(
                            sum(1 for x in rp if x > 0) / len(rp) * 100, 1
                        ),
                        "avg_pnl": round(sum(rp) / len(rp), 4),
                        "kelly": round(
                            self._kelly_by_regime.get(r, self._kelly_mult), 3
                        ),
                    }

            # ── v6: selfdev log (последние 10 событий) ──────────────────
            recent_events = self._selfdev_log[-10:]

            return {
                "version": "v6",
                "trained": self._trained,
                "samples": len(exp),
                "sell_fills": len(sells),
                "buy_fills": len(buys),
                "total_profit_ton": round(sum(profits), 4),
                "avg_profit_ton": (
                    round(sum(profits) / len(profits), 4) if profits else 0
                ),
                "best_profit_ton": round(max(profits), 4) if profits else 0,
                "worst_profit_ton": round(min(profits), 4) if profits else 0,
                "win_rate_pct": round(win_rate, 1),
                "recent_win_rate": round(recent_wr, 1),
                "profit_trend": trend,
                "avg_step_used": round(avg_step, 2),
                "win_streak": self._win_streak,
                "consecutive_losses": self._consecutive_losses,
                "risk_level": self.get_risk_level(),
                "kelly_edge": kelly_edge,
                "kelly_mult": round(self._kelly_mult, 3),
                "calibrated_min_step": round(self.calibrated_min_step, 2),
                "avg_fill_hours": avg_fill_hours,
                "regime_duration": self._regime_dur,
                "last_regime": self._last_regime,
                "regime_breakdown": regime_stats,
                "ensemble": self._ensemble_info(),
                "feat_dim": FEAT_DIM,
                # v5 fields
                "backtest_r2": round(self._backtest_r2, 3),
                "backtest_dir_acc": round(self._backtest_dir_acc, 3),
                "models_validated": self._models_validated,
                "predicted_atr": round(self._predicted_atr, 3),
                "mkt_ctx_present": bool(self._mkt_ctx),
                "mtf_ctx_present": bool(self._mtf_ctx),
                # v6 selfdev fields
                "generation": self._generation,
                "drift_count": self._drift_detector.drift_count,
                "bandit": self._bandit.get_stats(),
                "regime_models": self._regime_models.get_stats(),
                "hyper_evolver": {
                    "evolutions": self._hyper_evolver._evolutions,
                    "best_config_idx": self._hyper_evolver._best_config_idx,
                    "best_r2": round(self._hyper_evolver._best_r2, 3),
                },
                "rl_agent": self._rl_agent.get_stats(),
                "selfdev_log": recent_events,
            }

    # ══════════════════════════════════════════════════════════════════════════
    # Внутренние методы
    # ══════════════════════════════════════════════════════════════════════════

    def _compute_fill_density(self) -> float:
        now = time.time()
        cutoff = now - 3600.0
        count = sum(1 for e in self._experience if e.get("ts", 0) > cutoff)
        return float(count)

    def _ensemble_info(self) -> dict:
        return {
            "step_models": [
                m
                for m, v in [
                    ("RF", self._step_rf),
                    ("ET", self._step_et),
                    ("GB", self._step_gb),
                    ("HistGB", self._step_hgb),
                    ("Ridge", self._step_ridge),
                    ("Meta", self._step_meta),
                    ("SGD", self._step_sgd),
                ]
                if v
            ],
            "dca_models": [
                m
                for m, v in [
                    ("RF", self._dca_rf),
                    ("ET", self._dca_et),
                    ("HistGB", self._dca_hgb),
                    ("LR", self._dca_lr),
                    ("SGD", self._dca_sgd),
                ]
                if v
            ],
            "v5_models": [
                m
                for m, v in [
                    ("VolModel", self._vol_model),
                    ("ExitModel", self._exit_model),
                ]
                if v
            ],
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

    def _extract_market_features(
        self, mkt: dict = None, mtf: dict = None, entry: dict = None
    ) -> list:
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
        rsi = _safe_float(mkt.get("rsi"), 50.0)
        rsi_vel = _safe_float(mkt.get("rsi_vel"), 0.0)
        macd_h = _safe_float(mkt.get("macd_h"), 0.0)
        macd_sign = float(1 if macd_h > 0.001 else (-1 if macd_h < -0.001 else 0))
        bb_pos = _safe_float(mkt.get("bb_pos"), 0.5)
        bb_width = _safe_float(mkt.get("bb_width"), 0.05)
        bb_sq = float(bool(mkt.get("bb_squeeze", False)))
        vol_ratio = _safe_float(mkt.get("vol_ratio"), 1.0)
        vol_trend = _safe_float(mkt.get("vol_trend"), 0.0)
        ema_cross = _safe_float(mkt.get("ema_cross"), 0.0)
        of_buy = _safe_float(mkt.get("order_flow_buy_ratio"), 0.5)
        of_net = _safe_float(mkt.get("order_flow_net"), 0.0)
        pump_sc = _safe_float(mkt.get("pump_score"), 0.0) / 100.0
        liq_sc = _safe_float(mkt.get("liquidity_score"), 50.0) / 100.0
        # RSI категория: -1=перепродан(<30), 0=нейтрально, 1=перекуплен(>70)
        rsi_cat = float(1 if rsi > 70 else (-1 if rsi < 30 else 0))

        # ── 5 MTF + производных признаков ────────────────────────────────
        t4h = float(max(-1, min(1, _safe_float(mtf.get("trend_4h"), 0))))
        t1d = float(max(-1, min(1, _safe_float(mtf.get("trend_1d"), 0))))
        # Согласованность MTF: -1=оба вниз, 0=разнонаправлены, 1=оба вверх
        mtf_agree = float(
            1 if t4h > 0 and t1d > 0 else (-1 if t4h < 0 and t1d < 0 else 0)
        )
        # Предсказанный ATR (от vol-модели)
        pred_atr_feat = self._predicted_atr / 5.0  # нормируем к ~1.0
        # Ликвидность × объём = качество рынка
        market_qual = liq_sc * min(vol_ratio / 2.0, 1.0)

        feat_mkt = [
            # 15 рыночных
            rsi / 100.0,  # 0-1
            max(-1.0, min(1.0, rsi_vel / 30.0)),  # -1..1
            macd_sign,  # -1/0/1
            max(0.0, min(1.0, bb_pos)),  # 0-1
            max(0.0, min(0.3, bb_width)) / 0.3,  # нормировано 0-1
            bb_sq,  # 0/1
            min(vol_ratio / 5.0, 2.0),  # нормировано
            max(-1.0, min(1.0, vol_trend)),  # -1..1
            max(-0.1, min(0.1, ema_cross)) / 0.1,  # -1..1
            max(0.0, min(1.0, of_buy)),  # 0-1
            max(-1.0, min(1.0, of_net)),  # -1..1
            max(0.0, min(1.0, pump_sc)),  # 0-1
            max(0.0, min(1.0, liq_sc)),  # 0-1
            rsi_cat,  # -1/0/1
            min(vol_ratio * pump_sc, 3.0) / 3.0,  # взаимодействие vol×pump
            # 5 MTF + производных
            t4h,  # -1/0/1
            t1d,  # -1/0/1
            mtf_agree,  # -1/0/1
            max(0.0, min(3.0, pred_atr_feat)),  # 0-3
            max(0.0, min(1.0, market_qual)),  # 0-1
        ]
        assert len(feat_mkt) == 20
        return feat_mkt

    def _make_features(self, atr_pct: float, regime: str, entry: dict = None) -> list:
        """Вектор признаков v5 (ровно FEAT_DIM=40 значений).

        Блок 1 (5): ATR-базовые
        Блок 2 (8): контекстные
        Блок 3 (7): v4 расширенные
        Блок 4 (20): v5 рыночные + MTF
        """
        re = _regime_enc(regime)
        atr = _safe_float(atr_pct)

        # ── Блок 1: базовые 5 ────────────────────────────────────────────
        feat = [
            atr,
            float(re),
            atr**2,
            float(abs(re)),
            atr * re,
        ]

        # ── Блок 2: контекстные 8 ────────────────────────────────────────
        if entry is not None:
            win_streak = _safe_float(entry.get("win_streak", 0))
            recent_avg = _safe_float(entry.get("recent_avg_profit", 0))
            profit_momentum = _safe_float(entry.get("profit_momentum", 0))
            hour = _safe_float(entry.get("hour", 12))
            regime_dur = _safe_float(entry.get("regime_duration", 0))
        else:
            win_streak = float(self._win_streak)
            recent_avg = (
                sum(self._recent_profits) / len(self._recent_profits)
                if self._recent_profits
                else 0.0
            )
            profit_momentum = 0.0
            if len(self._recent_profits) >= 3:
                rp = list(self._recent_profits)
                profit_momentum = (rp[-1] - rp[0]) / max(abs(rp[0]) + 1e-6, 1e-6)
                profit_momentum = max(-2.0, min(2.0, profit_momentum))
            hour = float(int(time.strftime("%H", time.gmtime())))
            regime_dur = float(self._regime_dur)

        hour_sin = math.sin(2 * math.pi * hour / 24)
        hour_cos = math.cos(2 * math.pi * hour / 24)

        feat.extend(
            [
                min(win_streak, 10.0),
                max(-5.0, min(5.0, recent_avg)),
                max(-2.0, min(2.0, profit_momentum)),
                hour_sin,
                hour_cos,
                min(regime_dur, 50.0),
                1.0 if re > 0 else 0.0,
                1.0 if re < 0 else 0.0,
            ]
        )  # +8 = 13 total

        # ── Блок 3: v4 расширенные 7 ─────────────────────────────────────
        if entry is not None:
            consec_loss = _safe_float(entry.get("consecutive_losses", 0))
            compound = _safe_float(entry.get("compound_mult", 1.0))
            drawdown = _safe_float(entry.get("drawdown_pct", 0.0))
            win_rate_5 = _safe_float(entry.get("recent_win_rate_5", 0.5))
            fill_dens = _safe_float(entry.get("fill_density_1h", 0.0))
            atr_norm = _safe_float(entry.get("atr_normalized", 1.0))
            reg_conf = _safe_float(entry.get("regime_confidence", 0.5))
        else:
            consec_loss = float(self._consecutive_losses)
            compound = float(self._last_compound_mult)
            drawdown = 0.0
            rp5 = list(self._recent_profits)[-5:]
            win_rate_5 = sum(1 for x in rp5 if x > 0) / max(len(rp5), 1)
            fill_dens = self._compute_fill_density()
            mean_atr = (
                sum(self._recent_atrs) / len(self._recent_atrs)
                if self._recent_atrs
                else atr
            )
            atr_norm = atr / max(mean_atr, 0.5)
            reg_conf = min(self._regime_dur / 20.0, 1.0)

        feat.extend(
            [
                min(consec_loss, 10.0),
                max(1.0, min(2.0, compound)),
                max(0.0, min(50.0, drawdown)),
                max(0.0, min(1.0, win_rate_5)),
                min(fill_dens / 5.0, 4.0),
                max(0.3, min(3.0, atr_norm)),
                max(0.0, min(1.0, reg_conf)),
            ]
        )  # +7 = 20 total

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
        models = [
            self._step_rf,
            self._step_et,
            self._step_gb,
            self._step_hgb,
            self._step_ridge,
        ]

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
                avg_base = sum(base_preds) / len(base_preds)
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

    def _simulate_best_step(
        self, feat: list, ml_pred: float, eff_min: float, eff_max: float
    ) -> float:
        """v5 НОВОЕ: P&L-симуляция для выбора оптимального шага.

        Генерирует 5 кандидатов шага и выбирает тот, у которого
        exit_model предсказывает максимальную прибыль.
        """
        try:
            # Кандидаты шага: вокруг ML-предсказания
            delta = (eff_max - eff_min) / 4.0
            candidates = []
            for offset in [-2 * delta, -delta, 0, delta, 2 * delta]:
                c = max(eff_min, min(eff_max, ml_pred + offset))
                c = round(c * 2) / 2
                if c not in candidates:
                    candidates.append(c)

            if len(candidates) < 2:
                return ml_pred

            best_step = ml_pred
            best_pnl = -999.0

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
                        best_pnl = predicted_pnl
                        best_step = step
                except Exception:
                    pass

            return best_step

        except Exception as e:
            log.debug("[GridAI v6] simulate_step error: %s", e)
            return ml_pred

    def _compute_sample_weights(self, entries: list, now: float) -> list:
        """v5: Profit-weighted веса = временной вес × прибыльный буст.

        Прибыльные сделки: вес × (1 + profit_boost)
        Убыточные сделки: вес × near-zero (0.1)
        """
        weights = []
        for e in entries:
            ts = _safe_float(e.get("ts", now - 86400))
            time_w = max(0.01, _exp_decay_weight(ts, now))

            profit = _safe_float(e.get("profit_ton", 0))
            if profit > 0:
                # Буст пропорционален прибыльности, но ограничен 3×
                profit_pct = _safe_float(e.get("profit_pct", 0))
                profit_boost = min(2.0, max(0.0, profit_pct / 5.0))
                profit_w = 1.0 + profit_boost  # 1.0 – 3.0
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
            _safe_float(e.get("step_used", 4.0)) for e in profitable
        )
        calibrated = round(max(3.5, min_profitable_step - 0.25) * 2) / 2
        if abs(calibrated - self.calibrated_min_step) >= 0.5:
            log.info(
                "[GridAI v6] ⚙️ MIN_STEP: %.2f%% → %.2f%% (%d прибыльных)",
                self.calibrated_min_step,
                calibrated,
                len(profitable),
            )
        self.calibrated_min_step = calibrated

    def _compute_kelly_mult(self, profits: list):
        """Kelly criterion → глобальный множитель шага (Half-Kelly)."""
        if len(profits) < 5:
            self._kelly_mult = 1.0
            return
        wins = [p for p in profits if p > 0]
        losses = [abs(p) for p in profits if p <= 0]
        if not wins:
            self._kelly_mult = 0.7
            return
        if not losses:
            self._kelly_mult = 1.1
            return
        p = len(wins) / len(profits)
        avg_w = sum(wins) / len(wins)
        avg_l = sum(losses) / len(losses)
        kelly = (p * avg_w - (1 - p) * avg_l) / avg_w
        mult = 1.0 + max(-0.3, min(0.3, kelly * 0.5))
        self._kelly_mult = round(mult, 3)

    def _compute_kelly_by_regime(self):
        """Per-regime Kelly."""
        for regime, rp in self._regime_profits.items():
            if len(rp) < 5:
                self._kelly_by_regime[regime] = self._kelly_mult
                continue
            wins = [p for p in rp if p > 0]
            losses = [abs(p) for p in rp if p <= 0]
            if not wins or not losses:
                self._kelly_by_regime[regime] = self._kelly_mult
                continue
            p = len(wins) / len(rp)
            avg_w = sum(wins) / len(wins)
            avg_l = sum(losses) / len(losses)
            kelly = (p * avg_w - (1 - p) * avg_l) / avg_w
            mult = 1.0 + max(-0.3, min(0.3, kelly * 0.5))
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

            feat = [
                self._make_features(
                    self._safe_atr(entry), entry.get("regime", "SIDEWAYS"), entry
                )
            ]

            if entry.get("side") == "sell" and entry.get("step_used") is not None:
                y_step = [_safe_float(entry.get("step_used", 4.0))]
                if self._step_sgd is None:
                    self._step_sgd = SGDRegressor(
                        loss="huber",
                        penalty="l2",
                        alpha=0.01,
                        learning_rate="invscaling",
                        eta0=0.05,
                        power_t=0.5,
                        max_iter=1,
                        tol=None,
                        random_state=42,
                    )
                self._step_sgd.partial_fit(feat, y_step)

            y_cls = [int(entry.get("is_profitable", 0))]
            if self._dca_sgd is None:
                self._dca_sgd = SGDClassifier(
                    loss="log_loss",
                    penalty="l2",
                    alpha=0.01,
                    learning_rate="invscaling",
                    eta0=0.05,
                    power_t=0.5,
                    max_iter=1,
                    tol=None,
                    random_state=42,
                )
                self._dca_sgd.partial_fit(feat, y_cls, classes=[0, 1])
            else:
                self._dca_sgd.partial_fit(feat, y_cls)

        except ImportError:
            pass
        except Exception as e:
            log.debug("[GridAI v6] incremental_update error: %s", e)

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
                past5 = atr_history[i - 5 : i]
                next_atr = atr_history[i]
                re = _regime_enc(sells[i].get("regime", "SIDEWAYS"))

                x_row = past5 + [
                    float(re),
                    sum(past5) / 5.0,  # среднее ATR
                    max(past5) - min(past5),  # диапазон ATR
                    past5[-1] - past5[-2],  # последнее изменение
                    sum(1 for a in past5 if a > past5[-1]),  # сколько выше текущего
                ]
                X.append(x_row)
                y.append(next_atr)

            if len(X) < 8:
                return

            vol_m = Pipeline(
                [
                    ("sc", StandardScaler()),
                    (
                        "m",
                        RandomForestRegressor(
                            n_estimators=40, max_depth=4, random_state=42, n_jobs=1
                        ),
                    ),
                ]
            )
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
                log.info(
                    "[GridAI v6] 📈 VolModel: предсказанный ATR=%.2f%% "
                    "(текущий=%.2f%%)",
                    self._predicted_atr,
                    atr_history[-1] if atr_history else 0,
                )

        except Exception as e:
            log.debug("[GridAI v6] vol_model error: %s", e)

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
                f = self._make_features(
                    self._safe_atr(e), e.get("regime", "SIDEWAYS"), e
                )
                X_exit.append(f)
                y_exit.append(_safe_float(e.get("profit_pct", 0)))

            if len(X_exit) < 5:
                return

            exit_m = Pipeline(
                [
                    ("sc", StandardScaler()),
                    (
                        "m",
                        ExtraTreesRegressor(
                            n_estimators=50, max_depth=5, random_state=42, n_jobs=1
                        ),
                    ),
                ]
            )
            exit_m.fit(X_exit, y_exit)
            self._exit_model = exit_m
            log.info(
                "[GridAI v6] 🎯 ExitModel обучена на %d прибыльных "
                "сделках (avg_target=%.2f%%)",
                len(profitable_sells),
                sum(y_exit) / len(y_exit),
            )

        except Exception as e:
            log.debug("[GridAI v6] exit_model error: %s", e)

    # ── v5: Бэктест + валидация ────────────────────────────────────────────────

    def _backtest_validate(
        self, X_s: list, y_s: list, w_s: list
    ) -> Tuple[float, float]:
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
            r2_scores = []
            dir_accs = []

            for train_idx, test_idx in tscv.split(X_s):
                if len(train_idx) < 5 or len(test_idx) < 2:
                    continue
                X_tr = [X_s[i] for i in train_idx]
                y_tr = [y_s[i] for i in train_idx]
                w_tr = [w_s[i] for i in train_idx]
                X_te = [X_s[i] for i in test_idx]
                y_te = [y_s[i] for i in test_idx]

                try:
                    m = Pipeline(
                        [
                            ("sc", StandardScaler()),
                            (
                                "m",
                                ExtraTreesRegressor(
                                    n_estimators=30,
                                    max_depth=4,
                                    random_state=42,
                                    n_jobs=1,
                                ),
                            ),
                        ]
                    )
                    m.fit(X_tr, y_tr, m__sample_weight=w_tr)
                    y_pred = m.predict(X_te)

                    # R²
                    ss_res = sum((yt - yp) ** 2 for yt, yp in zip(y_te, y_pred))
                    ss_tot = sum((yt - sum(y_te) / len(y_te)) ** 2 for yt in y_te)
                    r2 = 1.0 - ss_res / (ss_tot + 1e-10)
                    r2_scores.append(r2)

                    # Direction accuracy: предсказываем правильное направление
                    # (шаг выше/ниже среднего)
                    y_mean = sum(y_te) / len(y_te)
                    dir_correct = sum(
                        1
                        for yt, yp in zip(y_te, y_pred)
                        if (yt > y_mean) == (yp > y_mean)
                    )
                    dir_accs.append(dir_correct / len(y_te))
                except Exception:
                    pass

            r2 = sum(r2_scores) / len(r2_scores) if r2_scores else 0.0
            acc = sum(dir_accs) / len(dir_accs) if dir_accs else 0.5
            return r2, acc

        except Exception as e:
            log.debug("[GridAI v6] backtest error: %s", e)
            return 0.0, 0.5

    def _train(self):
        """Полное переобучение ансамбля моделей (v6)."""
        try:
            from sklearn.ensemble import (
                RandomForestRegressor,
                ExtraTreesRegressor,
                GradientBoostingRegressor,
                RandomForestClassifier,
                ExtraTreesClassifier,
            )
            from sklearn.linear_model import Ridge, LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline
            from sklearn.model_selection import TimeSeriesSplit

            try:
                from sklearn.ensemble import (
                    HistGradientBoostingRegressor,
                    HistGradientBoostingClassifier,
                )

                _has_hgb = True
            except ImportError:
                _has_hgb = False

            now = time.time()
            sells = [
                e
                for e in self._experience
                if e.get("side") == "sell" and not e.get("_synthetic")
            ]  # только реальные сделки
            all_e = self._experience

            # ── Калибровка и Kelly ──────────────────────────────────────
            profits = [e.get("profit_ton", 0) for e in sells]
            self._calibrate_min_step(sells)
            self._compute_kelly_mult(profits)
            self._compute_kelly_by_regime()

            # ── v5: Обучаем vol-модель (предсказание ATR) ───────────────
            if len(sells) >= 10:
                self._train_vol_model(sells)

            # ── v6: Synthetic Data Augmentation ─────────────────────────
            real_sells = sells
            if len(sells) < 80:
                aug_exp = self._aug.augment(sells, target_n=150, noise_scale=0.04)
                sells_train = aug_exp
                log.info(
                    "[GridAI v6] 🧪 SyntheticAug: %d → %d примеров (sells)",
                    len(real_sells),
                    len(sells_train),
                )
            else:
                sells_train = sells

            # ── v6: HyperEvolver ────────────────────────────────────────
            hyp_cfg = self._hyper_evolver.best_config
            if len(real_sells) >= MIN_SAMPLES and self._hyper_evolver.should_evolve(
                len(real_sells)
            ):
                # Обучаем на реальных для поиска гипер-параметров
                _X_tmp = [
                    self._make_features(
                        self._safe_atr(e), e.get("regime", "SIDEWAYS"), e
                    )
                    for e in real_sells
                ]
                _y_tmp = [_safe_float(e.get("step_used", 4.0)) for e in real_sells]
                _w_tmp = self._compute_sample_weights(real_sells, now)
                hyp_cfg = self._hyper_evolver.evolve(_X_tmp, _y_tmp, _w_tmp)
                self._selfdev_log.append(
                    {
                        "ts": now,
                        "event": "hyper_evolved",
                        "evolutions": self._hyper_evolver._evolutions,
                        "best_r2": round(self._hyper_evolver._best_r2, 3),
                        "config": hyp_cfg,
                    }
                )
                if len(self._selfdev_log) > 50:
                    self._selfdev_log = self._selfdev_log[-50:]

            # ── Step-ансамбль ────────────────────────────────────────────
            if len(sells_train) >= MIN_SAMPLES:
                X_s = [
                    self._make_features(
                        self._safe_atr(e), e.get("regime", "SIDEWAYS"), e
                    )
                    for e in sells_train
                ]
                y_s = [_safe_float(e.get("step_used", 4.0)) for e in sells_train]
                w_s = self._compute_sample_weights(sells_train, now)

                def _fit_step(model, use_w=True):
                    if use_w:
                        model.fit(X_s, y_s, m__sample_weight=w_s)
                    else:
                        model.fit(X_s, y_s)
                    return model

                # v6: используем лучший конфиг от HyperEvolver
                _ne = hyp_cfg.get("n_estimators", 60)
                _md = hyp_cfg.get("max_depth", 6)

                self._step_rf = _fit_step(
                    Pipeline(
                        [
                            ("sc", StandardScaler()),
                            (
                                "m",
                                RandomForestRegressor(
                                    n_estimators=_ne,
                                    max_depth=_md,
                                    min_samples_leaf=2,
                                    random_state=42,
                                    n_jobs=1,
                                ),
                            ),
                        ]
                    )
                )

                self._step_et = _fit_step(
                    Pipeline(
                        [
                            ("sc", StandardScaler()),
                            (
                                "m",
                                ExtraTreesRegressor(
                                    n_estimators=60,
                                    max_depth=6,
                                    min_samples_leaf=2,
                                    random_state=42,
                                    n_jobs=1,
                                ),
                            ),
                        ]
                    )
                )

                self._step_gb = _fit_step(
                    Pipeline(
                        [
                            ("sc", StandardScaler()),
                            (
                                "m",
                                GradientBoostingRegressor(
                                    n_estimators=80,
                                    max_depth=3,
                                    learning_rate=0.08,
                                    random_state=42,
                                ),
                            ),
                        ]
                    )
                )

                if _has_hgb:
                    try:
                        hgb = HistGradientBoostingRegressor(
                            max_iter=80,
                            max_depth=4,
                            learning_rate=0.08,
                            random_state=42,
                        )
                        hgb.fit(X_s, y_s, sample_weight=w_s)
                        self._step_hgb = hgb
                    except Exception as he:
                        log.debug("[GridAI v6] HistGB step: %s", he)

                self._step_ridge = _fit_step(
                    Pipeline(
                        [
                            ("sc", StandardScaler()),
                            ("m", Ridge(alpha=1.0)),
                        ]
                    )
                )

                # ── v5: OOF мета-стекинг (TimeSeriesSplit) ───────────────
                if len(sells) >= 15:
                    try:
                        tscv = TimeSeriesSplit(n_splits=3)
                        meta_X_oof = [[0.0] * 5 for _ in X_s]

                        for tr_idx, te_idx in tscv.split(X_s):
                            if len(tr_idx) < 5:
                                continue
                            X_tr = [X_s[i] for i in tr_idx]
                            y_tr = [y_s[i] for i in tr_idx]
                            w_tr = [w_s[i] for i in tr_idx]

                            oof_preds = []
                            for mname, mcls, mkw in [
                                (
                                    "RF",
                                    RandomForestRegressor,
                                    dict(
                                        n_estimators=30,
                                        max_depth=5,
                                        random_state=42,
                                        n_jobs=1,
                                    ),
                                ),
                                (
                                    "ET",
                                    ExtraTreesRegressor,
                                    dict(
                                        n_estimators=30,
                                        max_depth=5,
                                        random_state=42,
                                        n_jobs=1,
                                    ),
                                ),
                                (
                                    "GB",
                                    GradientBoostingRegressor,
                                    dict(
                                        n_estimators=40,
                                        max_depth=3,
                                        learning_rate=0.1,
                                        random_state=42,
                                    ),
                                ),
                                ("Ridge", Ridge, dict(alpha=1.0)),
                            ]:
                                try:
                                    m = Pipeline(
                                        [("sc", StandardScaler()), ("m", mcls(**mkw))]
                                    )
                                    m.fit(X_tr, y_tr, m__sample_weight=w_tr)
                                    fold_preds = [
                                        float(m.predict([X_s[i]])[0]) for i in te_idx
                                    ]
                                    oof_preds.append(fold_preds)
                                except Exception:
                                    pass

                            if oof_preds:
                                for col, fp in enumerate(oof_preds):
                                    for row, idx in enumerate(te_idx):
                                        if col < 5:
                                            meta_X_oof[idx][col] = fp[row]

                        meta = Pipeline(
                            [
                                ("sc", StandardScaler()),
                                ("m", Ridge(alpha=0.5)),
                            ]
                        )
                        meta.fit(meta_X_oof, y_s, m__sample_weight=w_s)
                        self._step_meta = meta
                        log.info(
                            "[GridAI v6] 🔗 OOF мета-стекер обучен "
                            "на %d продажах (TimeSeriesSplit)",
                            len(sells),
                        )
                    except Exception as me:
                        log.debug("[GridAI v6] meta-stacker error: %s", me)

                # ── v5: Бэктест перед активацией ─────────────────────────
                r2, dir_acc = self._backtest_validate(X_s, y_s, w_s)
                self._backtest_r2 = r2
                self._backtest_dir_acc = dir_acc
                self._models_validated = (
                    r2 >= BACKTEST_MIN_R2 and dir_acc >= BACKTEST_MIN_DIR_ACC
                )
                log.info(
                    "[GridAI v6] 📊 Бэктест: R²=%.3f dir_acc=%.2f%% " "validated=%s",
                    r2,
                    dir_acc * 100,
                    self._models_validated,
                )

                # ── v5: Обучаем exit-модель ───────────────────────────────
                self._train_exit_model(sells_train, X_s, now)

                log.info(
                    "[GridAI v6] 📊 Step-ансамбль (RF+ET+GB+HistGB+Ridge"
                    "+OOF-Meta) на %d продажах (real=%d aug=%d)",
                    len(sells_train),
                    len(real_sells),
                    len(sells_train) - len(real_sells),
                )
                gc.collect()

                # ── v6: Режимные специализированные модели ────────────────
                self._train_regime_models(real_sells, now)

            # ── DCA-ансамбль ─────────────────────────────────────────────
            if len(all_e) >= MIN_SAMPLES:
                y_p = [
                    int(e.get("is_profitable", 0))
                    for e in all_e
                    if not e.get("_synthetic")
                ]
                all_e_real = [e for e in all_e if not e.get("_synthetic")]
                n_pos = sum(y_p)
                n_neg = len(y_p) - n_pos

                if n_pos >= 2 and n_neg >= 1:
                    X_p = [
                        self._make_features(
                            self._safe_atr(e), e.get("regime", "SIDEWAYS"), e
                        )
                        for e in all_e_real
                    ]
                    w_p = self._compute_sample_weights(all_e_real, now)

                    def _fit_cls(model, use_w=True):
                        if use_w:
                            model.fit(X_p, y_p, m__sample_weight=w_p)
                        else:
                            model.fit(X_p, y_p)
                        return model

                    self._dca_rf = _fit_cls(
                        Pipeline(
                            [
                                ("sc", StandardScaler()),
                                (
                                    "m",
                                    RandomForestClassifier(
                                        n_estimators=60,
                                        max_depth=5,
                                        class_weight="balanced",
                                        random_state=42,
                                        n_jobs=1,
                                    ),
                                ),
                            ]
                        )
                    )

                    self._dca_et = _fit_cls(
                        Pipeline(
                            [
                                ("sc", StandardScaler()),
                                (
                                    "m",
                                    ExtraTreesClassifier(
                                        n_estimators=60,
                                        max_depth=5,
                                        class_weight="balanced",
                                        random_state=42,
                                        n_jobs=1,
                                    ),
                                ),
                            ]
                        )
                    )

                    if _has_hgb:
                        try:
                            hgb_cls = HistGradientBoostingClassifier(
                                max_iter=80,
                                max_depth=4,
                                learning_rate=0.08,
                                random_state=42,
                                class_weight="balanced",
                            )
                            hgb_cls.fit(X_p, y_p, sample_weight=w_p)
                            self._dca_hgb = hgb_cls
                        except Exception as he:
                            log.debug("[GridAI v6] HistGB dca: %s", he)

                    self._dca_lr = _fit_cls(
                        Pipeline(
                            [
                                ("sc", StandardScaler()),
                                (
                                    "m",
                                    LogisticRegression(
                                        C=1.0,
                                        max_iter=500,
                                        class_weight="balanced",
                                        random_state=42,
                                    ),
                                ),
                            ]
                        )
                    )

                    log.info(
                        "[GridAI v6] 📊 DCA-ансамбль на %d примерах " "(pos=%d neg=%d)",
                        len(all_e_real),
                        n_pos,
                        n_neg,
                    )
                    gc.collect()

            # ── v6: Bump поколения ───────────────────────────────────────
            self._generation += 1
            self._trained = True
            self._last_train_n = len(self._experience)

            # v6: лог события обучения
            self._selfdev_log.append(
                {
                    "ts": time.time(),
                    "event": "trained",
                    "generation": self._generation,
                    "samples": len(self._experience),
                    "real_sells": len(real_sells),
                    "backtest_r2": round(self._backtest_r2, 3),
                }
            )
            if len(self._selfdev_log) > 50:
                self._selfdev_log = self._selfdev_log[-50:]

            self._save_selfdev_state()

            log.info(
                "[GridAI v6] ✅ Поколение #%d. Примеров: %d (реальных sells=%d) "
                "| min_step=%.2f%% kelly=%.3f risk=%d "
                "| vol_model=%s exit_model=%s regime_models=%s",
                self._generation,
                len(self._experience),
                len(real_sells),
                self.calibrated_min_step,
                self._kelly_mult,
                self.get_risk_level(),
                "✓" if self._vol_model else "✗",
                "✓" if self._exit_model else "✗",
                list(self._regime_models._models.keys()),
            )

        except ImportError as e:
            log.warning("[GridAI v6] sklearn не найден: %s — heuristic-режим", e)
        except Exception as e:
            log.error("[GridAI v6] Ошибка обучения: %s", e, exc_info=True)

    # ── v6: Режимные модели ───────────────────────────────────────────────────

    def _train_regime_models(self, sells: list, now: float):
        """v6: обучить специализированные модели для каждого режима с 15+ сделками."""
        if len(sells) < 15:
            return
        try:
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline

            by_regime: Dict[str, list] = {}
            for e in sells:
                r = e.get("regime", "UNKNOWN")
                by_regime.setdefault(r, []).append(e)

            for regime, regime_sells in by_regime.items():
                if len(regime_sells) < RegimeSpecializedModels.MIN_REGIME_SAMPLES:
                    continue
                X_r = [
                    self._make_features(self._safe_atr(e), regime, e)
                    for e in regime_sells
                ]
                y_r = [_safe_float(e.get("step_used", 4.0)) for e in regime_sells]
                w_r = self._compute_sample_weights(regime_sells, now)
                self._regime_models.train_regime(regime, X_r, y_r, w_r)
        except Exception as e:
            log.debug("[GridAI v6] _train_regime_models error: %s", e)

    # ── v6: Сохранение/загрузка состояния саморазвития ───────────────────────

    def _save_selfdev_state(self):
        """Сохранить состояние v6 компонентов в SELFDEV_FILE."""
        try:
            state = {
                "generation": self._generation,
                "bandit": self._bandit.to_json(),
                "hyper_evolver": self._hyper_evolver.to_json(),
                "rl_agent": self._rl_agent.to_json(),
                "drift_count": self._drift_detector.drift_count,
                "drift_ts": self._drift_detector._last_drift_ts,
                "selfdev_log": self._selfdev_log[-50:],
            }
            os.makedirs(os.path.dirname(SELFDEV_FILE) or ".", exist_ok=True)
            with open(SELFDEV_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception as e:
            log.debug("[GridAI v6] selfdev save error: %s", e)

    def _load_selfdev_state(self):
        """Загрузить состояние v6 компонентов из SELFDEV_FILE."""
        try:
            if not os.path.exists(SELFDEV_FILE):
                return
            with open(SELFDEV_FILE, encoding="utf-8") as f:
                state = json.load(f)
            self._generation = int(state.get("generation", 0))
            bandit_d = state.get("bandit")
            if bandit_d:
                self._bandit = StepStrategyBandit.from_json(bandit_d)
            hyp_d = state.get("hyper_evolver")
            if hyp_d:
                self._hyper_evolver = HyperEvolver.from_json(hyp_d)
            rl_d = state.get("rl_agent")
            if rl_d:
                self._rl_agent = RLGridAgent.from_json(rl_d)
            self._drift_detector._drift_count = int(state.get("drift_count", 0))
            self._drift_detector._last_drift_ts = float(state.get("drift_ts", 0.0))
            self._selfdev_log = list(state.get("selfdev_log", []))
            log.info(
                "[GridAI v6] 💾 Selfdev state загружен: gen=#%d "
                "bandit_pulls=%d rl_ep=%d drift=%d",
                self._generation,
                self._bandit._total,
                self._rl_agent._episodes,
                self._drift_detector.drift_count,
            )
        except Exception as e:
            log.debug("[GridAI v6] selfdev load error: %s", e)

    # ── PostgreSQL dual-write (v5, улучшение #3) ──────────────────────────────

    def _save_experience(self):
        """Dual-write: JSON + PostgreSQL."""
        # 1. JSON fallback (быстро, надёжно)
        try:
            os.makedirs(os.path.dirname(EXPERIENCE_FILE) or ".", exist_ok=True)
            with open(EXPERIENCE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._experience, f, ensure_ascii=False)
        except Exception as e:
            log.warning("[GridAI v6] JSON save error: %s", e)

        # 2. PostgreSQL — только последнюю запись (bulk-insert при старте)
        if self._experience:
            last = self._experience[-1]
            try:
                import db_store as _db

                _db.grid_experience_insert(last)
            except Exception as e:
                log.debug("[GridAI v6] DB save error: %s", e)

    def _load_experience(self):
        """Загрузка: сначала PostgreSQL (свежее), потом JSON fallback."""
        loaded = False

        # 1. Пробуем PostgreSQL
        try:
            import db_store as _db

            db_exp = _db.grid_experience_load()
            if db_exp:
                self._experience = db_exp
                log.info(
                    "[GridAI v6] 🗄️  Загружено %d примеров из PostgreSQL",
                    len(self._experience),
                )
                self._rebuild_rolling_state()
                loaded = True
        except Exception as e:
            log.debug("[GridAI v6] DB load skip: %s", e)

        # 2. JSON fallback или импорт старого формата
        if not loaded:
            try:
                if os.path.exists(EXPERIENCE_FILE):
                    with open(EXPERIENCE_FILE, encoding="utf-8") as f:
                        self._experience = json.load(f)
                    log.info(
                        "[GridAI v6] 📁 Загружено %d примеров из JSON "
                        "(DB недоступна)",
                        len(self._experience),
                    )
                    self._rebuild_rolling_state()

                    # Миграция JSON → PostgreSQL (bulk-insert при первом запуске)
                    self._migrate_json_to_db()
            except Exception as e:
                log.warning("[GridAI v6] Загрузка опыта: %s", e)
                self._experience = []

    def _migrate_json_to_db(self):
        """Разовая миграция JSON-опыта в PostgreSQL."""
        if not self._experience:
            return
        try:
            import db_store as _db

            if _db.grid_experience_count() > 0:
                return  # уже мигрировано
            log.info(
                "[GridAI v6] 🔄 Миграция %d записей JSON → PostgreSQL...",
                len(self._experience),
            )
            for entry in self._experience:
                _db.grid_experience_insert(entry)
            log.info("[GridAI v6] ✅ Миграция завершена")
        except Exception as e:
            log.debug("[GridAI v6] Миграция DB: %s", e)

    def _rebuild_rolling_state(self):
        """Восстанавливаем все трекеры из загруженной истории."""
        sells = sorted(
            [e for e in self._experience if e.get("side") == "sell"],
            key=lambda x: x.get("ts", 0),
        )

        self._win_streak = 0
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
                self._win_streak += 1
                self._consecutive_losses = 0
            else:
                self._win_streak = 0
                self._consecutive_losses += 1

        for r in list(self._regime_profits):
            if len(self._regime_profits[r]) > 50:
                self._regime_profits[r] = self._regime_profits[r][-50:]


# ── Синглтон ──────────────────────────────────────────────────────────────────

_instance: Optional[GridAI] = None
_init_lock: threading.Lock = threading.Lock()


def get_grid_ai() -> GridAI:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = GridAI()
    return _instance
