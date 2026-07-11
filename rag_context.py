"""
rag_context.py — RAG / векторный поиск похожих исторических рыночных ситуаций.

Назначение
----------
Даёт LLM-агенту (llm_agent.py) и explainability-слою контекст вида
«в прошлый раз при похожей картине индикаторов цена сделала +X% за N часов».
Осознанно НЕ используется отдельная векторная БД (Chroma/FAISS) — это
добавило бы лишний процесс/память на и так ограниченном по RAM VPS
(см. память проекта: RAM-пол уже ~200-250MB на numpy/pandas/sklearn).
Вместо этого — простое сравнение векторов признаков через numpy
(косинусное сходство), которое достаточно для десятков-сотен исторических
баров и не требует новых тяжёлых зависимостей.

Как это работает
-----------------
1. build_pattern_index(ohlcv) — по историческим свечам считает вектор
   признаков на каждом баре (rsi, macd_hist, bb_pct, adx, vol_ratio,
   ema_alignment-прокси) через strategy.compute_indicators, и "исход" —
   доходность цены через HORIZON_BARS баров вперёд (известна только для
   прошлых баров, поэтому последние HORIZON_BARS исключаются из индекса).
2. find_similar(current_features, index, top_k) — косинусное сходство
   между текущим вектором и всеми проиндексированными, топ-K по схожести.
3. get_historical_context(ohlcv) — удобная обёртка: строит индекс по тем
   же свечам (минус последние HORIZON_BARS) и ищет похожие на ТЕКУЩИЙ
   (последний) бар ситуации — READ-ONLY, ничего не пишет и не торгует.

Использование
-------------
    python3 rag_context.py --days 20
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

HORIZON_BARS = 6   # на сколько баров вперёд считаем исход для исторических паттернов
FEATURE_COLS = ["rsi", "macd_hist", "bb_pct", "adx", "vol_ratio"]


def _feature_vector(df: pd.DataFrame, idx: int) -> np.ndarray:
    row = df.iloc[idx]
    rsi       = float(row["rsi"]) / 100.0
    macd_hist = np.tanh(float(row["macd_hist"]) * 50.0)   # сжимаем разброс в [-1,1]
    bb_pct    = float(row["bb_pct"]) / 100.0
    adx       = min(float(row["adx"]) / 50.0, 1.5)
    vol_ratio = min(float(row["vol_ratio"]) / 3.0, 1.5)
    return np.array([rsi, macd_hist, bb_pct, adx, vol_ratio], dtype=float)


def build_pattern_index(ohlcv: list) -> dict:
    """Возвращает {"vectors": np.ndarray [N,5], "outcomes": [...], "meta": [...]}
    для баров, у которых известен исход через HORIZON_BARS свечей вперёд."""
    import strategy
    df = strategy.compute_indicators(ohlcv)
    n = len(df)
    if n < 40:
        return {"vectors": np.zeros((0, 5)), "outcomes": [], "meta": []}

    vectors, outcomes, meta = [], [], []
    for i in range(30, n - HORIZON_BARS):
        try:
            vec = _feature_vector(df, i)
        except Exception:
            continue
        price_now = float(df.iloc[i]["close"])
        price_future = float(df.iloc[i + HORIZON_BARS]["close"])
        outcome_pct = (price_future - price_now) / price_now * 100.0
        vectors.append(vec)
        outcomes.append(outcome_pct)
        meta.append({"ts": int(df.iloc[i]["timestamp"].value // 10**6), "price": price_now})

    return {
        "vectors": np.array(vectors) if vectors else np.zeros((0, 5)),
        "outcomes": outcomes,
        "meta": meta,
    }


def find_similar(current_vec: np.ndarray, index: dict, top_k: int = 5) -> list:
    vectors = index["vectors"]
    if len(vectors) == 0:
        return []
    norms = np.linalg.norm(vectors, axis=1) * (np.linalg.norm(current_vec) + 1e-10)
    sims = (vectors @ current_vec) / (norms + 1e-10)
    top_idx = np.argsort(-sims)[:top_k]
    return [
        {
            "similarity": round(float(sims[i]), 3),
            "outcome_pct": round(index["outcomes"][i], 3),
            "ts": index["meta"][i]["ts"],
            "price_then": index["meta"][i]["price"],
        }
        for i in top_idx
    ]


def get_historical_context(ohlcv: list, top_k: int = 5) -> dict:
    """Индексирует всё, кроме последних HORIZON_BARS баров (у которых ещё
    нет известного исхода), и ищет похожие на ТЕКУЩИЙ (последний) бар."""
    import strategy
    if len(ohlcv) < 40 + HORIZON_BARS:
        return {"matches": [], "note": "недостаточно истории для RAG-контекста"}

    index = build_pattern_index(ohlcv[:-1])   # исключаем текущий бар из индекса-доноров
    df_now = strategy.compute_indicators(ohlcv)
    current_vec = _feature_vector(df_now, len(df_now) - 1)
    matches = find_similar(current_vec, index, top_k=top_k)

    if matches:
        avg_outcome = sum(m["outcome_pct"] for m in matches) / len(matches)
        bullish = sum(1 for m in matches if m["outcome_pct"] > 0)
        note = (
            f"Среди {len(matches)} похожих прошлых ситуаций {bullish} привели к росту цены "
            f"в следующие {HORIZON_BARS} баров, средний исход {avg_outcome:+.2f}%."
        )
    else:
        note = "Похожих исторических ситуаций не найдено."

    return {"matches": matches, "note": note}


def main():
    parser = argparse.ArgumentParser(description="RAG-контекст похожих исторических ситуаций")
    parser.add_argument("--days", type=int, default=20)
    args = parser.parse_args()

    from backtest import fetch_historical_ohlcv
    from config import Config

    ohlcv = fetch_historical_ohlcv(Config.GRINCH_POOL_ADDRESS, days=args.days, tf="hour", aggregate=1)
    ctx = get_historical_context(ohlcv)
    print(ctx["note"])
    for m in ctx["matches"]:
        print(f"  similarity={m['similarity']} outcome={m['outcome_pct']:+.2f}% ts={m['ts']}")


if __name__ == "__main__":
    main()
