import time
import pandas as pd
import numpy as np

# ── Кэш результата analyze(): trader.py и ручные снапшоты (/api/candles,
# force_buy/sell) вызывают analyze() на тех же свечах несколько раз за тик —
# индикаторы пересчитываются один раз и переиспользуются, пока свечи не изменились.
_ANALYZE_CACHE_TTL = 8
_analyze_cache_key    = None
_analyze_cache_result = None
_analyze_cache_ts     = 0.0


def compute_indicators(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

    # EMA быстрая и медленная
    df["ema_fast"] = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema_50"]   = df["close"].ewm(span=50, adjust=False).mean()

    # RSI
    delta    = df["close"].diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs       = avg_gain / (avg_loss + 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]        = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]

    # Bollinger Bands
    df["bb_mid"]   = df["close"].rolling(20).mean()
    std            = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * std
    df["bb_lower"] = df["bb_mid"] - 2 * std
    # BB ширина — индикатор сжатия
    df["bb_width"]    = (df["bb_upper"] - df["bb_lower"]) / (df["bb_mid"] + 1e-10)
    df["bb_width_ma"] = df["bb_width"].rolling(20).mean()

    # ATR (14-период)
    hl   = df["high"] - df["low"]
    hcp  = (df["high"] - df["close"].shift(1)).abs()
    lcp  = (df["low"]  - df["close"].shift(1)).abs()
    df["atr"]     = pd.concat([hl, hcp, lcp], axis=1).max(axis=1).rolling(14).mean()
    df["atr_pct"] = df["atr"] / (df["close"] + 1e-10) * 100

    # Volume ratio (объём относительно среднего)
    df["vol_ma"]    = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / (df["vol_ma"] + 1e-10)

    # Stochastic RSI (14)
    rsi_min       = df["rsi"].rolling(14).min()
    rsi_max       = df["rsi"].rolling(14).max()
    df["stoch_rsi"] = (df["rsi"] - rsi_min) / (rsi_max - rsi_min + 1e-10)

    # OBV (On-Balance Volume)
    df["obv"]    = (np.sign(df["close"].diff()) * df["volume"]).fillna(0).cumsum()
    df["obv_ma"] = df["obv"].rolling(10).mean()

    # ADX (Average Directional Index, 14)
    up_move   = df["high"].diff()
    down_move = -df["low"].diff()
    dm_plus   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    dm_minus  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    df["dm_plus"]  = pd.Series(dm_plus,  index=df.index)
    df["dm_minus"] = pd.Series(dm_minus, index=df.index)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift(1)).abs(),
                    (df["low"]  - df["close"].shift(1)).abs()], axis=1).max(axis=1)
    atr14    = tr.ewm(span=14, adjust=False).mean()
    di_plus  = 100 * df["dm_plus"].ewm(span=14, adjust=False).mean()  / (atr14 + 1e-10)
    di_minus = 100 * df["dm_minus"].ewm(span=14, adjust=False).mean() / (atr14 + 1e-10)
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus + 1e-10)
    df["adx"]      = dx.ewm(span=14, adjust=False).mean()
    df["di_plus"]  = di_plus
    df["di_minus"] = di_minus

    # BB позиция (0=нижняя полоса, 100=верхняя)
    bb_range = df["bb_upper"] - df["bb_lower"]
    df["bb_pct"] = ((df["close"] - df["bb_lower"]) / (bb_range + 1e-10) * 100).clip(0, 100)

    # Volume trend (скользящая 5 vs 20)
    df["vol_ma5"] = df["volume"].rolling(5).mean()

    # ── Williams %R (14) ─────────────────────────────────────────────────
    lo14 = df["low"].rolling(14).min()
    hi14 = df["high"].rolling(14).max()
    df["willr"] = -100 * (hi14 - df["close"]) / (hi14 - lo14 + 1e-10)

    # ── CCI (Commodity Channel Index, 20) ────────────────────────────────
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["cci"] = (tp - tp.rolling(20).mean()) / (0.015 * tp.rolling(20).std() + 1e-10)

    # ── Ichimoku Cloud (упрощённый: tenkan / kijun / senkou) ─────────────
    df["tenkan"] = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2
    df["kijun"]  = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
    df["ichi_gap"] = (df["tenkan"] - df["kijun"]) / (df["kijun"] + 1e-10) * 100  # % разрыв
    senkou_a = (df["tenkan"] + df["kijun"]) / 2
    senkou_b = (df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2
    df["above_cloud"] = (df["close"] > senkou_a) & (df["close"] > senkou_b)

    # ── Heiken Ashi ────────────────────────────────────────────────────────
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open  = (df["open"].shift(1) + df["close"].shift(1)) / 2
    df["ha_body"]  = ha_close - ha_open   # >0 бычья, <0 медвежья
    df["ha_trend"] = np.sign(df["ha_body"])

    # ── VWAP отклонение ──────────────────────────────────────────────────
    df["vwap"] = (df["volume"] * (df["high"] + df["low"] + df["close"]) / 3).cumsum() / (df["volume"].cumsum() + 1e-10)
    df["vwap_dev"] = (df["close"] - df["vwap"]) / (df["vwap"] + 1e-10) * 100  # % отклонение от VWAP

    return df


def _get_support_resistance(df, lookback=50):
    """Находит ближайшие уровни поддержки и сопротивления."""
    if len(df) < lookback:
        lookback = len(df)
    window = df.tail(lookback)
    price  = df["close"].iloc[-1]
    highs  = window["high"].values
    lows   = window["low"].values

    # Кластеризуем уровни (±1% зона)
    def cluster(vals, tolerance=0.01):
        vals = sorted(vals)
        clusters = []
        for v in vals:
            placed = False
            for c in clusters:
                if abs(v - c[0]) / (c[0] + 1e-10) <= tolerance:
                    c.append(v)
                    placed = True
                    break
            if not placed:
                clusters.append([v])
        return [np.mean(c) for c in clusters]

    all_highs = cluster(highs)
    all_lows  = cluster(lows)

    # Сопротивление = ближайший уровень выше цены
    resistances = sorted([h for h in all_highs if h > price * 1.003])
    # Поддержка = ближайший уровень ниже цены
    supports = sorted([l for l in all_lows if l < price * 0.997], reverse=True)

    return (
        round(supports[0],    _pdigits(price)) if supports    else None,
        round(resistances[0], _pdigits(price)) if resistances else None,
    )


def _get_market_regime(df):
    """Классифицирует рыночный режим по ADX, BB-ширине, объёму, Ichimoku и DI±."""
    if len(df) < 20:
        return "RANGING", "#ffd166"
    last = df.iloc[-1]
    adx       = float(last.get("adx", 20))
    bb_width  = float(last.get("bb_width", 0))
    bb_avg    = float(last.get("bb_width_ma", bb_width + 1e-10))
    vol_ratio = float(last.get("vol_ratio", 1))
    rsi       = float(last.get("rsi", 50))
    di_plus   = float(last.get("di_plus",  20))
    di_minus  = float(last.get("di_minus", 20))
    atr_pct   = float(last.get("atr_pct",  0))
    ichi_gap  = float(last.get("ichi_gap", 0)) if "ichi_gap" in last.index else 0

    # Дополнительные подтверждения направления тренда
    di_bull  = di_plus > di_minus * 1.15   # DI+ доминирует
    di_bear  = di_minus > di_plus * 1.15   # DI- доминирует
    ichi_ok  = ichi_gap > 0                # tenkan > kijun

    breakout = bb_width > bb_avg * 1.35 and vol_ratio > 1.4
    trending = adx > 23
    volatile = atr_pct > 3.0

    # Пробой: BB расширяется + объём
    if breakout and vol_ratio > 2.0:
        return "BREAKOUT", "#00d4ff"
    # Восходящий тренд: ADX сильный + DI+ ведёт + RSI выше нейтрали + Ichimoku
    if trending and di_bull and rsi > 52 and ichi_ok:
        return "UPTREND", "#00ff88"
    # Восходящий тренд (менее подтверждённый)
    if trending and rsi > 55 and (di_bull or ichi_ok):
        return "UPTREND", "#00ff88"
    # Нисходящий тренд: ADX сильный + DI- ведёт + RSI ниже нейтрали
    if trending and di_bear and rsi < 48:
        return "DOWNTREND", "#ff4d6d"
    if trending and rsi < 45 and di_bear:
        return "DOWNTREND", "#ff4d6d"
    if breakout:
        return "BREAKOUT", "#00d4ff"
    if volatile:
        return "VOLATILE", "#ffd166"
    return "RANGING", "#8892b0"


# ── Факторы качества входа ─────────────────────────────────────────────────────

def _check_volume_surge(df):
    """Объём значительно выше среднего — признак институционального интереса."""
    ratio = df["vol_ratio"].iloc[-1]
    if ratio >= 2.0:
        return 3, f"🔥 Объём {ratio:.1f}x выше среднего (кит/институция)"
    if ratio >= 1.5:
        return 2, f"📈 Объём {ratio:.1f}x выше нормы (повышенный интерес)"
    if ratio >= 1.2:
        return 1, f"📊 Объём немного повышен ({ratio:.1f}x)"
    return 0, None


def _check_bb_squeeze_breakout(df):
    """BB сжалась, затем цена пробивает вверх — «катапульта» после сжатия."""
    if len(df) < 25:
        return 0, None
    width_now  = df["bb_width"].iloc[-1]
    width_avg  = df["bb_width_ma"].iloc[-1]
    if width_avg <= 0:
        return 0, None
    # В последние 5-10 баров ширина была заметно ниже среднего (сжатие)
    recent_width = df["bb_width"].iloc[-8:-1]
    recent_avg   = df["bb_width_ma"].iloc[-8:-1]
    was_squeezed = ((recent_width / (recent_avg + 1e-10)) < 0.82).any()
    # Сейчас: цена выше BB середины и ширина растёт (разжатие)
    expanding_up = (
        df["close"].iloc[-1] > df["bb_mid"].iloc[-1] and
        width_now > df["bb_width"].iloc[-2] * 1.02
    )
    if was_squeezed and expanding_up:
        return 3, "💥 BB сжатие → разрыв вверх (накопленная энергия)"
    return 0, None


def _check_bullish_divergence(df, lookback=14):
    """RSI бычья дивергенция: цена делает новый минимум, RSI — нет."""
    if len(df) < lookback + 3:
        return 0, None
    try:
        prices  = df["close"].iloc[-(lookback + 2):-1]
        rsis    = df["rsi"].iloc[-(lookback + 2):-1]
        p_now   = df["close"].iloc[-1]
        rsi_now = df["rsi"].iloc[-1]
        p_min   = prices.min()
        rsi_at_min = rsis.iloc[prices.argmin()]
        # Цена сейчас примерно у минимума, RSI выше чем был при минимуме цены
        near_low = p_now <= p_min * 1.025
        div_rsi  = rsi_now > rsi_at_min + 4
        if near_low and div_rsi:
            return 3, f"📐 Бычья дивергенция RSI ({rsi_at_min:.0f}→{rsi_now:.0f})"
    except Exception:
        pass
    return 0, None


def _check_momentum_candles(df):
    """Несколько зелёных свечей подряд с нарастающим объёмом — конвикция."""
    if len(df) < 5:
        return 0, None
    last = df.iloc[-4:]
    closes = list(last["close"])
    opens  = list(last["open"])
    vols   = list(last["volume"])
    green3 = all(closes[i] > opens[i] for i in range(1, 4))
    green2 = closes[-1] > opens[-1] and closes[-2] > opens[-2]
    vol_up = vols[-1] > vols[-2]
    if green3 and vol_up:
        return 2, "🕯️ 3 бычьи свечи + растущий объём"
    if green2 and vol_up:
        return 1, "🕯️ 2 бычьи свечи + растущий объём"
    return 0, None


def _check_stoch_rsi_cross(df):
    """Stoch RSI разворот вверх из зоны перепроданности."""
    if len(df) < 5:
        return 0, None
    stoch = df["stoch_rsi"]
    prev2 = stoch.iloc[-3]
    prev  = stoch.iloc[-2]
    last  = stoch.iloc[-1]
    # Был в перепроданности, сейчас разворачивается вверх
    was_oversold = prev < 0.25 or prev2 < 0.25
    turning_up   = last > prev + 0.04 and last > 0.15
    if was_oversold and turning_up:
        return 2, f"⚡ Stoch RSI разворот из перепроданности ({prev:.2f}→{last:.2f})"
    return 0, None


def _check_support_bounce(df, lookback=20):
    """Цена отскакивает от ключевого уровня поддержки."""
    if len(df) < lookback + 5:
        return 0, None
    try:
        past_closes  = df["close"].iloc[-(lookback + 4):-3]
        support      = past_closes.min()
        price_now    = df["close"].iloc[-1]
        low_now      = df["low"].iloc[-1]
        price_prev   = df["close"].iloc[-2]
        # Цена ткнулась в зону поддержки (±1.5%) и восстанавливается
        touch_zone   = low_now <= support * 1.015
        recovering   = price_now > price_prev * 1.002
        # Бычий фитиль — закрылись заметно выше минимума (поглощение продавцов)
        bull_wick    = (price_now - low_now) > (price_now - df["open"].iloc[-1]).abs() * 0.4
        if touch_zone and (recovering or bull_wick):
            return 2, f"🎯 Отскок от поддержки (${support:.4g})"
    except Exception:
        pass
    return 0, None


def _check_obv_confirm(df):
    """OBV растёт — покупочное давление подтверждено объёмом."""
    if len(df) < 10:
        return 0, None
    obv = df["obv"]
    growing = (obv.iloc[-1] > obv.iloc[-3] > obv.iloc[-5])
    above_ma = obv.iloc[-1] > df["obv_ma"].iloc[-1]
    if growing and above_ma:
        return 1, "📊 OBV подтверждает покупочное давление"
    return 0, None


def _check_ema_confluence(df):
    """EMA 9 > EMA 21 > EMA 50 и цена выше всех EM — полный тренд-фильтр."""
    if len(df) < 10:
        return 0, None
    last = df.iloc[-1]
    triple_align = (last["ema_fast"] > last["ema_slow"] > last["ema_50"])
    price_above  = last["close"] > last["ema_fast"]
    if triple_align and price_above:
        return 1, "📈 Тройная EMA выстроена (9>21>50)"
    return 0, None


def _check_macd_acceleration(df):
    """MACD гистограмма растёт 2+ бара подряд — нарастающий импульс."""
    if len(df) < 4:
        return 0, None
    h = df["macd_hist"]
    if h.iloc[-1] > h.iloc[-2] > h.iloc[-3] and h.iloc[-1] > 0:
        return 1, "🚀 MACD ускорение (гистограмма растёт 3 бара)"
    if h.iloc[-1] > h.iloc[-2] and h.iloc[-1] > 0:
        return 1, "🚀 MACD нарастающий импульс"
    return 0, None


def _check_rsi_oversold(df):
    """RSI в зоне перепроданности — вероятен отскок."""
    rsi = df["rsi"].iloc[-1]
    if rsi < 20:
        return 3, f"🩸 RSI критическая перепроданность ({rsi:.0f})"
    if rsi < 30:
        return 2, f"🔴 RSI глубокая перепроданность ({rsi:.0f})"
    if rsi < 38:
        return 1, f"📉 RSI перепроданность ({rsi:.0f})"
    return 0, None


def _check_ichimoku_bull(df):
    """Ichimoku: tenkan > kijun + цена выше kijun — бычий облачный сигнал."""
    if len(df) < 30 or "tenkan" not in df.columns:
        return 0, None
    last = df.iloc[-1]
    tenkan = float(last.get("tenkan", 0))
    kijun  = float(last.get("kijun",  0))
    price  = float(last["close"])
    if tenkan <= 0 or kijun <= 0:
        return 0, None
    tk_cross = tenkan > kijun
    above    = price > kijun
    cloud_ok = bool(last.get("above_cloud", False))
    if tk_cross and above and cloud_ok:
        return 3, f"☁️ Ichimoku: цена над облаком, tenkan>kijun (мощный тренд)"
    if tk_cross and above:
        return 2, f"☁️ Ichimoku: tenkan>kijun + цена выше kijun"
    if tk_cross:
        return 1, f"☁️ Ichimoku: tenkan пересёк kijun вверх"
    return 0, None


def _check_williams_oversold(df):
    """Williams %R разворот из зоны перепроданности < -80."""
    if len(df) < 5 or "willr" not in df.columns:
        return 0, None
    willr     = df["willr"]
    now       = float(willr.iloc[-1])
    prev      = float(willr.iloc[-2])
    was_low   = float(willr.iloc[-4:-1].min())
    # Был глубоко перепродан и разворачивается вверх
    was_oversold = was_low < -80
    turning_up   = now > prev + 3 and now > -85
    if was_oversold and turning_up and now < -50:
        return 2, f"📉 Williams %R разворот из перепроданности ({now:.0f})"
    if now < -85:
        return 1, f"📉 Williams %R экстремальная перепроданность ({now:.0f})"
    return 0, None


def _check_cci_momentum(df):
    """CCI выходит из перепроданности (< -100) — нарастающий импульс покупок."""
    if len(df) < 5 or "cci" not in df.columns:
        return 0, None
    cci   = df["cci"]
    now   = float(cci.iloc[-1])
    prev  = float(cci.iloc[-2])
    prev2 = float(cci.iloc[-3])
    # Пересечение -100 снизу вверх → сильный разворот
    cross_up = prev2 < -100 and prev < -100 and now > -100
    if cross_up:
        return 2, f"📊 CCI пересёк -100 снизу вверх ({now:.0f}) — разворот"
    if now < -120 and now > prev:
        return 1, f"📊 CCI отскок от перепроданности ({now:.0f})"
    # CCI строит положительный импульс (нарастает выше 0)
    if now > 0 and prev > 0 and now > prev > prev2:
        return 1, f"📊 CCI нарастающий бычий импульс ({now:.0f})"
    return 0, None


def _check_heiken_ashi_bull(df):
    """Heiken Ashi: несколько подряд бычьих свечей — конвикция тренда."""
    if len(df) < 6 or "ha_body" not in df.columns:
        return 0, None
    ha = df["ha_body"].iloc[-6:]
    bull_streak = 0
    for v in reversed(ha.values):
        if v > 0:
            bull_streak += 1
        else:
            break
    if bull_streak >= 5:
        return 2, f"🕯️ Heiken Ashi: {bull_streak} бычьих свечей подряд — сильный тренд"
    if bull_streak >= 3:
        return 1, f"🕯️ Heiken Ashi: {bull_streak} бычьих свечей подряд"
    return 0, None


# ── Итоговый грейд входа ───────────────────────────────────────────────────────

def analyze_entry_quality(df):
    """
    Многофакторный скоринг точки входа.
    Возвращает: quality ('A'/'B'/'C'), score (int), reasons (list[str])

    Грейды:
      A (score ≥ 7) — элитный вход: 4+ сильных фактора совпали;
                      пропускаем ожидание 2-го подтверждения,
                      цель откатного входа -0.3% (быстрее заполняется).
      B (score ≥ 3) — стандартный вход: обычное подтверждение, откат -0.8%.
      C (score <  3) — слабый вход: требуем 3 подтверждения, откат -1.5%.
    """
    total  = 0
    reasons = []

    for checker in [
        _check_volume_surge,
        _check_bb_squeeze_breakout,
        _check_bullish_divergence,
        _check_momentum_candles,
        _check_stoch_rsi_cross,
        _check_support_bounce,
        _check_obv_confirm,
        _check_ema_confluence,
        _check_macd_acceleration,
        _check_rsi_oversold,
        # Новые чекеры v2
        _check_ichimoku_bull,
        _check_williams_oversold,
        _check_cci_momentum,
        _check_heiken_ashi_bull,
    ]:
        try:
            pts, reason = checker(df)
            if pts > 0 and reason:
                total += pts
                reasons.append(reason)
        except Exception:
            pass

    if total >= 7:
        quality = "A"
    elif total >= 3:
        quality = "B"
    else:
        quality = "C"

    return quality, total, reasons


# ── Базовый сигнал (технический) ──────────────────────────────────────────────

def get_signal(df):
    if len(df) < 30:
        return "HOLD", 0.0

    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0

    # EMA crossover
    if prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
        score += 2
    elif prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
        score -= 2

    # RSI
    if last["rsi"] < 35:
        score += 1
    elif last["rsi"] > 65:
        score -= 1

    # MACD histogram crossover
    if prev["macd_hist"] < 0 and last["macd_hist"] > 0:
        score += 1
    elif prev["macd_hist"] > 0 and last["macd_hist"] < 0:
        score -= 1

    # Bollinger Bands
    if last["close"] < last["bb_lower"]:
        score += 1
    elif last["close"] > last["bb_upper"]:
        score -= 1

    # Williams %R (новый фактор)
    if "willr" in last.index:
        willr = float(last["willr"])
        if willr < -80:
            score += 1
        elif willr > -20:
            score -= 1

    # CCI (новый фактор)
    if "cci" in last.index:
        cci = float(last["cci"])
        if cci < -100:
            score += 1
        elif cci > 150:
            score -= 1

    # Ichimoku (новый фактор)
    if "ichi_gap" in last.index and "tenkan" in last.index:
        tenkan = float(last["tenkan"])
        kijun  = float(last["kijun"])
        price  = float(last["close"])
        if tenkan > kijun and price > kijun:
            score += 1
        elif tenkan < kijun and price < kijun:
            score -= 1

    # Heiken Ashi тренд (новый фактор)
    if "ha_trend" in last.index:
        ha = float(last["ha_trend"])
        if ha > 0 and float(df["ha_trend"].iloc[-2]) > 0:
            score += 1
        elif ha < 0 and float(df["ha_trend"].iloc[-2]) < 0:
            score -= 1

    strength = min(abs(score) / 8.0, 1.0)

    if score >= 3:
        return "BUY", strength
    elif score <= -3:
        return "SELL", strength
    else:
        return "HOLD", strength


# ── Форматирование цены ─────────────────────────────────────────────────────────

def _pdigits(p):
    """Адаптивное число знаков после запятой в зависимости от величины цены."""
    p = abs(float(p))
    if p >= 100:  return 2
    if p >= 1:    return 4
    if p >= 0.01: return 6
    return 8


# ── Публичный API ──────────────────────────────────────────────────────────────

def analyze(ohlcv):
    global _analyze_cache_key, _analyze_cache_result, _analyze_cache_ts

    if ohlcv:
        last_bar = ohlcv[-1]
        cache_key = (len(ohlcv), last_bar[0], last_bar[4])
        now = time.time()
        if (
            cache_key == _analyze_cache_key
            and _analyze_cache_result is not None
            and (now - _analyze_cache_ts) < _ANALYZE_CACHE_TTL
        ):
            return _analyze_cache_result
    else:
        cache_key = None

    result = _analyze_impl(ohlcv)

    if cache_key is not None:
        _analyze_cache_key    = cache_key
        _analyze_cache_result = result
        _analyze_cache_ts     = time.time()
    return result


def _analyze_impl(ohlcv):
    df = compute_indicators(ohlcv)
    signal, strength = get_signal(df)

    quality, eq_score, eq_reasons = analyze_entry_quality(df)
    # Детальный скоринг по всем 14 факторам
    factors_detail = []
    for checker in [
        _check_volume_surge, _check_bb_squeeze_breakout, _check_bullish_divergence,
        _check_momentum_candles, _check_stoch_rsi_cross, _check_support_bounce,
        _check_obv_confirm, _check_ema_confluence, _check_macd_acceleration, _check_rsi_oversold,
        _check_ichimoku_bull, _check_williams_oversold, _check_cci_momentum, _check_heiken_ashi_bull,
    ]:
        try:
            pts, reason = checker(df)
            factors_detail.append({"pts": pts, "reason": reason or ""})
        except Exception:
            factors_detail.append({"pts": 0, "reason": ""})

    last   = df.iloc[-1]
    d      = _pdigits(last["close"])
    candles = df[["timestamp", "open", "high", "low", "close", "volume"]].tail(50).copy()
    candles["timestamp"] = candles["timestamp"].astype(str)

    # Рыночный режим
    regime_name, regime_color = _get_market_regime(df)

    # Поддержка / сопротивление из ценового действия
    try:
        support_pa, resistance_pa = _get_support_resistance(df)
    except Exception:
        support_pa, resistance_pa = None, None

    # RSI зона
    rsi_val = float(last["rsi"])
    if rsi_val < 30:      rsi_zone = "OVERSOLD"
    elif rsi_val > 70:    rsi_zone = "OVERBOUGHT"
    elif rsi_val < 45:    rsi_zone = "LOW"
    elif rsi_val > 55:    rsi_zone = "HIGH"
    else:                 rsi_zone = "NEUTRAL"

    # MACD направление
    hist_now  = float(last["macd_hist"])
    hist_prev = float(df["macd_hist"].iloc[-2]) if len(df) > 1 else 0.0
    if hist_now > hist_prev * 1.05 and hist_now > 0:   macd_dir = "UP"
    elif hist_now < hist_prev * 1.05 and hist_now < 0:  macd_dir = "DOWN"
    else:                                                macd_dir = "FLAT"

    # OBV тренд
    obv = df["obv"]
    if len(obv) >= 5:
        if obv.iloc[-1] > obv.iloc[-3] > obv.iloc[-5]:  obv_dir = "UP"
        elif obv.iloc[-1] < obv.iloc[-3]:                obv_dir = "DOWN"
        else:                                             obv_dir = "FLAT"
    else:
        obv_dir = "FLAT"

    # Volume trend (5-bar MA vs 20-bar MA)
    vm5  = float(last.get("vol_ma5", last["vol_ma"]))
    vm20 = float(last["vol_ma"])
    vol_trend = "UP" if vm5 > vm20 * 1.05 else ("DOWN" if vm5 < vm20 * 0.95 else "FLAT")

    # EMA выравнивание (0=нет, 1=fast>slow, 2=fast>slow>50, 3=цена>fast>slow>50)
    ema_align = 0
    if last["ema_fast"] > last["ema_slow"]:              ema_align = 1
    if last["ema_fast"] > last["ema_slow"] > last["ema_50"]: ema_align = 2
    if (last["close"] > last["ema_fast"] > last["ema_slow"] > last["ema_50"]): ema_align = 3

    # Расстояние до EMA50 в %
    price_vs_ema50 = round((last["close"] / last["ema_50"] - 1) * 100, 2) if last["ema_50"] > 0 else 0.0

    # ── AI Opportunity Score (0-100) ─────────────────────────────────
    score_entry  = min(eq_score / 10 * 38, 38)           # 0-38: качество входа
    _rbmap = {"UPTREND": 22, "BREAKOUT": 22, "VOLATILE": 9, "RANGING": 4, "DOWNTREND": 0}
    score_regime = _rbmap.get(regime_name, 4)             # 0-22: режим
    score_vol    = min(float(last["vol_ratio"]) / 3 * 18, 18)  # 0-18: объём
    score_adx    = min(float(last["adx"]) / 50 * 12, 12)      # 0-12: тренд
    score_sig    = round(strength * 10)                    # 0-10: сила сигнала
    opportunity_score = int(min(100, round(score_entry + score_regime + score_vol + score_adx + score_sig)))

    # ── Мультитаймфреймные сигналы ────────────────────────────────────
    def _mtf_signal(window_n):
        if len(df) < window_n + 5:
            return "НЕЙТРАЛ", 50, "#8892b0"
        sub  = df.iloc[-window_n:]
        sl   = sub.iloc[-1]
        rsi  = float(sl["rsi"])
        hist = float(sl["macd_hist"])
        ema_cross = sl["ema_fast"] > sl["ema_slow"]
        stoch = float(sl["stoch_rsi"])
        willr = float(sl.get("willr", -50))
        cci   = float(sl.get("cci", 0))
        ha    = float(sl.get("ha_trend", 0))
        ichi  = float(sl.get("ichi_gap", 0))
        bull = sum([rsi < 65, rsi > 38, hist > 0, ema_cross, stoch < 0.75,
                    willr < -20, cci > -50, ha > 0, ichi > 0])
        bear = sum([rsi > 35, rsi < 62, hist < 0, not ema_cross, stoch > 0.25,
                    willr > -80, cci < 50, ha < 0, ichi < 0])
        if bull >= 6: return "ПОКУПКА", min(50 + bull * 5, 95), "#00ff88"
        if bear >= 6: return "ПРОДАЖА", min(50 + bear * 5, 95), "#ff4d6d"
        if bull > bear: return "СЛАБЫЙ ↗", 40 + bull * 4, "#5cc8ff"
        if bear > bull: return "СЛАБЫЙ ↘", 40 + bear * 4, "#ffd166"
        return "НЕЙТРАЛ", 50, "#8892b0"

    signals_mtf = [
        {"tf": "Скальпинг",  "n": 8,  **dict(zip(["signal","conf","color"], _mtf_signal(8)))},
        {"tf": "Интрадей",   "n": 20, **dict(zip(["signal","conf","color"], _mtf_signal(20)))},
        {"tf": "Свинг",      "n": 50, **dict(zip(["signal","conf","color"], _mtf_signal(50)))},
    ]

    # ── Предсказание цены ────────────────────────────────────────────
    atr_val   = float(last["atr"])
    price_now = float(last["close"])
    uptrend   = float(last["ema_fast"]) > float(last["ema_slow"])
    tgt_mult  = 1.8 if opportunity_score >= 70 else 1.3
    stp_mult  = 1.0

    price_target  = round(price_now + atr_val * (tgt_mult  if uptrend else -tgt_mult),  d)
    price_stop    = round(price_now - atr_val * (stp_mult  if uptrend else -stp_mult),  d)
    rr_ratio      = round(abs(price_target - price_now) / (abs(price_stop - price_now) + 1e-10), 2)
    prob_win      = min(95, max(30, opportunity_score + (10 if uptrend else -10)))

    # ── AI Компоненты (breakdown для radar/bars) ──────────────────────
    ai_components = [
        {"name": "Качество входа",   "icon": "🎯", "val": int(score_entry),  "max": 38,
         "pct": round(score_entry / 38 * 100), "color": "#00ff88"},
        {"name": "Режим рынка",      "icon": "🌊", "val": int(score_regime), "max": 22,
         "pct": round(score_regime / 22 * 100), "color": "#00d4ff"},
        {"name": "Объём/Ликвид.",    "icon": "💧", "val": round(score_vol,1), "max": 18,
         "pct": round(score_vol / 18 * 100), "color": "#5cc8ff"},
        {"name": "Сила тренда ADX",  "icon": "📐", "val": round(score_adx,1), "max": 12,
         "pct": round(score_adx / 12 * 100), "color": "#ffd166"},
        {"name": "Сигнал модели",    "icon": "🤖", "val": score_sig, "max": 10,
         "pct": round(score_sig / 10 * 100), "color": "#c084fc"},
    ]

    return {
        "signal":        signal,
        "strength":      round(strength * 100, 1),
        "price":         round(last["close"], d),
        "rsi":           round(rsi_val, 2),
        "macd":          round(last["macd"],        max(4, d)),
        "macd_signal":   round(last["macd_signal"], max(4, d)),
        "macd_hist":     round(hist_now, max(4, d)),
        "macd_dir":      macd_dir,
        "ema_fast":      round(last["ema_fast"],    d),
        "ema_slow":      round(last["ema_slow"],    d),
        "ema_50":        round(last["ema_50"],      d),
        "bb_upper":      round(last["bb_upper"],    d),
        "bb_lower":      round(last["bb_lower"],    d),
        "bb_mid":        round(last["bb_mid"],      d),
        "bb_pct":        round(float(last["bb_pct"]), 1),
        "bb_width_pct":  round(float(last["bb_width"]) / (float(last["bb_width_ma"]) + 1e-10) * 100, 1),
        # ADX
        "adx":           round(float(last["adx"]),      1),
        "di_plus":       round(float(last["di_plus"]),  1),
        "di_minus":      round(float(last["di_minus"]), 1),
        # Режим
        "regime":        regime_name,
        "regime_color":  regime_color,
        # RSI
        "rsi_zone":      rsi_zone,
        # Объём
        "vol_ratio":     round(float(last["vol_ratio"]), 2),
        "vol_trend":     vol_trend,
        # OBV
        "obv_dir":       obv_dir,
        # Stoch RSI
        "stoch_rsi":     round(float(last["stoch_rsi"]), 3),
        "atr_pct":       round(float(last["atr_pct"]),   4),
        # EMA выравнивание
        "ema_alignment":    ema_align,
        "price_vs_ema50":   price_vs_ema50,
        # Поддержка/Сопротивление (из ценового действия)
        "support_pa":    support_pa,
        "resistance_pa": resistance_pa,
        # Качество входа
        "entry_quality": quality,
        "entry_score":   eq_score,
        "entry_reasons": eq_reasons,
        "factors_detail": factors_detail,
        # AI аналитика
        "opportunity_score": opportunity_score,
        "signals_mtf":       signals_mtf,
        "price_target":      price_target,
        "price_stop":        price_stop,
        "rr_ratio":          rr_ratio,
        "prob_win":          prob_win,
        "ai_components":     ai_components,
        # Свечи для графика
        "candles":       candles.to_dict("records"),
        # Новые индикаторы v2
        "willr":         round(float(last.get("willr",  -50)), 2),
        "cci":           round(float(last.get("cci",      0)), 1),
        "ichi_gap":      round(float(last.get("ichi_gap", 0)), 3),
        "ha_trend":      int(float(last.get("ha_trend",   0))),
        "vwap_dev":      round(float(last.get("vwap_dev", 0)), 3),
        "tenkan":        round(float(last.get("tenkan", last["close"])), d),
        "kijun":         round(float(last.get("kijun",  last["close"])), d),
        "above_cloud":   bool(last.get("above_cloud", False)),
    }
