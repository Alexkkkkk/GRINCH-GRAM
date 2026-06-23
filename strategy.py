import pandas as pd
import numpy as np

def compute_indicators(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

    # EMA быстрая и медленная
    df["ema_fast"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()

    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Bollinger Bands
    df["bb_mid"] = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * std
    df["bb_lower"] = df["bb_mid"] - 2 * std

    return df


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

    # MACD
    if prev["macd_hist"] < 0 and last["macd_hist"] > 0:
        score += 1
    elif prev["macd_hist"] > 0 and last["macd_hist"] < 0:
        score -= 1

    # Bollinger
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


def analyze(ohlcv):
    df = compute_indicators(ohlcv)
    signal, strength = get_signal(df)
    last = df.iloc[-1]
    candles = df[["timestamp", "open", "high", "low", "close", "volume"]].tail(50).copy()
    candles["timestamp"] = candles["timestamp"].astype(str)

    return {
        "signal": signal,
        "strength": round(strength * 100, 1),
        "price": round(last["close"], 2),
        "rsi": round(last["rsi"], 2),
        "macd": round(last["macd"], 4),
        "macd_signal": round(last["macd_signal"], 4),
        "ema_fast": round(last["ema_fast"], 2),
        "ema_slow": round(last["ema_slow"], 2),
        "bb_upper": round(last["bb_upper"], 2),
        "bb_lower": round(last["bb_lower"], 2),
        "candles": candles.to_dict("records"),
    }
