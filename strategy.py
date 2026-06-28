import pandas as pd
import numpy as np


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

    return df


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

    strength = min(abs(score) / 5.0, 1.0)

    if score >= 2:
        return "BUY", strength
    elif score <= -2:
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
    df = compute_indicators(ohlcv)
    signal, strength = get_signal(df)

    quality, eq_score, eq_reasons = analyze_entry_quality(df)

    last = df.iloc[-1]
    candles = df[["timestamp", "open", "high", "low", "close", "volume"]].tail(50).copy()
    candles["timestamp"] = candles["timestamp"].astype(str)

    d = _pdigits(last["close"])
    return {
        "signal":        signal,
        "strength":      round(strength * 100, 1),
        "price":         round(last["close"], d),
        "rsi":           round(last["rsi"], 2),
        "macd":          round(last["macd"],        max(4, d)),
        "macd_signal":   round(last["macd_signal"], max(4, d)),
        "ema_fast":      round(last["ema_fast"],    d),
        "ema_slow":      round(last["ema_slow"],    d),
        "bb_upper":      round(last["bb_upper"],    d),
        "bb_lower":      round(last["bb_lower"],    d),
        # ── Новое: качество точки входа ──────────────────────────────────
        "entry_quality": quality,       # 'A' / 'B' / 'C'
        "entry_score":   eq_score,      # итоговый балл
        "entry_reasons": eq_reasons,    # список причин на русском
        # ── Дополнительные индикаторы ──────────────────────────────────
        "vol_ratio":     round(float(last["vol_ratio"]), 2),
        "stoch_rsi":     round(float(last["stoch_rsi"]), 3),
        "atr_pct":       round(float(last["atr_pct"]),   4),
        "candles":       candles.to_dict("records"),
    }
