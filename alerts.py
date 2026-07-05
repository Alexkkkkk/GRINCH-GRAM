"""
alerts.py — оповещения о состоянии торгового бота в Telegram.

Не тянем тяжёлую библиотеку python-telegram-bot ради одного метода —
используем Bot API напрямую через общую HTTP-сессию (http_client.SESSION).

Логика:
- send_alert(text) — отправить сообщение прямо сейчас (используется и вручную,
  и из монитора).
- start_monitor() — фоновый поток, который раз в 20с смотрит на реальное
  состояние торгового цикла (trader.last_tick_ts / trader.last_tick_ok) и
  отправляет алерт ТОЛЬКО при смене состояния (healthy → unhealthy/degraded
  и обратно на healthy), чтобы не заспамить чат одним и тем же сообщением
  каждые 20 секунд.
"""
import logging
import threading
import time

import settings_store
from http_client import SESSION

logger = logging.getLogger(__name__)

_STALL_THRESHOLD_SEC = 90  # синхронизировано с порогом в app.py /health
_POLL_INTERVAL_SEC = 20

_lock = threading.Lock()
_last_state = "unknown"   # "ok" | "degraded" | "unhealthy" | "unknown"
_last_sent_ts = 0.0
_MIN_RESEND_GAP = 300      # не слать повторно то же нездоровое состояние чаще, чем раз в 5 мин


def _get_creds():
    sec = settings_store.get_section("alerts")
    token   = (sec.get("telegram_bot_token") or "").strip()
    chat_id = (sec.get("telegram_chat_id") or "").strip()
    enabled = bool(sec.get("enabled", True)) and bool(token) and bool(chat_id)
    return token, chat_id, enabled


def send_alert(text: str) -> dict:
    """Отправить сообщение в Telegram. Возвращает {"ok": bool, "error"?: str}."""
    token, chat_id, enabled = _get_creds()
    if not token or not chat_id:
        return {"ok": False, "error": "Telegram не настроен (нет токена/chat_id)"}
    try:
        resp = SESSION.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description", "неизвестная ошибка Telegram")}
        return {"ok": True}
    except Exception as e:
        logger.warning(f"[Alerts] Telegram send error: {e}")
        return {"ok": False, "error": str(e)}


def _compute_state():
    """Определить текущее состояние торгового цикла (та же логика, что в /health)."""
    from app import trader
    if not trader.running:
        return "ok"
    if trader.last_tick_ts == 0:
        return "ok"  # предобучение
    age = time.time() - trader.last_tick_ts
    if age > _STALL_THRESHOLD_SEC:
        return "unhealthy"
    if trader.last_tick_ok is False:
        return "degraded"
    return "ok"


def _monitor_loop():
    global _last_state, _last_sent_ts
    time.sleep(30)  # дать боту время на предобучение перед первой проверкой
    while True:
        try:
            token, chat_id, enabled = _get_creds()
            state = _compute_state()
            with _lock:
                prev = _last_state
                changed = state != prev
                now = time.time()
                should_send = enabled and (
                    (changed and state != "ok") or
                    (changed and prev != "ok" and state == "ok") or
                    (not changed and state != "ok" and (now - _last_sent_ts) >= _MIN_RESEND_GAP)
                )
                _last_state = state
            if should_send:
                if state == "unhealthy":
                    msg = "🔴 <b>QuantumBrain: торговый цикл завис!</b>\nБот не тикает более 90 секунд — сделки не исполняются."
                elif state == "degraded":
                    msg = "🟡 <b>QuantumBrain: сбой в тике торгового цикла.</b>\nЦикл продолжает работать, но последняя итерация завершилась с ошибкой. Проверьте логи."
                else:
                    msg = "🟢 <b>QuantumBrain: торговый цикл восстановлен.</b>\nБот снова работает штатно."
                result = send_alert(msg)
                if result.get("ok"):
                    with _lock:
                        _last_sent_ts = time.time()
        except Exception as e:
            logger.warning(f"[Alerts] monitor loop error: {e}")
        time.sleep(_POLL_INTERVAL_SEC)


_monitor_started = False
_monitor_lock = threading.Lock()


def start_monitor():
    global _monitor_started
    with _monitor_lock:
        if _monitor_started:
            return
        _monitor_started = True
        threading.Thread(target=_monitor_loop, daemon=True).start()
