"""
llm_agent.py — LLM-агент с function-calling поверх существующих данных бота.

Назначение
----------
Расширяет текущего Groq-советника (ai_advisor.py) до полноценного АГЕНТА:
вместо одного большого промпта со снимком состояния, модель сама решает,
какие инструменты (функции) вызвать, чтобы собрать нужный контекст, и в
конце объясняет решение человеческим языком.

Почему отдельный модуль, а не правка ai_advisor.py
---------------------------------------------------
ai_advisor.py уже применяет рекомендации к боевому конфигу (auto_apply,
_apply_recommendations, _apply_strategy_toggles) — трогать его логику
рискованно. llm_agent.py — READ-ONLY консультативный слой: вызывает
существующие функции модулей (price_feed, backtest, paper_trading,
wallet_tracker) только на чтение, ничего не пишет в конфиг/БД/боевые
таблицы и не исполняет сделки. Использует тот же Groq-клиент
(GROQ_API_KEY из ai_advisor._effective_key()), чтобы не дублировать
логику хранения ключа.

Инструменты (tools), которые может вызвать модель
--------------------------------------------------
- get_current_price()            — текущая цена GRINCH (price_feed)
- get_backtest_summary(days)     — быстрый бэктест за N дней (backtest.py)
- get_paper_trading_status()     — статус виртуального портфеля (paper_trading.py)
- get_smart_money_signal()       — сигнал по кошелькам умных денег (wallet_tracker), best-effort

Использование
-------------
    python3 llm_agent.py --ask "Стоит ли сейчас входить в позицию и почему?"

Возвращает текстовый ответ модели плюс лог вызванных инструментов (для
прозрачности — какие данные реально использовались для ответа).
"""

from __future__ import annotations

import argparse
import json

import ai_advisor  # переиспользуем _get_client()/_effective_key(), ничего не мутируем

GROQ_MODEL = "llama-3.3-70b-versatile"

MAX_TOOL_ROUNDS = 4


# ──────────────────────────────────────────────────────────────────────────
# Инструменты — все READ-ONLY, без побочных эффектов на боевую торговлю
# ──────────────────────────────────────────────────────────────────────────

def _tool_get_current_price(_args: dict) -> dict:
    try:
        from price_feed import price_feed
        price = price_feed.get("GRINCH")
        return {"symbol": "GRINCH/TON", "price_usd": price}
    except Exception as e:
        return {"error": str(e)}


def _tool_get_backtest_summary(args: dict) -> dict:
    try:
        from backtest import fetch_historical_ohlcv, run_backtest
        from config import Config
        days = int(args.get("days", 14))
        days = max(3, min(days, 60))  # разумные границы, чтобы не долбить API
        ohlcv = fetch_historical_ohlcv(Config.GRINCH_POOL_ADDRESS, days=days, tf="hour", aggregate=1)
        if len(ohlcv) < 65:
            return {"error": "недостаточно исторических данных для бэктеста"}
        result = run_backtest(ohlcv, min_quality=args.get("min_quality", "B"))
        d = result.to_dict()
        d.pop("trades", None)  # агенту хватает сводки, не нужен полный список сделок
        return d
    except Exception as e:
        return {"error": str(e)}


def _tool_get_paper_trading_status(_args: dict) -> dict:
    try:
        from paper_trading import _load_state, status_summary
        return status_summary(_load_state())
    except Exception as e:
        return {"error": str(e)}


def _tool_get_similar_past_patterns(args: dict) -> dict:
    try:
        from backtest import fetch_historical_ohlcv
        from rag_context import get_historical_context
        from config import Config
        days = int(args.get("days", 20))
        days = max(10, min(days, 60))
        ohlcv = fetch_historical_ohlcv(Config.GRINCH_POOL_ADDRESS, days=days, tf="hour", aggregate=1)
        return get_historical_context(ohlcv)
    except Exception as e:
        return {"error": str(e)}


def _tool_get_smart_money_signal(_args: dict) -> dict:
    """WalletTracker — обычно синглтон, создаваемый в app.py при старте бота
    (накапливает события из живого потока). Здесь бот не запущен, поэтому
    создаём временный экземпляр — если событий в БД/памяти нет, вернёт
    нейтральный сигнал с basis='no_data', что и должно быть отражено агенту
    честно (не выдумывать сигнал из отсутствующих данных)."""
    try:
        from wallet_tracker import WalletTracker
        tracker = WalletTracker()
        return tracker.get_signal()
    except Exception as e:
        return {"error": str(e)}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_price",
            "description": "Текущая цена GRINCH в USD (спот, DexScreener через price_feed).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_backtest_summary",
            "description": "Быстрый бэктест технической стратегии за последние N дней "
                            "(walk-forward, без заглядывания в будущее). Возвращает "
                            "доходность, win rate, просадку, число сделок.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Глубина истории в днях (3-60)"},
                    "min_quality": {"type": "string", "enum": ["A", "B", "C"],
                                     "description": "Минимальный грейд качества входа"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_paper_trading_status",
            "description": "Текущее состояние виртуального (пейпер-трейдинг) портфеля: "
                            "доходность, число сделок, открыта ли позиция сейчас.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_smart_money_signal",
            "description": "Сигнал по активности крупных/умных кошельков в пуле (bias BUY/HOLD).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_similar_past_patterns",
            "description": "RAG: находит похожие на текущую исторические рыночные ситуации "
                            "(по индикаторам) и что происходило с ценой после них.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Глубина истории для поиска (10-60 дней)"},
                },
            },
        },
    },
]

_TOOL_IMPL = {
    "get_current_price": _tool_get_current_price,
    "get_backtest_summary": _tool_get_backtest_summary,
    "get_paper_trading_status": _tool_get_paper_trading_status,
    "get_smart_money_signal": _tool_get_smart_money_signal,
    "get_similar_past_patterns": _tool_get_similar_past_patterns,
}

SYSTEM_PROMPT = (
    "Ты — консультативный AI-агент торгового бота GRINCH/TON. У тебя есть инструменты "
    "для чтения РЕАЛЬНЫХ данных (цена, бэктест, пейпер-трейдинг, умные деньги). "
    "Всегда вызывай нужные инструменты вместо догадок. Ты НЕ исполняешь сделки и "
    "НЕ меняешь настройки бота — только объясняешь ситуацию и даёшь рекомендацию "
    "человеку, который сам примет решение. Отвечай на русском, кратко и по делу, "
    "указывая на какие цифры опираешься."
)


def ask(question: str, verbose: bool = True) -> dict:
    """Задать вопрос агенту. Возвращает {"answer": str, "tool_calls": [...]}"""
    client = ai_advisor._get_client()
    if client is None:
        return {"answer": None, "tool_calls": [], "error": "GROQ_API_KEY не задан"}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tool_log = []

    for _round in range(MAX_TOOL_ROUNDS):
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.3,
        )
        choice = resp.choices[0]
        msg = choice.message

        if not getattr(msg, "tool_calls", None):
            return {"answer": msg.content, "tool_calls": tool_log}

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments or "{}")
            except Exception:
                fn_args = {}
            impl = _TOOL_IMPL.get(fn_name)
            result = impl(fn_args) if impl else {"error": f"неизвестный инструмент {fn_name}"}
            tool_log.append({"tool": fn_name, "args": fn_args, "result": result})
            if verbose:
                print(f"[Agent] → {fn_name}({fn_args}) = {json.dumps(result, ensure_ascii=False)[:300]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    return {"answer": "Достигнут лимит вызовов инструментов без финального ответа.",
            "tool_calls": tool_log}


def main():
    parser = argparse.ArgumentParser(description="LLM-агент с function-calling (read-only, консультативный)")
    parser.add_argument("--ask", type=str, required=True, help="Вопрос агенту")
    args = parser.parse_args()

    result = ask(args.ask)
    if result.get("error"):
        print(f"[Agent] Ошибка: {result['error']}")
        return
    print("\n=== Ответ агента ===")
    print(result["answer"])


if __name__ == "__main__":
    main()
