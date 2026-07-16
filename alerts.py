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
    _monitor_stop.wait(timeout=30)   # прерываемая начальная пауза
    while not _monitor_stop.is_set():
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
        _monitor_stop.wait(timeout=_POLL_INTERVAL_SEC)   # прерываемый сон


_monitor_started = False
_monitor_lock    = threading.Lock()
_monitor_stop    = threading.Event()   # мгновенная остановка монитора


def start_monitor():
    global _monitor_started
    with _monitor_lock:
        if _monitor_started:
            return
        _monitor_started = True
    _monitor_stop.clear()
    threading.Thread(target=_monitor_loop, daemon=True,
                     name="alerts-monitor").start()


def stop_monitor():
    """Останавливает монитор алертов мгновенно."""
    global _monitor_started
    with _monitor_lock:
        _monitor_started = False
    _monitor_stop.set()


# ══════════════════════════════════════════════════════════════════════════════
#  HOURLY REPORT — раз в час пишет полный снимок состояния бота в лог и файл
# ══════════════════════════════════════════════════════════════════════════════

_REPORT_INTERVAL_SEC = 3600   # раз в час
_REPORT_FILE = None           # устанавливается в start_hourly_report()
_hourly_stop = threading.Event()
_hourly_started = False
_hourly_lock = threading.Lock()


def _build_report() -> str:
    """Собирает полный снимок состояния бота. Никогда не кидает исключений."""
    lines = []
    try:
        import os, resource
        from db_store import ai_state_get_all, open_trades_get
        from price_feed import price_feed

        lines.append("═" * 50)
        lines.append(f"📊 HOURLY REPORT  {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("═" * 50)

        # ── Статистика ─────────────────────────────────────────────────
        ai  = ai_state_get_all() or {}
        stats = ai.get("stats") or {}
        total   = int(stats.get("total_trades") or 0)
        winning = int(stats.get("winning_trades") or 0)
        pnl     = float(stats.get("total_pnl") or 0)
        winrate = round(winning / total * 100, 1) if total else 0
        lines.append(f"📈 Сделок: {total}  |  Побед: {winning} ({winrate}%)  |  PnL: +{pnl:.4f} TON")

        # ── Открытые позиции ────────────────────────────────────────────
        ots = open_trades_get() or []
        if ots:
            for t in ots:
                spot     = price_feed.get("GRINCH") or 0
                gton     = price_feed.get_grinch_ton_price() or 0
                amount   = float(t.get("amount") or 0)
                entry    = float(t.get("entry_price") or 0)
                stake    = float(t.get("stake_ton") or 0)
                val_ton  = amount * gton
                net      = float(t.get("net_pct_now") or 0)
                peak     = float(t.get("high_water") or entry)
                move_pct = (spot / entry - 1) * 100 if entry else 0
                lines.append(
                    f"📌 ЛОНГ {amount:,.0f} GRINCH @ ${entry:.8f}"
                    f"  |  сейчас ${spot:.8f} ({move_pct:+.2f}%)"
                )
                lines.append(
                    f"   Ставка {stake:.2f} TON  |  Стоит {val_ton:.4f} TON"
                    f"  |  Нетто {net:+.2f}%"
                )
                lines.append(f"   Пик: ${peak:.8f}  |  dca_entry:{t.get('dca_entry')} idx:{t.get('dca_index')}")
        else:
            lines.append("📌 Открытых позиций нет")

        # ── DCA состояние ───────────────────────────────────────────────
        try:
            from app import trader
            lines.append(
                f"🔄 DCA: entries={trader.dca_entries_count}"
                f"  stake={trader.dca_total_stake:.2f} TON"
                f"  wait_pullback={trader.dca_wait_pullback}"
                f"  peak=${trader.dca_peak_price:.8f}"
            )
            age = time.time() - trader.last_tick_ts if trader.last_tick_ts else 0
            lines.append(f"⏱  Последний тик: {age:.1f} сек назад  |  ok={trader.last_tick_ok}")
        except Exception as _e:
            lines.append(f"⚠️  DCA/tick: {_e}")

        # ── RAM ─────────────────────────────────────────────────────────
        try:
            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            lines.append(f"💾 RAM: {rss_mb:.1f} MB")
        except Exception:
            pass

        # ── Health ──────────────────────────────────────────────────────
        try:
            from http_client import SESSION
            r = SESSION.get("http://localhost:3000/health", timeout=5)
            h = r.json()
            lines.append(
                f"🏥 Health: {h.get('status')}  |  tick_age={h.get('seconds_since_last_tick',0):.1f}s"
                f"  |  rss={h.get('rss_mb',0):.1f}MB"
            )
        except Exception as _e:
            lines.append(f"🏥 Health: недоступен ({_e})")

    except Exception as _outer:
        lines.append(f"⚠️  report build error: {_outer}")

    lines.append("═" * 50)
    return "\n".join(lines)


def _hourly_loop():
    """Фоновый поток: собирает отчёт раз в час, пишет в лог + файл + Telegram."""
    # Первый отчёт через 60 сек после старта (не сразу — дать боту прогреться)
    _hourly_stop.wait(timeout=60)
    while not _hourly_stop.is_set():
        try:
            report = _build_report()

            # 1) В стандартный лог бота (видно в дашборде)
            for line in report.split("\n"):
                logger.info(line)

            # 2) В файл на диске (переживёт рестарт)
            if _REPORT_FILE:
                try:
                    os.makedirs(os.path.dirname(_REPORT_FILE), exist_ok=True)
                    with open(_REPORT_FILE, "a", encoding="utf-8") as f:
                        f.write(report + "\n")
                except Exception as _fe:
                    logger.warning(f"[HourlyReport] file write error: {_fe}")

            # 3) В Telegram (если настроен)
            token, chat_id, enabled = _get_creds()
            if enabled:
                send_alert(report)

        except Exception as _e:
            logger.warning(f"[HourlyReport] ошибка: {_e}")

        _hourly_stop.wait(timeout=_REPORT_INTERVAL_SEC)


def start_hourly_report(data_dir: str = "/app/data"):
    """Запускает фоновый поток hourly report. Вызывается один раз из app.py."""
    global _hourly_started, _REPORT_FILE
    with _hourly_lock:
        if _hourly_started:
            return
        _hourly_started = True
    import os
    _REPORT_FILE = os.path.join(data_dir, "hourly_report.log")
    _hourly_stop.clear()
    threading.Thread(target=_hourly_loop, daemon=True,
                     name="hourly-report").start()
    logger.info(f"[HourlyReport] ✅ Запущен (каждые 60 мин → {_REPORT_FILE})")
