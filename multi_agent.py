"""
multi_agent.py — мультиагентная архитектура (консультативная, read-only).

Назначение
----------
BrainFusion (brain_fusion.py) — боевой "центральный мозг", который реально
участвует в решениях трейдера (RLock, синглтон, влияет на реальные сделки).
Трогать его рискованно. multi_agent.py — ОТДЕЛЬНЫЙ, параллельный оркестратор
для экспериментов: несколько специализированных "агентов" (каждый — просто
функция, читающая один источник данных) голосуют, а простой взвешенный
консенсус выдаёт итоговое READ-ONLY мнение. Ничего не пишет в БД/конфиг,
не исполняет сделки — это песочница для сравнения с BrainFusion, не замена.

Агенты
------
- technical_agent   — strategy.analyze() (индикаторы, качество входа)
- smart_money_agent — wallet_tracker.WalletTracker().get_signal() (умные деньги)
- backtest_agent    — недавняя историческая доходность стратегии (context, не сигнал по бару)

Каждый агент возвращает {"vote": -1..+1, "confidence": 0..1, "reason": str}.
Оркестратор считает взвешенное среднее votes по confidence и весам агента.

Использование
-------------
    python3 multi_agent.py --days 5
"""

from __future__ import annotations

import argparse


# Веса агентов в консенсусе — технический анализ первичен (это то, на чём
# реально основана стратегия), умные деньги и бэктест-контекст — модификаторы.
_WEIGHTS = {
    "technical": 0.6,
    "smart_money": 0.25,
    "backtest_context": 0.15,
}


def technical_agent(ohlcv: list) -> dict:
    import strategy
    try:
        analysis = strategy.analyze(ohlcv)
    except Exception as e:
        return {"vote": 0.0, "confidence": 0.0, "reason": f"ошибка анализа: {e}"}
    signal = (analysis or {}).get("signal", "HOLD")
    strength = (analysis or {}).get("strength", 0.0) / 100.0
    quality = (analysis or {}).get("entry_quality", "")
    vote = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}.get(signal, 0.0)
    return {
        "vote": vote,
        "confidence": strength,
        "reason": f"Технический сигнал {signal} (грейд {quality or 'н/д'}, сила {strength:.2f})",
    }


def smart_money_agent(_ohlcv: list = None) -> dict:
    try:
        from wallet_tracker import WalletTracker
        sig = WalletTracker().get_signal()
    except Exception as e:
        return {"vote": 0.0, "confidence": 0.0, "reason": f"ошибка: {e}"}
    score = sig.get("score", 0.0)
    basis = sig.get("basis", "idle")
    confidence = 0.0 if basis in ("idle", "no_data") else min(abs(score), 1.0)
    return {
        "vote": score,
        "confidence": confidence,
        "reason": f"Умные деньги: {sig.get('label', 'нейтрально')} (basis={basis})",
    }


def backtest_context_agent(ohlcv: list) -> dict:
    """Не сигнал по текущему бару, а контекст: 'работала ли стратегия
    последние дни на этом же рынке'. Слабый агент — низкий вес и очень
    сдержанная уверенность, чтобы не подменять собой бэктест как инструмент."""
    try:
        from backtest import run_backtest
        result = run_backtest(ohlcv, min_quality="B")
    except Exception as e:
        return {"vote": 0.0, "confidence": 0.0, "reason": f"ошибка бэктеста: {e}"}
    if not result.trades:
        return {"vote": 0.0, "confidence": 0.0, "reason": "недостаточно недавних сделок для контекста"}
    vote = 1.0 if result.win_rate >= 0.5 and result.total_return_pct > 0 else -0.3
    confidence = min(len(result.trades) / 10.0, 0.5)
    return {
        "vote": vote,
        "confidence": confidence,
        "reason": f"Недавний бэктест: {len(result.trades)} сделок, "
                   f"win rate {result.win_rate*100:.0f}%, доходность {result.total_return_pct:+.1f}%",
    }


def consensus(ohlcv: list) -> dict:
    votes = {
        "technical": technical_agent(ohlcv),
        "smart_money": smart_money_agent(ohlcv),
        "backtest_context": backtest_context_agent(ohlcv),
    }

    weighted_sum = 0.0
    weight_total = 0.0
    for name, v in votes.items():
        w = _WEIGHTS.get(name, 0.0) * v["confidence"]
        weighted_sum += v["vote"] * w
        weight_total += w

    final_score = weighted_sum / weight_total if weight_total > 0 else 0.0
    if final_score >= 0.25:
        final_signal = "BUY"
    elif final_score <= -0.25:
        final_signal = "SELL"
    else:
        final_signal = "HOLD"

    return {
        "final_signal": final_signal,
        "final_score": round(final_score, 3),
        "agents": votes,
    }


def main():
    parser = argparse.ArgumentParser(description="Мультиагентный консенсус (read-only, экспериментальный)")
    parser.add_argument("--days", type=int, default=5)
    args = parser.parse_args()

    from backtest import fetch_historical_ohlcv
    from config import Config

    ohlcv = fetch_historical_ohlcv(Config.GRINCH_POOL_ADDRESS, days=args.days, tf="hour", aggregate=1)
    if len(ohlcv) < 65:
        print("Недостаточно данных.")
        return

    result = consensus(ohlcv)
    print(f"Итоговый сигнал: {result['final_signal']} (score={result['final_score']})")
    for name, v in result["agents"].items():
        print(f"  [{name}] vote={v['vote']:.2f} conf={v['confidence']:.2f} — {v['reason']}")


if __name__ == "__main__":
    main()
