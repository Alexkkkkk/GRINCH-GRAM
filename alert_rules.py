"""
alert_rules.py — расширенный движок алертов с гибкими правилами.

Назначение
----------
alerts.py уже покрывает health-мониторинг торгового цикла (зависание,
ошибки тика) — эту логику не трогаем, она критична для прод-бота.
alert_rules.py добавляет ВТОРОЙ, независимый набор правил поверх событий
бизнес-уровня (закрытие сделки, просадка портфеля, готовность бэктеста) —
использует тот же send_alert() из alerts.py как транспорт, но с отдельной
логикой срабатывания и отдельным анти-спам состоянием, чтобы не
конфликтовать со health-монитором.

Правила по умолчанию
---------------------
- trade_closed: net_pct >= BIG_WIN_PCT      → "крупная победа"
- trade_closed: net_pct <= BIG_LOSS_PCT     → "крупный убыток" (даже с ONLY_PROFIT_EXIT
                                                может быть 0, но правило общее для paper/backtest)
- drawdown: max_drawdown_pct >= DD_ALERT_PCT → предупреждение о просадке
- backtest_done: сводка после ручного запуска backtest.py/paper_trading.py

Все правила READ-ONLY по отношению к торговле — только читают события и
шлют уведомления, ничего не меняют в конфиге/БД.

Использование
-------------
    from alert_rules import notify_trade_closed, notify_drawdown, notify_backtest_done

    notify_trade_closed({"net_pct": 24.5, "symbol": "GRINCH/TON"})
    notify_drawdown(current_dd_pct=12.3)
    notify_backtest_done(result_dict)
"""

from __future__ import annotations

import logging
import threading
import time

import alerts  # переиспользуем send_alert() как единственный транспорт

logger = logging.getLogger(__name__)

BIG_WIN_PCT = 15.0
BIG_LOSS_PCT = -8.0
DD_ALERT_PCT = 20.0
_DD_RESEND_GAP_SEC = 1800  # не долбить одним и тем же предупреждением о просадке чаще 30 мин

_lock = threading.Lock()
_last_dd_alert_ts = 0.0
_last_dd_alert_level = 0.0


def notify_trade_closed(trade: dict) -> None:
    """Вызывать при закрытии сделки (реальной, paper или в бэктесте, если
    нужен алерт из ручного прогона). Не путать с ai_advisor.notify_trade_closed —
    та функция кормит статистику советника, эта — только уведомления."""
    net_pct = trade.get("net_pct")
    if net_pct is None:
        return
    symbol = trade.get("symbol", "GRINCH/TON")
    if net_pct >= BIG_WIN_PCT:
        alerts.send_alert(
            f"💰 <b>Крупная победа</b> по {symbol}: +{net_pct:.1f}% нетто."
        )
    elif net_pct <= BIG_LOSS_PCT:
        alerts.send_alert(
            f"⚠️ <b>Крупный убыток</b> по {symbol}: {net_pct:.1f}% нетто."
        )


def notify_drawdown(current_dd_pct: float) -> None:
    """Вызывать периодически с текущей просадкой портфеля (реальной или
    виртуальной). Шлёт алерт только когда просадка впервые превышает порог
    и не чаще раза в 30 минут для одного и того же уровня."""
    global _last_dd_alert_ts, _last_dd_alert_level
    if current_dd_pct < DD_ALERT_PCT:
        return
    now = time.time()
    with _lock:
        should_send = (now - _last_dd_alert_ts) >= _DD_RESEND_GAP_SEC or current_dd_pct > _last_dd_alert_level + 5.0
        if should_send:
            _last_dd_alert_ts = now
            _last_dd_alert_level = current_dd_pct
    if should_send:
        alerts.send_alert(
            f"📉 <b>Просадка портфеля {current_dd_pct:.1f}%</b> — выше порога {DD_ALERT_PCT:.0f}%."
        )


def notify_backtest_done(result: dict) -> None:
    """Вызывать после ручного запуска backtest.run_backtest(...).to_dict()."""
    n_trades = result.get("n_trades", 0)
    ret = result.get("total_return_pct", 0.0)
    wr = result.get("win_rate_pct", 0.0)
    dd = result.get("max_drawdown_pct", 0.0)
    alerts.send_alert(
        f"📊 <b>Бэктест завершён</b>: {n_trades} сделок, доходность {ret:+.1f}%, "
        f"win rate {wr:.0f}%, макс. просадка {dd:.1f}%."
    )
