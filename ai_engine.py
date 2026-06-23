import numpy as np
import pandas as pd
import threading
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
import warnings
warnings.filterwarnings("ignore")

class AIEngine:
    """
    AI движок: ансамбль ML-моделей для предсказания цены и анализа рынка.
    - RandomForest + GradientBoosting ансамбль
    - Детектор рыночного режима (тренд / боковик / волатильность)
    - Детектор паттернов свечей
    - Уровни поддержки/сопротивления
    - Оценка уверенности + объяснение решения
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._trained = False
        self._feature_names = []
        self._build_models()

    def _build_models(self):
        self._rf = Pipeline([
            ("scaler", StandardScaler()),
            ("clf", RandomForestClassifier(n_estimators=100, max_depth=6,
                                           min_samples_split=5, random_state=42,
                                           class_weight="balanced", n_jobs=1))
        ])
        self._gb = Pipeline([
            ("scaler", StandardScaler()),
            ("clf", GradientBoostingClassifier(n_estimators=80, max_depth=4,
                                                learning_rate=0.05, random_state=42))
        ])

    # ──────────────────────────────────────────
    # Публичный метод (thread-safe)
    # ──────────────────────────────────────────
    def analyze(self, ohlcv: list) -> dict:
        with self._lock:
            return self._analyze_locked(ohlcv)

    def _analyze_locked(self, ohlcv: list) -> dict:
        df = self._build_features(ohlcv)
        if df is None or len(df) < 40:
            return self._empty_result()

        X, y = self._make_dataset(df)
        if X is None or len(X) < 30:
            return self._empty_result()

        # Авто-дообучение на каждом тике
        self._fit(X, y)

        # Предсказание для последней точки
        if not self._trained:
            return self._empty_result()

        last_features = X[[-1]]
        try:
            rf_proba = self._rf.predict_proba(last_features)[0]
            gb_proba = self._gb.predict_proba(last_features)[0]
        except Exception:
            self._trained = False
            self._build_models()
            return self._empty_result()
        ensemble  = (rf_proba + gb_proba) / 2

        classes = self._rf.classes_
        idx_up   = list(classes).index(1)  if 1  in classes else None
        idx_down = list(classes).index(-1) if -1 in classes else None
        idx_hold = list(classes).index(0)  if 0  in classes else None

        prob_up   = float(ensemble[idx_up])   if idx_up   is not None else 0.0
        prob_down = float(ensemble[idx_down]) if idx_down is not None else 0.0
        prob_hold = float(ensemble[idx_hold]) if idx_hold is not None else 0.0

        # Сигнал
        max_prob = max(prob_up, prob_down, prob_hold)
        if max_prob == prob_up and prob_up > 0.45:
            ai_signal = "BUY"
        elif max_prob == prob_down and prob_down > 0.45:
            ai_signal = "SELL"
        else:
            ai_signal = "HOLD"

        confidence = round(max_prob * 100, 1)

        # Дополнительные аналитики
        regime    = self._detect_regime(df)
        patterns  = self._detect_candle_patterns(df)
        sr_levels = self._support_resistance(df)
        forecast  = self._price_forecast(df)
        importance = self._feature_importance()
        anomaly   = self._detect_anomaly(df)

        return {
            "ai_signal":   ai_signal,
            "confidence":  confidence,
            "prob_up":     round(prob_up   * 100, 1),
            "prob_down":   round(prob_down * 100, 1),
            "prob_hold":   round(prob_hold * 100, 1),
            "regime":      regime,
            "patterns":    patterns,
            "support_resistance": sr_levels,
            "forecast":    forecast,
            "feature_importance": importance,
            "anomaly":     anomaly,
            "model_trained": self._trained,
            "samples_trained": len(X),
        }

    # ──────────────────────────────────────────
    # Feature engineering
    # ──────────────────────────────────────────
    def _build_features(self, ohlcv):
        if len(ohlcv) < 40:
            return None
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])

        c = df["close"]
        h = df["high"]
        l = df["low"]
        v = df["volume"]

        # Возвраты
        df["ret_1"]  = c.pct_change(1)
        df["ret_3"]  = c.pct_change(3)
        df["ret_5"]  = c.pct_change(5)
        df["ret_10"] = c.pct_change(10)

        # EMA
        for s in [5, 9, 21, 50]:
            df[f"ema_{s}"] = c.ewm(span=s, adjust=False).mean()
        df["ema_cross_9_21"]  = df["ema_9"]  - df["ema_21"]
        df["ema_cross_21_50"] = df["ema_21"] - df["ema_50"]

        # RSI
        delta = c.diff()
        gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
        df["rsi"] = 100 - 100 / (1 + gain / (loss + 1e-10))

        # MACD
        df["macd"]  = c.ewm(12).mean() - c.ewm(26).mean()
        df["macd_s"] = df["macd"].ewm(9).mean()
        df["macd_h"] = df["macd"] - df["macd_s"]

        # Bollinger
        df["bb_mid"]    = c.rolling(20).mean()
        std             = c.rolling(20).std()
        df["bb_up"]     = df["bb_mid"] + 2 * std
        df["bb_lo"]     = df["bb_mid"] - 2 * std
        df["bb_width"]  = (df["bb_up"] - df["bb_lo"]) / (df["bb_mid"] + 1e-10)
        df["bb_pos"]    = (c - df["bb_lo"]) / (df["bb_up"] - df["bb_lo"] + 1e-10)

        # ATR (волатильность)
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        df["atr"]     = tr.rolling(14).mean()
        df["atr_pct"] = df["atr"] / (c + 1e-10)

        # Объём
        df["vol_ma"]   = v.rolling(20).mean()
        df["vol_ratio"] = v / (df["vol_ma"] + 1e-10)

        # Стохастик
        low14  = l.rolling(14).min()
        high14 = h.rolling(14).max()
        df["stoch_k"] = 100 * (c - low14) / (high14 - low14 + 1e-10)
        df["stoch_d"] = df["stoch_k"].rolling(3).mean()

        # Моментум
        df["momentum"] = c - c.shift(10)
        df["roc"]      = c.pct_change(12)

        # Свечные паттерны (числа)
        df["body"]      = (c - df["open"]).abs()
        df["upper_wick"] = h - pd.concat([c, df["open"]], axis=1).max(axis=1)
        df["lower_wick"] = pd.concat([c, df["open"]], axis=1).min(axis=1) - l
        df["bull_candle"] = (c > df["open"]).astype(int)

        df.dropna(inplace=True)
        return df

    def _make_dataset(self, df):
        LOOK_AHEAD = 3
        feature_cols = [
            "ret_1", "ret_3", "ret_5", "ret_10",
            "ema_cross_9_21", "ema_cross_21_50",
            "rsi", "macd_h", "bb_width", "bb_pos",
            "atr_pct", "vol_ratio",
            "stoch_k", "stoch_d", "momentum", "roc",
            "body", "upper_wick", "lower_wick", "bull_candle",
        ]
        self._feature_names = feature_cols

        X = df[feature_cols].values
        c = df["close"].values
        y = np.zeros(len(c), dtype=int)
        for i in range(len(c) - LOOK_AHEAD):
            future_ret = (c[i + LOOK_AHEAD] - c[i]) / (c[i] + 1e-10)
            if future_ret > 0.005:
                y[i] = 1
            elif future_ret < -0.005:
                y[i] = -1
        X = X[:-LOOK_AHEAD]
        y = y[:-LOOK_AHEAD]
        return X, y

    def _fit(self, X, y):
        classes = np.unique(y)
        if len(classes) < 2:
            return
        self._rf.fit(X, y)
        self._gb.fit(X, y)
        self._trained = True

    # ──────────────────────────────────────────
    # Детектор рыночного режима
    # ──────────────────────────────────────────
    def _detect_regime(self, df) -> dict:
        c    = df["close"]
        atr  = df["atr"].iloc[-1]
        atr_pct = float(df["atr_pct"].iloc[-1]) if "atr_pct" in df else 0.0
        price = c.iloc[-1]
        ema9  = df["ema_9"].iloc[-1]
        ema21 = df["ema_21"].iloc[-1]
        ema50 = df["ema_50"].iloc[-1]
        bb_w  = df["bb_width"].iloc[-1]
        vol_r = df["vol_ratio"].iloc[-1]

        # Тренд
        trending_up   = ema9 > ema21 > ema50
        trending_down = ema9 < ema21 < ema50
        ranging       = abs(ema9 - ema50) / (price + 1e-10) < 0.003

        # Волатильность
        avg_bb_width  = df["bb_width"].rolling(20).mean().iloc[-1]
        high_vol      = bb_w > avg_bb_width * 1.4

        if high_vol:
            regime_name = "VOLATILE"
            regime_color = "yellow"
            regime_desc  = "Высокая волатильность — осторожно"
        elif trending_up:
            regime_name  = "UPTREND"
            regime_color = "green"
            regime_desc  = "Восходящий тренд"
        elif trending_down:
            regime_name  = "DOWNTREND"
            regime_color = "red"
            regime_desc  = "Нисходящий тренд"
        elif ranging:
            regime_name  = "RANGING"
            regime_color = "blue"
            regime_desc  = "Боковое движение"
        else:
            regime_name  = "TRANSITION"
            regime_color = "purple"
            regime_desc  = "Переходная фаза"

        return {
            "name":  regime_name,
            "color": regime_color,
            "desc":  regime_desc,
            "atr":   round(float(atr), 2),
            "atr_pct": round(atr_pct * 100, 3),
            "vol_ratio": round(float(vol_r), 2),
        }

    # ──────────────────────────────────────────
    # Детектор свечных паттернов
    # ──────────────────────────────────────────
    def _detect_candle_patterns(self, df) -> list:
        patterns = []
        o = df["open"].values
        h = df["high"].values
        l = df["low"].values
        c = df["close"].values

        if len(c) < 3:
            return patterns

        def body(i):  return abs(c[i] - o[i])
        def rng(i):   return h[i] - l[i]
        def upper(i): return h[i] - max(c[i], o[i])
        def lower(i): return min(c[i], o[i]) - l[i]

        i = len(c) - 1

        # Доджи
        if rng(i) > 0 and body(i) / rng(i) < 0.1:
            patterns.append({"name": "Дожи", "type": "neutral", "desc": "Нерешительность рынка"})

        # Молот
        if lower(i) > body(i) * 2 and upper(i) < body(i) * 0.5:
            patterns.append({"name": "Молот", "type": "bullish", "desc": "Разворот вверх"})

        # Падающая звезда
        if upper(i) > body(i) * 2 and lower(i) < body(i) * 0.5:
            patterns.append({"name": "Падающая звезда", "type": "bearish", "desc": "Разворот вниз"})

        # Поглощение (бычье)
        if i > 0 and c[i-1] < o[i-1] and c[i] > o[i] and body(i) > body(i-1):
            patterns.append({"name": "Бычье поглощение", "type": "bullish", "desc": "Сильный сигнал вверх"})

        # Поглощение (медвежье)
        if i > 0 and c[i-1] > o[i-1] and c[i] < o[i] and body(i) > body(i-1):
            patterns.append({"name": "Медвежье поглощение", "type": "bearish", "desc": "Сильный сигнал вниз"})

        # Три белых солдата
        if i >= 2:
            if all(c[j] > o[j] for j in range(i-2, i+1)):
                if c[i] > c[i-1] > c[i-2]:
                    patterns.append({"name": "Три белых солдата", "type": "bullish", "desc": "Сильный тренд вверх"})

        # Три чёрных вороны
        if i >= 2:
            if all(c[j] < o[j] for j in range(i-2, i+1)):
                if c[i] < c[i-1] < c[i-2]:
                    patterns.append({"name": "Три чёрных вороны", "type": "bearish", "desc": "Сильный тренд вниз"})

        return patterns[:4]

    # ──────────────────────────────────────────
    # Поддержка / сопротивление
    # ──────────────────────────────────────────
    def _support_resistance(self, df) -> dict:
        c = df["close"].values[-50:]
        h = df["high"].values[-50:]
        l = df["low"].values[-50:]

        # Локальные максимумы / минимумы
        resistances = []
        supports    = []
        for i in range(2, len(c) - 2):
            if h[i] > h[i-1] and h[i] > h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]:
                resistances.append(round(float(h[i]), 2))
            if l[i] < l[i-1] and l[i] < l[i-2] and l[i] < l[i+1] and l[i] < l[i+2]:
                supports.append(round(float(l[i]), 2))

        # Кластеризуем уровни (объединяем близкие)
        def cluster(levels, tol=0.005):
            if not levels:
                return []
            levels = sorted(set(levels))
            clusters = [[levels[0]]]
            for v in levels[1:]:
                if (v - clusters[-1][-1]) / (clusters[-1][-1] + 1e-10) < tol:
                    clusters[-1].append(v)
                else:
                    clusters.append([v])
            return [round(sum(g)/len(g), 2) for g in clusters]

        price = float(c[-1])
        res_levels = cluster(resistances)
        sup_levels = cluster(supports)

        nearest_res = min((r for r in res_levels if r > price), default=None)
        nearest_sup = max((s for s in sup_levels if s < price), default=None)

        return {
            "resistance": res_levels[-3:],
            "support":    sup_levels[:3],
            "nearest_resistance": nearest_res,
            "nearest_support":    nearest_sup,
        }

    # ──────────────────────────────────────────
    # Прогноз цены (линейная экстраполяция + ATR)
    # ──────────────────────────────────────────
    def _price_forecast(self, df) -> dict:
        c    = df["close"].values
        atr  = float(df["atr"].iloc[-1])
        price = float(c[-1])

        # Краткосрочный тренд (линрег на 10 свечах)
        x  = np.arange(10)
        y  = c[-10:]
        m  = np.polyfit(x, y, 1)[0]
        slope_pct = m / (price + 1e-10) * 100

        # Прогноз на 3 свечи вперёд
        delta = m * 3
        t1 = round(price + m,     2)
        t2 = round(price + m*2,   2)
        t3 = round(price + delta, 2)

        return {
            "t1": t1,
            "t2": t2,
            "t3": t3,
            "slope_pct": round(float(slope_pct), 3),
            "bull": bool(slope_pct > 0),
            "range_up":   round(price + atr, 2),
            "range_down": round(price - atr, 2),
        }

    # ──────────────────────────────────────────
    # Важность признаков
    # ──────────────────────────────────────────
    def _feature_importance(self) -> list:
        if not self._trained or not self._feature_names:
            return []
        fi = self._rf.named_steps["clf"].feature_importances_
        pairs = sorted(zip(self._feature_names, fi), key=lambda x: -x[1])
        return [{"feature": k, "importance": round(float(v)*100, 1)} for k, v in pairs[:8]]

    # ──────────────────────────────────────────
    # Детектор аномалий (Z-score)
    # ──────────────────────────────────────────
    def _detect_anomaly(self, df) -> dict:
        c   = df["close"].values
        vol = df["volume"].values
        mu  = np.mean(c[-30:])
        std = np.std(c[-30:]) + 1e-10
        z_price = abs((c[-1] - mu) / std)

        mu_v  = np.mean(vol[-30:])
        std_v = np.std(vol[-30:]) + 1e-10
        z_vol = abs((vol[-1] - mu_v) / std_v)

        anomaly = z_price > 2.5 or z_vol > 3.0
        return {
            "detected":    anomaly,
            "z_price":     round(float(z_price), 2),
            "z_volume":    round(float(z_vol), 2),
            "description": "Аномальное движение!" if anomaly else "Норма",
        }

    def _empty_result(self):
        return {
            "ai_signal": "HOLD", "confidence": 0,
            "prob_up": 0, "prob_down": 0, "prob_hold": 100,
            "regime": {"name": "UNKNOWN", "color": "grey", "desc": "Нет данных",
                       "atr": 0, "vol_ratio": 0},
            "patterns": [], "support_resistance": {},
            "forecast": {}, "feature_importance": [],
            "anomaly": {"detected": False, "z_price": 0, "z_volume": 0, "description": "Нет данных"},
            "model_trained": False, "samples_trained": 0,
        }
