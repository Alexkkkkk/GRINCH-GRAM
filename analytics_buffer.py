"""
analytics_buffer.py — Аналитика торгового бота на основе PostgreSQL.

Раньше это был in-memory кольцевой буфер (deque), который полностью терял
историю тиков и сделок при каждом рестарте процесса. Теперь тики пишутся
в таблицу bot_ticks (db_store.py) и переживают перезапуск, а статистика по
сделкам строится из постоянной таблицы bot_trades — единственного источника
истины по закрытым сделкам (пишется из experience_manager.record_trade()).

Публичный API сохранён без изменений (push_tick, get_advisor_summary,
tick_count, ...), поэтому вызывающий код в trader.py/app.py/ai_advisor.py
не пришлось менять — изменилась только реализация хранения.

DeDust GRINCH: https://dedust.io/coins/EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL
Token address: EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL
"""
from __future__ import annotations

import math
import logging
from datetime import datetime
from typing import Dict, List, Optional

import db_store as _db

logger = logging.getLogger(__name__)

GRINCH_DEDUST_URL  = "https://dedust.io/coins/EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL"
GRINCH_TOKEN_ADDR  = "EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL"

DEFAULT_TICK_WINDOW = 100
TRADE_WINDOW         = 30


class AnalyticsBuffer:
    """
    Центральный хаб аналитики торгового бота GRINCH-GRAM — теперь без
    собственного состояния в памяти. Тики читаются/пишутся через bot_ticks,
    статистика по сделкам — через bot_trades (обе таблицы в PostgreSQL).
    """

    # ─── Публичный API записи ─────────────────────────────────────────────────

    def push_tick(self, data: dict) -> None:
        """
        Вызывается из trader._tick() / trader._tick_dca() каждый тик (≈15 сек).
        Записывает полный снимок рыночного состояния в БД (bot_ticks).
        """
        entry = {
            "ts":             datetime.utcnow().strftime("%H:%M:%S"),
            # ── Цена ──────────────────────────────────────────────────────────
            "p_usd":          _sf(data.get("price_usd")),
            "p_ton":          _sf(data.get("price_ton")),
            # ── Технические индикаторы ─────────────────────────────────────────
            "rsi":            _sf(data.get("rsi"), 50.0),
            "adx":            _sf(data.get("adx")),
            "atr_pct":        _sf(data.get("atr_pct")),
            "bb_pct":         _sf(data.get("bb_pct")),
            "vol_ratio":      _sf(data.get("vol_ratio"), 1.0),
            "macd_hist":      _sf(data.get("macd_hist")),
            "stoch_rsi":      _sf(data.get("stoch_rsi"), 0.5),
            # ── AI & режим рынка ──────────────────────────────────────────────
            "regime":         str(data.get("regime") or "?"),
            "ai_sig":         str(data.get("ai_signal") or "HOLD"),
            "ai_conf":        _sf(data.get("ai_conf")),
            "prob_up":        _sf(data.get("prob_up")),
            "prob_down":      _sf(data.get("prob_down")),
            "var_ratio":      _sf(data.get("var_ratio"), 1.0),
            "pump":           str(data.get("pump") or "NONE"),
            "anomaly":        bool(data.get("anomaly")),
            # ── Momentum & Breakout ───────────────────────────────────────────
            "mom":            str(data.get("momentum") or "CALM"),
            "bo":             str(data.get("breakout") or "FLAT"),
            "eq":             str(data.get("entry_quality") or "?"),   # A/B/C
            "eq_score":       int(data.get("entry_score") or 0),
            # ── Умные деньги ──────────────────────────────────────────────────
            "sm":             _sf(data.get("sm_score")),
            "sm_early":       bool(data.get("sm_early")),
            # ── Итоговое решение ──────────────────────────────────────────────
            "final":          str(data.get("final_signal") or "HOLD"),
            "blocked":        bool(data.get("blocked")),
            "block_reason":   str(data.get("blocked_reason") or "")[:50],
            # ── Портфель & ликвидность ────────────────────────────────────────
            "pos":            int(data.get("open_positions") or 0),
            "pnl_session":    _sf(data.get("portfolio_pnl")),
            "ton_bal":        _sf(data.get("ton_balance")),
            "liq_usd":        _sf(data.get("liq_usd")),
            # ── DCA цикл ─────────────────────────────────────────────────────
            "dca_n":          int(data.get("dca_entries") or 0),
            "dca_avg_p":      _sf(data.get("dca_avg_price")),
            "dca_pct":        _sf(data.get("dca_profit_pct")),
            "dca_ton":        _sf(data.get("dca_profit_ton")),
        }
        _db.ticks_insert(entry)

    def push_trade(self, event_type: str, data: dict) -> None:
        """
        Оставлен для обратной совместимости вызовов из trader.py.
        Реальная запись закрытых сделок уже происходит через
        experience_manager.record_trade() → db_store.trades_upsert()
        (таблица bot_trades), поэтому здесь дополнительного хранения не
        требуется — no-op, чтобы не дублировать источники истины.
        """
        return

    # ─── Аналитика для советника ──────────────────────────────────────────────

    def get_advisor_summary(self, window: int = DEFAULT_TICK_WINDOW) -> dict:
        """
        Компактный аналитический отчёт для Groq AI-советника.
        window — кол-во последних тиков (100 тиков ≈ 25 мин при 15-сек тике).

        Возвращает структурированный dict со всеми ключевыми метриками:
        ценовой тренд, индикаторы, режимы, качество AI-сигналов, DCA-прогресс,
        умные деньги, история сделок и список последних 12 тиков.
        Данные читаются из PostgreSQL (bot_ticks/bot_trades) — переживают
        рестарт бота, в отличие от прежнего in-memory буфера.
        """
        ticks = _db.ticks_get_recent(window)
        raw_trades = _db.trades_get_recent(TRADE_WINDOW)
        trades = [_trade_from_db(t) for t in reversed(raw_trades)]  # ASC как раньше

        if not ticks:
            return {"status": "данных пока нет — БД пуста или недоступна", "ticks": 0}

        n = len(ticks)

        # ── Ценовой ряд ───────────────────────────────────────────────────────
        prices = [t["p_usd"] for t in ticks if t["p_usd"] > 0]
        p_ton  = [t["p_ton"] for t in ticks if t["p_ton"] > 0]

        price_block: dict = {}
        if prices:
            chg = _pct_change(prices[0], prices[-1])
            # Мини-свечи: делим ценовой ряд на 8 блоков для визуального тренда
            block = max(1, len(prices) // 8)
            mini: List[dict] = []
            for i in range(0, len(prices), block):
                ch = prices[i:i+block]
                if ch:
                    mini.append({
                        "o": _fp(ch[0]), "h": _fp(max(ch)),
                        "l": _fp(min(ch)), "c": _fp(ch[-1]),
                        "Δ%": round(_pct_change(ch[0], ch[-1]), 2),
                    })
            # Подсчёт восходящих/нисходящих тиков
            up   = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i-1])
            down = sum(1 for i in range(1, len(prices)) if prices[i] < prices[i-1])
            price_block = {
                "open":     _fp(prices[0]),
                "current":  _fp(prices[-1]),
                "high":     _fp(max(prices)),
                "low":      _fp(min(prices)),
                "change_%": round(chg, 3),
                "direction": "↑ РОСТ" if chg > 0.5 else "↓ ПАДЕНИЕ" if chg < -0.5 else "→ БОКОВИК",
                "std_%":    _std_pct(prices),
                "up_ticks": up,
                "dn_ticks": down,
                "mini_candles": mini[-8:],
            }
            if p_ton:
                price_block["p_ton_current"] = round(p_ton[-1], 10)

        # ── Индикаторы ────────────────────────────────────────────────────────
        def _ind(key: str, default=50.0) -> dict:
            vals = [t[key] for t in ticks if t.get(key, 0) != 0]
            if not vals:
                return {"cur": 0, "avg": 0}
            return {"cur": round(vals[-1], 3), "avg": round(_mean(vals), 3),
                    "min": round(min(vals), 3), "max": round(max(vals), 3)}

        indicators = {
            "rsi":      _ind("rsi"),
            "adx":      _ind("adx"),
            "atr_%":    _ind("atr_pct"),
            "vol_ratio":_ind("vol_ratio"),
            "bb_%":     _ind("bb_pct"),
            "stoch_rsi":_ind("stoch_rsi"),
            "var_ratio":_ind("var_ratio"),
        }

        # ── Режимы рынка ──────────────────────────────────────────────────────
        reg_counts: Dict[str, int] = {}
        for t in ticks:
            r = t["regime"]
            reg_counts[r] = reg_counts.get(r, 0) + 1
        reg_dist = {k: round(v/n*100, 1)
                    for k, v in sorted(reg_counts.items(), key=lambda x: -x[1])}
        dominant = max(reg_counts, key=reg_counts.get) if reg_counts else "?"

        regime_block = {
            "current":  ticks[-1]["regime"],
            "dominant": dominant,
            "dist_%":   reg_dist,
        }

        # ── AI сигналы ────────────────────────────────────────────────────────
        sig_counts: Dict[str, int] = {"BUY": 0, "SELL": 0, "HOLD": 0}
        blocked_n = 0
        block_reasons: Dict[str, int] = {}
        eq_dist: Dict[str, int]       = {"A": 0, "B": 0, "C": 0}
        pump_events = []

        for t in ticks:
            sig_counts[t["final"]] = sig_counts.get(t["final"], 0) + 1
            if t["blocked"]:
                blocked_n += 1
                br = t["block_reason"][:35] if t["block_reason"] else "?"
                block_reasons[br] = block_reasons.get(br, 0) + 1
            q = t["eq"]
            if q in eq_dist:
                eq_dist[q] += 1
            if t["pump"] not in ("NONE", "?", ""):
                pump_events.append({"ts": t["ts"], "pump": t["pump"], "conf": round(t["ai_conf"], 1)})

        confs  = [t["ai_conf"] for t in ticks if t["ai_conf"] > 0]
        pups   = [t["prob_up"] for t in ticks if t["prob_up"] > 0]

        ai_block = {
            "cur_conf":    round(ticks[-1]["ai_conf"], 1),
            "avg_conf":    round(_mean(confs), 1),
            "cur_prob_up": round(ticks[-1]["prob_up"], 3),
            "avg_prob_up": round(_mean(pups), 3),
            "cur_signal":  ticks[-1]["ai_sig"],
            "buy_rate_%":  round(sig_counts["BUY"] / n * 100, 1),
            "sig_dist":    sig_counts,
            "blocked_%":   round(blocked_n / n * 100, 1),
            "top_blocks":  [k for k, _ in
                           sorted(block_reasons.items(), key=lambda x: -x[1])[:4]],
            "eq_dist":     eq_dist,
            "pump_events": pump_events[-4:],
        }

        # ── Умные деньги ─────────────────────────────────────────────────────
        sm_vals = [t["sm"] for t in ticks]
        sm_block = {
            "cur":         round(ticks[-1]["sm"], 3),
            "avg":         round(_mean(sm_vals), 3),
            "min":         round(min(sm_vals), 3) if sm_vals else 0,
            "max":         round(max(sm_vals), 3) if sm_vals else 0,
            "early_cnt":   sum(1 for t in ticks if t["sm_early"]),
        }

        # ── Momentum & Breakout ───────────────────────────────────────────────
        mom_counts: Dict[str, int] = {}
        bo_counts:  Dict[str, int] = {}
        for t in ticks:
            mom_counts[t["mom"]] = mom_counts.get(t["mom"], 0) + 1
            bo_counts[t["bo"]]   = bo_counts.get(t["bo"], 0) + 1

        momentum_block = {
            "cur_mom":   ticks[-1]["mom"],
            "cur_bo":    ticks[-1]["bo"],
            "mom_dist":  dict(sorted(mom_counts.items(), key=lambda x: -x[1])),
            "bo_dist":   dict(sorted(bo_counts.items(),  key=lambda x: -x[1])),
        }

        # ── DCA аналитика ─────────────────────────────────────────────────────
        dca_ticks = [t for t in ticks if t["pos"] > 0]
        dca_block = None
        if dca_ticks:
            pp  = [t["dca_pct"] for t in dca_ticks if t["dca_pct"] != 0]
            pt  = [t["dca_ton"] for t in dca_ticks if t["dca_ton"] != 0]
            dca_block = {
                "cur_entries":  dca_ticks[-1]["dca_n"],
                "cur_profit_%": round(dca_ticks[-1]["dca_pct"], 3),
                "cur_profit_ton": round(dca_ticks[-1]["dca_ton"], 4),
                "max_profit_%": round(max(pp), 3) if pp else 0,
                "min_profit_%": round(min(pp), 3) if pp else 0,
                "avg_profit_%": round(_mean(pp), 3),
                "max_profit_ton": round(max(pt), 4) if pt else 0,
                "holding_ticks": len(dca_ticks),
            }

        # ── Ликвидность ───────────────────────────────────────────────────────
        liq_vals = [t["liq_usd"] for t in ticks if t["liq_usd"] > 0]
        liq_block = {}
        if liq_vals:
            liq_block = {
                "cur":  round(liq_vals[-1], 0),
                "avg":  round(_mean(liq_vals), 0),
                "min":  round(min(liq_vals), 0),
                "max":  round(max(liq_vals), 0),
                "trend": "↑" if liq_vals[-1] > _mean(liq_vals[:len(liq_vals)//2] or liq_vals) else "↓",
            }

        # ── История сделок из bot_trades (закрытые, ВСЕ по определению) ───────
        closed = [tr for tr in trades if tr["type"] in ("CLOSE", "DCA_SELL")]
        wins   = [tr for tr in closed if tr["pnl_ton"] > 0]
        losses = [tr for tr in closed if tr["pnl_ton"] <= 0]
        trade_block = {
            "recent_n":    len(closed),
            "wins":        len(wins),
            "losses":      len(losses),
            "win_%":       round(len(wins)/max(1,len(closed))*100, 1),
            "avg_win_ton": round(_mean([t["pnl_ton"] for t in wins]), 4),
            "avg_loss_ton":round(_mean([t["pnl_ton"] for t in losses]), 4),
            "best_regime": _mode([t["regime"] for t in wins]) if wins else "?",
            "worst_regime":_mode([t["regime"] for t in losses]) if losses else "?",
            "recent_events": [
                {"ts": tr["ts"], "type": tr["type"], "pnl": round(tr["pnl_ton"], 4),
                 "pnl%": round(tr["pnl_pct"], 2), "regime": tr["regime"],
                 "reason": tr["reason"][:20]}
                for tr in trades[-8:]
            ],
        }

        # ── Последние 12 тиков (компактно — для "взгляда в прошлое") ──────────
        recent = [
            {
                "ts":    t["ts"],
                "p":     _fp(t["p_usd"]),
                "rsi":   round(t["rsi"], 1),
                "reg":   t["regime"],
                "sig":   t["ai_sig"],
                "conf":  round(t["ai_conf"], 1),
                "pump":  t["pump"] if t["pump"] not in ("NONE", "") else "",
                "sm":    round(t["sm"], 2),
                "mom":   t["mom"],
                "pnl%":  round(t["dca_pct"], 2),
                "fin":   t["final"],
                "blk":   t["block_reason"][:20] if t["blocked"] else "",
            }
            for t in ticks[-12:]
        ]

        # ── Итоговый блок ─────────────────────────────────────────────────────
        return {
            "token":          GRINCH_TOKEN_ADDR,
            "dedust_url":     GRINCH_DEDUST_URL,
            "window_ticks":   n,
            "window_min":     round(n * 15 / 60, 1),
            "price":          price_block,
            "indicators":     indicators,
            "regime":         regime_block,
            "ai_signals":     ai_block,
            "smart_money":    sm_block,
            "momentum":       momentum_block,
            "dca_analytics":  dca_block,
            "liquidity":      liq_block,
            "trade_stats":    trade_block,
            "recent_ticks":   recent,
            "session_totals": {
                "total_ticks": _db.ticks_count(),
                "total_trades_closed": _db.trades_count(),
            },
        }

    def get_full_summary(self, window: int = 200) -> dict:
        """Полный отчёт — для дашборда/API. Советник использует get_advisor_summary."""
        return self.get_advisor_summary(window=window)

    def tick_count(self) -> int:
        return max(0, _db.ticks_count())

    def trade_count(self) -> int:
        return max(0, _db.trades_count())

    def get_stats(self) -> dict:
        return {
            "total_ticks": _db.ticks_count(),
            "total_trades_closed": _db.trades_count(),
        }


# ─── Преобразование строки bot_trades в формат отчёта ─────────────────────────

def _trade_from_db(t: dict) -> dict:
    """Приводит сырой dict сделки из bot_trades к компактной форме, которую
    раньше строил push_trade() для in-memory буфера."""
    reason = str(t.get("close_reason") or t.get("reason") or "")
    closed_at = str(t.get("closed_at") or "")
    ts = closed_at[11:19] if len(closed_at) >= 19 else closed_at
    return {
        "ts":      ts,
        "type":    "DCA_SELL" if "dca_target" in reason else "CLOSE",
        "price":   _sf(t.get("exit_price")),
        "stake":   _sf(t.get("stake_ton")),
        "pnl_ton": _sf(t.get("pnl")),
        "pnl_pct": _sf(t.get("pnl_pct")),
        "regime":  str(t.get("exit_regime") or t.get("entry_regime") or "?"),
        "ai_conf": _sf(t.get("exit_ai_confidence") or t.get("ai_confidence")),
        "reason":  reason,
        "dca_n":   int(t.get("dca_entries") or 0),
    }


# ─── Математические хелперы ───────────────────────────────────────────────────

def _sf(v, default: float = 0.0) -> float:
    """Safe float — без исключений и NaN.
    None/missing → default; 0 остаётся 0 (не заменяется default).
    """
    if v is None:
        return default
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _mean(vals: list) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _std_pct(vals: list) -> float:
    if len(vals) < 2:
        return 0.0
    avg = _mean(vals)
    if avg == 0:
        return 0.0
    return round((_mean([(v - avg) ** 2 for v in vals]) ** 0.5) / avg * 100, 3)


def _pct_change(start: float, end: float) -> float:
    return round((end - start) / start * 100, 4) if start else 0.0


def _fp(v: float) -> str:
    """Форматирование цены GRINCH с адаптивными десятичными знаками."""
    if not v or not math.isfinite(v):
        return "0"
    if v < 0.000001:
        return f"{v:.10f}"
    if v < 0.0001:
        return f"{v:.8f}"
    if v < 0.01:
        return f"{v:.6f}"
    return f"{v:.4f}"


def _mode(items: list) -> str:
    """Наиболее частый элемент списка."""
    if not items:
        return "?"
    counts: Dict[str, int] = {}
    for it in items:
        counts[it] = counts.get(it, 0) + 1
    return max(counts, key=counts.get)


# ─── Глобальный синглтон (сохранён для совместимости вызовов) ────────────────
analytics_buffer: AnalyticsBuffer = AnalyticsBuffer()
