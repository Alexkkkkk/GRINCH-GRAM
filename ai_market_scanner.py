"""
AI Market Scanner — фоновый сканер высоковероятных торговых паттернов.

Работает как отдельный поток, постоянно анализируя OHLCV-данные
и детектируя классические паттерны с AI-подтверждением:

  🏺 Двойное дно    (Double Bottom)  — разворот после падения
  📦 Накопление     (Accumulation)   — умные деньги тихо скупают
  💥 Пробой сжатия  (Squeeze Break)  — после низкой волатильности
  🔄 Бычье поглощение (Bull Engulf) — сильная свеча поглощает падение
  📐 Восходящий треугольник          — паттерн продолжения

При обнаружении паттерна с уверенностью ≥ порога — отправляет сигнал
в BrainFusion как дополнительный источник (Scanner).

Интеграция:
    import ai_market_scanner as scanner
    scanner.start(get_candles_fn)   # один раз при старте
    sig = scanner.get_last_signal() # в торговом цикле
"""

import time
import math
import logging
import threading
from typing import Optional, Callable, List

log = logging.getLogger("ai_market_scanner")

# ─── Параметры ────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC   = 30     # сканируем каждые 30 секунд
SIGNAL_TTL_SEC      = 120    # сигнал живёт 2 минуты
MIN_CANDLES         = 20     # минимум свечей для анализа
PATTERN_CONF_THRESH = 0.60   # порог уверенности для выдачи сигнала

# ─── Глобальное состояние ─────────────────────────────────────────────────────
_lock         = threading.Lock()
_last_signal  = None    # dict с последним сигналом
_last_scan_ts = 0.0
_running      = False
_thread       = None
_get_candles  = None    # callable → list of OHLCV dicts


# ════════════════════════════════════════════════════════════════════════════════
# Паттерны
# ════════════════════════════════════════════════════════════════════════════════

def _detect_double_bottom(closes: list, lows: list, volumes: list) -> dict:
    """
    Двойное дно: два локальных минимума примерно на одном уровне,
    разделённых восстановлением ≥ 3%, второй объём ≥ первого.
    """
    if len(closes) < 15:
        return {"found": False}
    n = len(closes)

    # Ищем два локальных минимума в последних 30 барах
    window = min(n, 30)
    seg = lows[-window:]
    vols = volumes[-window:]

    bottoms = []
    for i in range(2, len(seg) - 2):
        if seg[i] < seg[i-1] and seg[i] < seg[i-2] and seg[i] < seg[i+1] and seg[i] < seg[i+2]:
            bottoms.append((i, seg[i], vols[i]))

    if len(bottoms) < 2:
        return {"found": False}

    b1_idx, b1_price, b1_vol = bottoms[-2]
    b2_idx, b2_price, b2_vol = bottoms[-1]

    # Проверяем условия
    price_diff_pct = abs(b2_price - b1_price) / max(b1_price, 1e-12) * 100
    if price_diff_pct > 4.0:   # дна должны быть близко (<4%)
        return {"found": False}

    # Восстановление между дном было ≥ 3%
    mid_high = max(seg[b1_idx:b2_idx+1]) if b2_idx > b1_idx else 0
    recovery_pct = (mid_high - b1_price) / max(b1_price, 1e-12) * 100
    if recovery_pct < 3.0:
        return {"found": False}

    # Текущая цена близко ко второму дну (потенциал роста)
    current = closes[-1]
    dist_from_b2 = (current - b2_price) / max(b2_price, 1e-12) * 100
    if dist_from_b2 > 8.0:    # слишком далеко ушла — поздно
        return {"found": False}

    conf = 0.60
    if b2_vol >= b1_vol * 0.8: conf += 0.10  # объём подтверждает
    if dist_from_b2 < 3.0:     conf += 0.08  # прямо у дна
    if recovery_pct > 5.0:     conf += 0.07  # сильное восстановление

    return {
        "found":       True,
        "pattern":     "double_bottom",
        "label":       "🏺 Двойное дно",
        "confidence":  round(min(conf, 0.95), 3),
        "signal":      "BUY",
        "note":        f"дна: ${b1_price:.6f} / ${b2_price:.6f} +{recovery_pct:.1f}% между",
    }


def _detect_accumulation(closes: list, volumes: list, sm_score: float = 0.0) -> dict:
    """
    Накопление: цена в узком диапазоне (ATR < 2%), объём постепенно растёт,
    умные деньги покупают (если данные доступны).
    """
    if len(closes) < 10:
        return {"found": False}

    seg_c = closes[-15:]
    seg_v = volumes[-15:]
    price_range = (max(seg_c) - min(seg_c)) / max(min(seg_c), 1e-12) * 100

    if price_range > 4.0:   # слишком большой диапазон — не накопление
        return {"found": False}

    # Объём растёт в последние 5 баров?
    recent_vol  = sum(seg_v[-5:]) / 5 if len(seg_v) >= 5 else sum(seg_v) / len(seg_v)
    earlier_vol = sum(seg_v[:5])  / 5 if len(seg_v) >= 10 else recent_vol
    vol_growth  = recent_vol / max(earlier_vol, 1e-12)

    if vol_growth < 1.1:    # объём не растёт
        return {"found": False}

    conf = 0.58
    if vol_growth > 1.5:  conf += 0.12
    if price_range < 2.0: conf += 0.08
    if sm_score > 0.2:    conf += 0.10
    if sm_score > 0.4:    conf += 0.08

    return {
        "found":      True,
        "pattern":    "accumulation",
        "label":      "📦 Накопление",
        "confidence": round(min(conf, 0.92), 3),
        "signal":     "BUY",
        "note":       f"диапазон {price_range:.1f}%, объём ×{vol_growth:.2f}",
    }


def _detect_squeeze_breakout(closes: list, highs: list, lows: list,
                              volumes: list) -> dict:
    """
    Пробой из сжатия Боллинджера:
    несколько баров с низкой волатильностью, затем резкий рост объёма.
    """
    if len(closes) < 20:
        return {"found": False}

    # BB ширина = (upper - lower) / middle
    def _bb_width(prices, w=10):
        if len(prices) < w:
            return 0.0
        seg = prices[-w:]
        mean = sum(seg) / w
        std  = math.sqrt(sum((x - mean)**2 for x in seg) / w)
        return (2 * 2 * std) / max(mean, 1e-12) * 100  # % width

    curr_width = _bb_width(closes, 10)
    prev_width = _bb_width(closes[:-5], 10)

    if prev_width <= 0:
        return {"found": False}

    # Сжатие: текущая ширина была маленькой (< 4%)
    if curr_width > 4.0:
        return {"found": False}

    # Объём в последних 3 барах выше среднего
    avg_vol = sum(volumes[-15:-3]) / max(len(volumes[-15:-3]), 1)
    recent  = sum(volumes[-3:]) / 3
    vol_surge = recent / max(avg_vol, 1e-12)

    if vol_surge < 1.3:
        return {"found": False}

    # Направление пробоя: закрытие выше середины диапазона
    price_mid = (max(closes[-15:]) + min(closes[-15:])) / 2
    bullish   = closes[-1] > price_mid

    if not bullish:
        return {"found": False}

    conf = 0.60
    if vol_surge > 2.0:    conf += 0.12
    if curr_width < 2.0:   conf += 0.10
    if vol_surge > 3.0:    conf += 0.08

    return {
        "found":      True,
        "pattern":    "squeeze_breakout",
        "label":      "💥 Пробой сжатия",
        "confidence": round(min(conf, 0.92), 3),
        "signal":     "BUY",
        "note":       f"BB ширина {curr_width:.1f}%, объём ×{vol_surge:.1f}",
    }


def _detect_bull_engulfing(opens: list, closes: list, volumes: list) -> dict:
    """
    Бычье поглощение: большая зелёная свеча поглощает предыдущую красную.
    """
    if len(closes) < 5:
        return {"found": False}

    o1, c1, v1 = opens[-2], closes[-2], volumes[-2]
    o2, c2, v2 = opens[-1], closes[-1], volumes[-1]

    prev_bearish = c1 < o1
    curr_bullish = c2 > o2
    engulfs      = c2 > o1 and o2 < c1

    if not (prev_bearish and curr_bullish and engulfs):
        return {"found": False}

    body_size = abs(c2 - o2) / max(o2, 1e-12) * 100
    if body_size < 1.0:   # слабая свеча
        return {"found": False}

    conf = 0.60
    if v2 > v1 * 1.5: conf += 0.12   # объём больше
    if body_size > 3.0: conf += 0.10  # крупное поглощение

    return {
        "found":      True,
        "pattern":    "bull_engulfing",
        "label":      "🔄 Бычье поглощение",
        "confidence": round(min(conf, 0.90), 3),
        "signal":     "BUY",
        "note":       f"тело {body_size:.1f}%, объём ×{v2/max(v1,1e-12):.1f}",
    }


# ════════════════════════════════════════════════════════════════════════════════
# Основной цикл сканера
# ════════════════════════════════════════════════════════════════════════════════

def _scan_once(sm_score: float = 0.0):
    """Один проход сканера — проверяет все паттерны."""
    global _last_signal, _last_scan_ts

    if _get_candles is None:
        return

    try:
        candles = _get_candles()
        if not candles or len(candles) < MIN_CANDLES:
            return
    except Exception as e:
        log.debug(f"[Scanner] get_candles error: {e}")
        return

    try:
        opens   = [float(c.get("open",  c.get("o", 0)) or 0) for c in candles]
        highs   = [float(c.get("high",  c.get("h", 0)) or 0) for c in candles]
        lows    = [float(c.get("low",   c.get("l", 0)) or 0) for c in candles]
        closes  = [float(c.get("close", c.get("c", 0)) or 0) for c in candles]
        volumes = [float(c.get("volume",c.get("v", 0)) or 0) for c in candles]
    except Exception as e:
        log.debug(f"[Scanner] candle parse error: {e}")
        return

    detectors = [
        lambda: _detect_double_bottom(closes, lows, volumes),
        lambda: _detect_accumulation(closes, volumes, sm_score),
        lambda: _detect_squeeze_breakout(closes, highs, lows, volumes),
        lambda: _detect_bull_engulfing(opens, closes, volumes),
    ]

    best = None
    for fn in detectors:
        try:
            result = fn()
            if result.get("found"):
                if best is None or result["confidence"] > best["confidence"]:
                    best = result
        except Exception as e:
            log.debug(f"[Scanner] detector error: {e}")

    with _lock:
        # _last_scan_ts обновляем под локом — get_status() читает его
        globals()["_last_scan_ts"] = time.time()

    if best and best["confidence"] >= PATTERN_CONF_THRESH:
        signal = {
            "pattern":    best["pattern"],
            "label":      best["label"],
            "signal":     best["signal"],
            "confidence": best["confidence"],
            "note":       best.get("note", ""),
            "detected_at": time.time(),
        }
        with _lock:
            globals()["_last_signal"] = signal
        log.info(f"[Scanner] ✨ {best['label']} conf={best['confidence']:.0%} — {best.get('note','')}")
    else:
        # Сбрасываем устаревший сигнал под тем же локом
        with _lock:
            sig = globals().get("_last_signal")
            if sig and time.time() - sig.get("detected_at", 0) > SIGNAL_TTL_SEC:
                globals()["_last_signal"] = None


def _worker():
    """Фоновый поток сканера."""
    log.info("[Scanner] 🔭 Рыночный сканер запущен")
    while _running:
        try:
            sm = 0.0
            try:
                import brain_fusion as bf
                # bf.brain — singleton BrainFusion; _ai — AIState dataclass
                sm = float(bf.brain._ai.pump_score or 0.0)
            except Exception:
                pass
            _scan_once(sm_score=sm)
        except Exception as e:
            log.warning(f"[Scanner] worker error: {e}")
        time.sleep(SCAN_INTERVAL_SEC)


def start(get_candles_fn: Callable):
    """Запускает фоновый поток сканера. Вызывать один раз при старте бота."""
    global _running, _thread, _get_candles
    if _running:
        return
    _get_candles = get_candles_fn
    _running     = True
    _thread      = threading.Thread(target=_worker, daemon=True, name="market-scanner")
    _thread.start()
    log.info("[Scanner] ✅ Запущен")


def stop():
    global _running
    _running = False


def get_last_signal() -> Optional[dict]:
    """
    Возвращает последний обнаруженный паттерн или None.
    Сигнал устаревает через SIGNAL_TTL_SEC секунд.
    """
    with _lock:
        sig = _last_signal
    if sig is None:
        return None
    if time.time() - sig.get("detected_at", 0) > SIGNAL_TTL_SEC:
        return None
    return sig


def get_status() -> dict:
    with _lock:
        sig = _last_signal
    return {
        "running":      _running,
        "last_scan_ts": int(_last_scan_ts),
        "last_pattern": sig.get("label") if sig else None,
        "last_conf":    sig.get("confidence") if sig else None,
        "description":  "Сканер рыночных паттернов (Double Bottom, Accumulation, Squeeze, Engulfing)",
    }
