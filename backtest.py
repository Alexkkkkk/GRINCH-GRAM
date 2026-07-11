"""
backtest.py — Движок бэктестинга стратегии на исторических данных.

Назначение
----------
Прогоняет ТУ ЖЕ техническую стратегию (strategy.py: get_signal + entry-quality
скоринг), что использует боевой trader.py, по историческим свечам GRINCH/TON
с GeckoTerminal, и считает метрики доходности БЕЗ единой реальной сделки.

Почему НЕ подключаем ai_engine (QuantumBrain) сюда
---------------------------------------------------
AIEngine — синглтон с изменяемым внутренним состоянием (модели, калибраторы,
replay-буфер, счётчики переобучения). Прогон backtest'а через него на
исторических данных будет ВЫЗЫВАТЬ РЕАЛЬНОЕ ПЕРЕОБУЧЕНИЕ и МУТИРОВАТЬ те же
модели, что торгуют вживую — то есть испортит боевой AI одним запуском
бэктеста. Поэтому v1 бэктестера использует только strategy.py (чистые
функции без побочных эффектов на глобальное состояние торговли/БД) —
это уже отражает вход/выход, размер позиции и защиту "только в плюс"
(ONLY_PROFIT_EXIT), которые применяет боевой бот.
Добавление AI-сигнала в бэктест — отдельная задача: нужна ОТДЕЛЬНАЯ,
не-синглтон копия ансамбля моделей, обучаемая только на данных ДО точки
симуляции (walk-forward), без записи в состояние живого AIEngine.

Что считает
-----------
- Реплей стратегии bar-by-bar (walk-forward: решение принимается только на
  основе свечей ДО текущего момента — без заглядывания в будущее).
- Симуляция сделок: 1 позиция за раз (как MAX_OPEN_TRADES=1 в Config),
  вход по сигналу BUY strategy.get_signal(), выход по TP/трейлингу или
  по осторожному правилу ONLY_PROFIT_EXIT (никогда не закрывать в минус).
  Комиссия FEE_ROUND_TRIP (обе стороны) применяется к каждой сделке.
- Метрики: total_return_pct, win_rate, trades, avg_win_pct, avg_loss_pct,
  max_drawdown_pct, sharpe_like (на основе returns по сделкам), equity_curve.

Использование
-------------
    python3 backtest.py --days 14 --tf hour --aggregate 1

Ничего не пишет в БД, не исполняет реальные ордера, не трогает
production-таблицы/боевой AI. Безопасно запускать сколько угодно раз.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field

import requests

from config import Config
import strategy


GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2/networks/ton/pools"


def fetch_historical_ohlcv(pool_address: str, days: int = 14, tf: str = "hour",
                            aggregate: int = 1, currency: str = "usd",
                            token: str = "base", page_limit: int = 1000,
                            max_pages: int = 20, sleep_between_pages: float = 0.4):
    """Пагинированная загрузка исторических свечей с GeckoTerminal.

    Использует собственный HTTP-запрос (НЕ ExchangeClient.get_real_ohlcv) —
    у боевого клиента TTL-кэш на 60с рассчитан на последние ~100 свечей для
    живого тика, а бэктесту нужна большая непрерывная история за прошлое.
    Идём назад по `before_timestamp`, пока не наберём нужную глубину в днях
    или не упрёмся в max_pages (защита от лишних запросов к бесплатному API).

    Возвращает список [[ts_ms, o, h, l, c, v], ...] от старых к новым.
    """
    tf_seconds = {"minute": 60, "hour": 3600, "day": 86400}.get(tf, 3600) * aggregate
    target_span = days * 86400
    collected = []
    before_ts = None
    pages = 0

    while pages < max_pages:
        url = (
            f"{GECKOTERMINAL_BASE}/{pool_address}/ohlcv/{tf}"
            f"?aggregate={aggregate}&limit={page_limit}&currency={currency}&token={token}"
        )
        if before_ts:
            url += f"&before_timestamp={before_ts}"
        resp = requests.get(url, timeout=15, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; GrinchGram-Backtest/1.0)",
        })
        resp.raise_for_status()
        raw = resp.json()["data"]["attributes"]["ohlcv_list"]  # newest-first
        if not raw:
            break

        page_bars = [
            [int(ts) * 1000, float(o), float(h), float(l), float(c), float(v)]
            for ts, o, h, l, c, v in raw
        ]
        collected.extend(page_bars)
        pages += 1

        oldest_ts = raw[-1][0]
        span_so_far = collected[0][0] / 1000 - oldest_ts if collected else 0
        if span_so_far >= target_span:
            break

        before_ts = oldest_ts
        time.sleep(sleep_between_pages)  # не долбим бесплатный API без пауз

    # newest-first (постранично) -> убрать дубли -> отсортировать по времени
    dedup = {bar[0]: bar for bar in collected}
    bars = sorted(dedup.values(), key=lambda b: b[0])

    if not bars:
        return []

    cutoff_ms = bars[-1][0] - target_span * 1000
    bars = [b for b in bars if b[0] >= cutoff_ms]
    return bars


@dataclass
class Trade:
    entry_idx: int
    entry_ts: int
    entry_price: float
    exit_idx: int | None = None
    exit_ts: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    net_pct: float | None = None


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)  # [(ts, equity_multiplier)]
    total_return_pct: float = 0.0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_like: float = 0.0
    n_bars: int = 0

    def to_dict(self):
        return {
            "n_bars": self.n_bars,
            "n_trades": len(self.trades),
            "total_return_pct": round(self.total_return_pct, 3),
            "win_rate_pct": round(self.win_rate * 100, 2),
            "avg_win_pct": round(self.avg_win_pct, 3),
            "avg_loss_pct": round(self.avg_loss_pct, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 3),
            "sharpe_like": round(self.sharpe_like, 3),
            "trades": [
                {
                    "entry_ts": t.entry_ts, "entry_price": t.entry_price,
                    "exit_ts": t.exit_ts, "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "net_pct": None if t.net_pct is None else round(t.net_pct, 3),
                }
                for t in self.trades
            ],
        }


def _trail_pct_for_gain(gain_pct: float) -> float | None:
    """Реплицирует ступенчатый трейлинг из Config (TRAIL_STAGE2..4).
    Возвращает ширину трейла в % от пика, либо None если ни одна ступень
    ещё не активна (держим позицию без трейла, ждём TP или роста)."""
    if gain_pct >= Config.TRAIL_STAGE4_AT:
        return Config.TRAIL_STAGE4_PCT
    if gain_pct >= Config.TRAIL_STAGE3_AT:
        return Config.TRAIL_STAGE3_PCT
    if gain_pct >= Config.TRAIL_STAGE2_AT:
        return Config.TRAIL_STAGE2_PCT
    return None


def run_backtest(ohlcv: list, min_quality: str = "B", warmup_bars: int = 60) -> BacktestResult:
    """Walk-forward симуляция: на каждом баре i используем только ohlcv[:i+1]
    (никакого заглядывания в будущее). 1 позиция одновременно, вход по BUY +
    минимальному качеству входа, выход по ступенчатому трейлингу / TP,
    никогда не в убыток (ONLY_PROFIT_EXIT)."""
    result = BacktestResult(n_bars=len(ohlcv))
    if len(ohlcv) < warmup_bars + 5:
        return result

    fee_round_trip = Config.FEE_ROUND_TRIP / 100.0
    required_gross = Config.required_gross_pct() / 100.0
    quality_rank = {"A": 3, "B": 2, "C": 1, "": 0}
    min_rank = quality_rank.get(min_quality, 2)

    position: Trade | None = None
    peak_gain_pct = 0.0
    equity = 1.0
    peak_equity = 1.0
    max_dd = 0.0

    for i in range(warmup_bars, len(ohlcv)):
        window = ohlcv[: i + 1]
        bar_ts = window[-1][0]
        close_price = window[-1][4]

        if position is None:
            try:
                analysis = strategy.analyze(window)
            except Exception:
                continue
            signal = (analysis or {}).get("signal")
            quality = (analysis or {}).get("entry_quality", "")
            if signal == "BUY" and quality_rank.get(quality, 0) >= min_rank:
                position = Trade(entry_idx=i, entry_ts=bar_ts, entry_price=close_price)
                peak_gain_pct = 0.0
        else:
            gain_pct = (close_price - position.entry_price) / position.entry_price * 100.0
            peak_gain_pct = max(peak_gain_pct, gain_pct)
            exit_reason = None

            # Take-profit (gross), достаточный для целевой нетто-прибыли
            if gain_pct >= required_gross * 100.0:
                exit_reason = "take_profit"

            # Ступенчатый трейлинг от пика
            trail_pct = _trail_pct_for_gain(peak_gain_pct)
            if exit_reason is None and trail_pct is not None:
                drop_from_peak = peak_gain_pct - gain_pct
                if drop_from_peak >= trail_pct:
                    exit_reason = "trailing_stop"

            # ONLY_PROFIT_EXIT: никогда не закрываем в чистый минус —
            # если выходной сигнал есть, но нетто был бы отрицательным, ждём.
            if exit_reason is not None:
                net_pct = gain_pct - Config.FEE_ROUND_TRIP
                if net_pct <= 0 and Config.ONLY_PROFIT_EXIT:
                    exit_reason = None

            if exit_reason:
                position.exit_idx = i
                position.exit_ts = bar_ts
                position.exit_price = close_price
                position.exit_reason = exit_reason
                position.net_pct = gain_pct - Config.FEE_ROUND_TRIP
                equity *= (1.0 + position.net_pct / 100.0)
                result.trades.append(position)
                position = None
                peak_gain_pct = 0.0

        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
        max_dd = max(max_dd, dd)
        result.equity_curve.append((bar_ts, round(equity, 6)))

    # Открытая на конец периода позиция не засчитывается как сделка
    # (ещё не реализованный результат) — но фиксируем неопределённость честно.

    trades = result.trades
    if trades:
        wins = [t.net_pct for t in trades if t.net_pct and t.net_pct > 0]
        losses = [t.net_pct for t in trades if t.net_pct and t.net_pct <= 0]
        result.win_rate = len(wins) / len(trades)
        result.avg_win_pct = sum(wins) / len(wins) if wins else 0.0
        result.avg_loss_pct = sum(losses) / len(losses) if losses else 0.0
        result.total_return_pct = (equity - 1.0) * 100.0
        returns = [t.net_pct for t in trades if t.net_pct is not None]
        if len(returns) > 1:
            mean_r = sum(returns) / len(returns)
            var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
            std = var ** 0.5
            result.sharpe_like = (mean_r / std) if std > 0 else 0.0
    result.max_drawdown_pct = max_dd
    return result


def main():
    parser = argparse.ArgumentParser(description="Бэктест стратегии GRINCH/TON")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--tf", type=str, default="hour", choices=["minute", "hour", "day"])
    parser.add_argument("--aggregate", type=int, default=1)
    parser.add_argument("--min-quality", type=str, default="B", choices=["A", "B", "C"])
    parser.add_argument("--pool", type=str, default=Config.GRINCH_POOL_ADDRESS)
    args = parser.parse_args()

    print(f"[Backtest] Загружаю историю: {args.days}д, tf={args.tf}×{args.aggregate}, pool={args.pool}")
    ohlcv = fetch_historical_ohlcv(args.pool, days=args.days, tf=args.tf, aggregate=args.aggregate)
    print(f"[Backtest] Загружено {len(ohlcv)} свечей")
    if len(ohlcv) < 65:
        print("[Backtest] Недостаточно данных для прогона (нужно >= 65 свечей). "
              "Попробуйте меньший --aggregate или больше --days.")
        return

    result = run_backtest(ohlcv, min_quality=args.min_quality)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
