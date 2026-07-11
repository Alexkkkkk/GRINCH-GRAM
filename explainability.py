"""
explainability.py — Движок объяснимости: переводит выход strategy.analyze()
в понятную человеку причину решения.

Назначение
----------
strategy.analyze() уже считает десятки факторов (entry_reasons, ai_components,
regime, rsi_zone, opportunity_score и т.д.), но это сырые числа/списки —
удобно для графиков, неудобно для человека, который хочет за 2 секунды понять
"почему бот купил/не купил". Этот модуль строит короткое связное объяснение
на русском языке из уже посчитанных (не новых!) данных — READ-ONLY, не влияет
на торговые решения.

Использование
-------------
    from explainability import explain
    analysis = strategy.analyze(ohlcv)
    text = explain(analysis)
    print(text)

Также доступен CLI для быстрой проверки на живых данных:
    python3 explainability.py --days 3
"""

from __future__ import annotations

import argparse


_REGIME_RU = {
    "UPTREND":   "восходящий тренд",
    "DOWNTREND": "нисходящий тренд",
    "BREAKOUT":  "пробой диапазона",
    "RANGING":   "боковик",
    "VOLATILE":  "повышенная волатильность",
    "POST_PUMP": "распределение после пампа",
}

_RSI_ZONE_RU = {
    "OVERSOLD":    "перепродан",
    "OVERBOUGHT":  "перекуплен",
    "LOW":         "ниже нейтральной зоны",
    "HIGH":        "выше нейтральной зоны",
    "NEUTRAL":     "нейтральная зона",
}

_SIGNAL_RU = {"BUY": "ПОКУПКА", "SELL": "ПРОДАЖА", "HOLD": "ОЖИДАНИЕ"}


def explain(analysis: dict) -> str:
    """Строит объяснение на русском из результата strategy.analyze().
    Не выбрасывает исключений на неполных данных — деградирует до
    минимального объяснения, если каких-то полей нет."""
    if not analysis:
        return "Недостаточно данных для анализа (нет свечей)."

    signal   = analysis.get("signal", "HOLD")
    quality  = analysis.get("entry_quality", "")
    regime   = analysis.get("regime", "")
    rsi_zone = analysis.get("rsi_zone", "")
    opp      = analysis.get("opportunity_score")
    reasons  = analysis.get("entry_reasons") or []
    price    = analysis.get("price")

    parts = []

    header = f"Сигнал: {_SIGNAL_RU.get(signal, signal)}"
    if quality:
        header += f" (грейд входа {quality})"
    if price is not None:
        header += f" при цене {price}"
    parts.append(header + ".")

    if regime:
        parts.append(f"Рынок сейчас в режиме «{_REGIME_RU.get(regime, regime)}».")

    if rsi_zone:
        parts.append(f"RSI: {_RSI_ZONE_RU.get(rsi_zone, rsi_zone)}.")

    if opp is not None:
        if opp >= 70:
            opp_txt = "высокая оценка возможности входа"
        elif opp >= 40:
            opp_txt = "средняя оценка возможности входа"
        else:
            opp_txt = "низкая оценка возможности входа"
        parts.append(f"Совокупный AI-скор возможности: {opp}/100 ({opp_txt}).")

    if reasons:
        top_reasons = reasons[:4]
        parts.append("Совпавшие факторы: " + "; ".join(top_reasons) + ".")
    elif signal == "BUY":
        parts.append("Технический сигнал есть, но подтверждающих факторов входа мало.")

    components = analysis.get("ai_components") or []
    if components:
        strongest = max(components, key=lambda c: c.get("pct", 0))
        parts.append(
            f"Сильнее всего вклад дал компонент «{strongest.get('name')}» "
            f"({strongest.get('pct')}% от максимума)."
        )

    if signal == "HOLD":
        parts.append("Бот ждёт более чёткого сигнала — открытие позиции пока не оправдано.")

    return " ".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Explainability для последнего анализа GRINCH/TON")
    parser.add_argument("--days", type=int, default=3, help="Сколько дней истории подтянуть для анализа")
    args = parser.parse_args()

    from backtest import fetch_historical_ohlcv
    from config import Config
    import strategy

    ohlcv = fetch_historical_ohlcv(Config.GRINCH_POOL_ADDRESS, days=args.days, tf="hour", aggregate=1)
    if len(ohlcv) < 40:
        print("Недостаточно данных.")
        return
    analysis = strategy.analyze(ohlcv)
    print(explain(analysis))


if __name__ == "__main__":
    main()
