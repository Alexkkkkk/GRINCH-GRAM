"""
liquidity_guard.py — постоянный мониторинг ликвидности пула GRINCH/TON.

Работает в фоне 24/7: следит за liquidity (DexScreener), держит историю и
пик, и если ликвидность резко проседает (например кит вывел ликвидность,
или рынок обвалился) — автоматически ставит новые BUY на паузу, пока
ликвидность не восстановится. Продажи НИКОГДА не блокируются (fail-safe:
деньги пользователей не должны застревать).
"""
import logging
import threading
import time
from collections import deque

from config import Config
from coin_info import coin_info

logger = logging.getLogger(__name__)

POLL_SEC          = 15     # частота опроса
HISTORY_MAXLEN    = 240    # ~1 час истории при 15с шаге
DROP_PAUSE_PCT    = 30.0   # просадка от пика → пауза на BUY
DROP_RESUME_PCT   = 15.0   # гистерезис восстановления → снятие паузы
MIN_SAFE_LIQ_USD  = 5000.0 # абсолютный пол ликвидности — ниже него BUY тоже на паузе

_lock = threading.Lock()
_history: deque = deque(maxlen=HISTORY_MAXLEN)   # [{ts, liq}, ...]
_peak_liq: float = 0.0
_current_liq: float = 0.0
_buys_paused: bool = False
_pause_reason: str = ""
_last_update_ts: float = 0.0
_started = False
_stop_event = threading.Event()   # мгновенная остановка потока

# BUG-FIX: защита от API-глитча.
# Если DexScreener вернул разово заниженное (но ненулевое) значение,
# одного чтения достаточно чтобы drop_pct ≥ 30% → _buys_paused = True.
# Счётчик требует CONSECUTIVE_LOW_REQUIRED последовательных «плохих» чтений
# перед тем как поставить BUY на паузу. Одно аномальное значение игнорируется.
CONSECUTIVE_LOW_REQUIRED = 2
_consecutive_low_count: int = 0


def _evaluate(liq: float):
    global _peak_liq, _buys_paused, _pause_reason, _consecutive_low_count
    if liq is None or liq <= 0:
        return
    if liq > _peak_liq:
        _peak_liq = liq
        _consecutive_low_count = 0   # новый пик — сброс счётчика

    drop_pct = 0.0
    if _peak_liq > 0:
        drop_pct = (1 - liq / _peak_liq) * 100.0

    if not _buys_paused:
        is_low = liq < MIN_SAFE_LIQ_USD or drop_pct >= DROP_PAUSE_PCT
        if is_low:
            _consecutive_low_count += 1
            if _consecutive_low_count >= CONSECUTIVE_LOW_REQUIRED:
                _buys_paused = True
                if liq < MIN_SAFE_LIQ_USD:
                    _pause_reason = f"ликвидность ${liq:,.0f} ниже безопасного порога ${MIN_SAFE_LIQ_USD:,.0f}"
                else:
                    _pause_reason = f"просадка ликвидности {drop_pct:.1f}% от пика ${_peak_liq:,.0f} → ${liq:,.0f}"
                logger.warning(f"[LiquidityGuard] ⛔ BUY приостановлены: {_pause_reason}")
            else:
                logger.warning(
                    f"[LiquidityGuard] ⚠️ Низкая ликвидность ${liq:,.0f} "
                    f"(просадка {drop_pct:.1f}%) — подтверждение {_consecutive_low_count}/{CONSECUTIVE_LOW_REQUIRED}"
                )
        else:
            _consecutive_low_count = 0   # чтение нормальное — сброс
    else:
        recovered = liq >= MIN_SAFE_LIQ_USD and drop_pct <= DROP_RESUME_PCT
        if recovered:
            logger.info(f"[LiquidityGuard] ✅ Ликвидность восстановилась (${liq:,.0f}, просадка {drop_pct:.1f}%) — BUY разрешены")
            _buys_paused = False
            _pause_reason = ""
            _consecutive_low_count = 0
            _peak_liq = liq  # новый пик отсчитываем от текущего восстановленного уровня


def _poll_loop():
    global _current_liq, _last_update_ts
    logger.info("[LiquidityGuard] 🟢 Мониторинг ликвидности GRINCH запущен")
    while not _stop_event.is_set():
        try:
            data = coin_info.market("GRINCH") or {}
            liq = data.get("liquidity")
            if liq:
                with _lock:
                    _current_liq = float(liq)
                    _last_update_ts = time.time()
                    _history.append({"ts": _last_update_ts, "liq": _current_liq})
                    _evaluate(_current_liq)
        except Exception as e:
            logger.error(f"[LiquidityGuard] ошибка опроса: {e}")
        _stop_event.wait(timeout=POLL_SEC)   # прерываемый сон


def start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    _stop_event.clear()
    threading.Thread(target=_poll_loop, daemon=True, name="liquidity-guard").start()


def stop():
    """Останавливает мониторинг мгновенно."""
    global _started
    with _lock:
        _started = False
    _stop_event.set()


def is_buy_paused() -> bool:
    with _lock:
        return _buys_paused


def get_status() -> dict:
    with _lock:
        drop_pct = (1 - _current_liq / _peak_liq) * 100.0 if _peak_liq > 0 else 0.0
        return {
            "current_liq":   round(_current_liq, 2),
            "peak_liq":      round(_peak_liq, 2),
            "drop_pct":      round(max(0.0, drop_pct), 2),
            "buys_paused":   _buys_paused,
            "pause_reason":  _pause_reason,
            "min_safe_liq":  MIN_SAFE_LIQ_USD,
            "pause_threshold_pct": DROP_PAUSE_PCT,
            "history":       list(_history)[-60:],
            "last_update_ts": _last_update_ts,
        }


start()
