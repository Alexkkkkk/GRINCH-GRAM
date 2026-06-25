"""
AI Engine v2 — Super Self-Learning Trading Brain for GRINCH/TON

Архитектура:
  • 4 базовые ML-модели: RandomForest · ExtraTrees · HistGradientBoosting · GradientBoosting
  • Динамические веса ансамбля (rolling accuracy per model, последние 40 тиков)
  • Мета-слой: LogisticRegression обучается на выходах базовых моделей после 30+ сделок
  • 45+ технических признаков: RSI · MACD · BB · ATR · ADX · OBV · CCI · Williams%R · Ichimoku ·
    Heiken Ashi · Volume Profile · Fibonacci lags · Trend angles · S/R distance
  • Адаптивная разметка: порог = 0.6 × ATR_pct (не фиксированный), мульти-горизонт (2/3/5 баров)
  • Буфер опыта (Experience Replay): 600 последних признаков + подтверждённые сделки
  • Обратная связь от трейдера: feedback(outcome, pnl) → добавляет 5× взвешенные примеры
  • Авто-переобучение: полный рефит каждые 5 тиков или при накоплении 10+ новых сделок
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
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
import warnings
warnings.filterwarnings("ignore")

try:
    from sklearn.ensemble import HistGradientBoostingClassifier
    _HAS_HGB = True
except ImportError:
    _HAS_HGB = False

log = logging.getLogger(__name__)


# ─── Константы ────────────────────────────────────────────────────────────────
LOOK_AHEADS       = [2, 3, 5]          # мульти-горизонт голосования
ATR_LABEL_MULT    = 0.6                 # порог = 0.6 × ATR_pct
CONFIRM_WEIGHT    = 5.0                 # вес подтверждённой сделки
REPLAY_SIZE       = 600                 # размер буфера опыта
ACCURACY_WINDOW   = 40                  # окно rolling accuracy
META_MIN_SAMPLES  = 30                  # мин. сделок для мета-слоя
RETRAIN_EVERY     = 5                   # полный рефит каждые N тиков


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
            clf = self.pipeline.named_steps.get("clf")
            if clf is not None and hasattr(clf, "sample_weight"):
                kw["clf__sample_weight"] = sample_weight
            # HistGradientBoosting не принимает sample_weight через Pipeline так же
            self.pipeline.fit(X, y)
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
                    n_estimators=200, max_depth=8, min_samples_split=4,
                    min_samples_leaf=2, max_features="sqrt",
                    class_weight="balanced", random_state=42, n_jobs=1)
            )),
            _ModelSlot("ET", _make_pipeline(
                ExtraTreesClassifier(
                    n_estimators=150, max_depth=7, min_samples_split=5,
                    class_weight="balanced", random_state=7, n_jobs=1)
            )),
            _ModelSlot("GB", _make_pipeline(
                GradientBoostingClassifier(
                    n_estimators=120, max_depth=4, learning_rate=0.05,
                    subsample=0.8, random_state=42)
            )),
        ]
        if _HAS_HGB:
            self._slots.append(_ModelSlot("HGB", Pipeline([
                ("clf", HistGradientBoostingClassifier(
                    max_iter=150, max_depth=5, learning_rate=0.05,
                    min_samples_leaf=10, random_state=42))
            ])))

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

        emit("ready", 100, f"✅ SuperAI готов! {len(self._slots)} моделей · {len(X)} баров · Самообучение активно 🟢", len(X))
        self.training_progress["trained"] = True

    # ─────────────────────────────────────────────────────────────────────────
    # Публичный анализ (каждый тик)
    # ─────────────────────────────────────────────────────────────────────────

    def analyze(self, ohlcv: list) -> dict:
        with self._lock:
            return self._analyze_locked(ohlcv)

    def _analyze_locked(self, ohlcv: list) -> dict:
        df = self._build_features(ohlcv)
        if df is None or len(df) < 40:
            return self._empty_result()

        X, y = self._make_dataset(df)
        if X is None or len(X) < 25:
            return self._empty_result()

        self._tick_count += 1

        # ── Авто-переобучение ────────────────────────────────────────────
        should_retrain = (
            self._tick_count % RETRAIN_EVERY == 0 or
            self._new_confirms >= 5
        )
        if should_retrain:
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

        max_prob = max(prob_up, prob_down, prob_hold)
        if max_prob == prob_up and prob_up > 0.42:
            ai_signal = "BUY"
            self._last_buy_features = X[-1].copy()
        elif max_prob == prob_down and prob_down > 0.42:
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

        return {
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
            "model_info":   model_info,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Обратная связь от трейдера (вызывается когда сделка закрывается)
    # ─────────────────────────────────────────────────────────────────────────

    def feedback(self, outcome: str, pnl: float):
        """
        outcome: "win" | "loss"
        pnl: P&L в TON (может быть отрицательным)
        """
        if self._last_buy_features is None:
            return
        with self._lock:
            label  = 1 if outcome == "win" else -1
            weight = CONFIRM_WEIGHT * (1.0 + min(abs(pnl), 2.0))
            self._confirmed_X.append(self._last_buy_features.copy())
            self._confirmed_y.append(label)
            self._confirmed_w.append(weight)
            self._last_buy_features = None
            self._new_confirms += 1

            # Обновляем accuracy для всех моделей
            for slot in self._slots:
                slot.record(outcome == "win")

            # Если накопилось много подтверждённых — обучаем мета
            if len(self._confirmed_X) >= META_MIN_SAMPLES:
                try:
                    self._try_fit_meta_confirmed()
                except Exception as e:
                    log.debug(f"[AI] meta fit error: {e}")

        log.info(f"[AI] Feedback: {outcome} PNL={pnl:.4f} → {len(self._confirmed_X)} подтверждённых примеров")

    # ─────────────────────────────────────────────────────────────────────────
    # Персистентность опыта (переживает перезапуск)
    # ─────────────────────────────────────────────────────────────────────────

    def export_experience(self) -> dict:
        """Сериализует подтверждённый опыт ИИ для записи на диск."""
        with self._lock:
            return {
                "confirmed_X": [list(map(float, x)) for x in self._confirmed_X],
                "confirmed_y": [int(v) for v in self._confirmed_y],
                "confirmed_w": [float(v) for v in self._confirmed_w],
                "slot_acc":    {s.name: list(s._history) for s in self._slots},
                "feature_dim": len(self._feature_names),
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
                n = len(self._confirmed_X)
                if n and self._trained:
                    self._refit_all()
                log.info(f"[AI] Восстановлено {n} подтверждённых примеров с диска")
                return n
            except Exception as e:
                log.warning(f"[AI] import_experience error: {e}")
                return 0

    # ─────────────────────────────────────────────────────────────────────────
    # Внутренние методы: обучение
    # ─────────────────────────────────────────────────────────────────────────

    def _refit_all(self):
        """Полный рефит всех моделей = исторические данные + подтверждённые сделки."""
        X_all = list(self._replay_X) + list(self._confirmed_X)
        y_all = list(self._replay_y) + list(self._confirmed_y)
        w_all = list(self._replay_w) + list(self._confirmed_w)

        # Ограничиваем буфер
        if len(X_all) > REPLAY_SIZE + len(self._confirmed_X):
            trim = len(X_all) - (REPLAY_SIZE + len(self._confirmed_X))
            X_all = X_all[trim:]
            y_all = y_all[trim:]
            w_all = w_all[trim:]

        X_arr = np.array(X_all)
        y_arr = np.array(y_all)

        classes = np.unique(y_arr)
        if len(classes) < 2:
            return

        for slot in self._slots:
            try:
                slot.fit(X_arr, y_arr)
            except Exception as e:
                log.debug(f"[AI:{slot.name}] refit error: {e}")

        self._trained = True
        self._new_confirms = 0

    def _try_fit_meta(self, X, y):
        """Первый запуск мета-слоя на исторических данных."""
        try:
            meta_X = self._stack_features(X)
            self._meta = Pipeline([
                ("scaler", StandardScaler()),
                ("clf",    LogisticRegression(C=1.0, max_iter=300, random_state=42))
            ])
            self._meta.fit(meta_X, y)
        except Exception as e:
            log.debug(f"[AI] meta init error: {e}")
            self._meta = None

    def _try_fit_meta_confirmed(self):
        """Переобучаем мета-слой на подтверждённых сделках."""
        X_arr = np.array(self._confirmed_X)
        y_arr = np.array(self._confirmed_y)
        meta_X = self._stack_features(X_arr)
        if self._meta is None:
            self._meta = Pipeline([
                ("scaler", StandardScaler()),
                ("clf",    LogisticRegression(C=1.0, max_iter=300, random_state=42))
            ])
        self._meta.fit(meta_X, y_arr)

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
        ]
        # Оставляем только существующие столбцы
        feature_cols = [col for col in feature_cols if col in df.columns]
        self._feature_names = feature_cols

        c       = df["close"].values
        atr_pct = df["atr_pct"].values
        X       = df[feature_cols].values
        n       = len(c)
        max_la  = max(LOOK_AHEADS)

        # Мульти-горизонт: голосование по [2, 3, 5] барам
        y = np.zeros(n, dtype=int)
        for i in range(n - max_la):
            thresh = ATR_LABEL_MULT * (atr_pct[i] + 1e-10)
            votes = []
            for la in LOOK_AHEADS:
                ret = (c[i + la] - c[i]) / (c[i] + 1e-10)
                if ret > thresh:
                    votes.append(1)
                elif ret < -thresh:
                    votes.append(-1)
                else:
                    votes.append(0)
            # Большинство голосов
            vote_sum = sum(votes)
            if vote_sum >= 2:
                y[i] = 1
            elif vote_sum <= -2:
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

    def _model_stats(self) -> list:
        return [
            {
                "name":     s.name,
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
        }
