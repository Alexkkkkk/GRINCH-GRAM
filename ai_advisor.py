"""
ai_advisor.py — Мета-ИИ советник на Groq (бесплатно).
Анализирует состояние торговли и оптимизирует параметры.
Модель: llama-3.3-70b-versatile через API, совместимый с OpenAI.
"""
import os, json, logging, threading, time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.3-70b-versatile"

# Параметры которые советник МОЖЕТ менять  →  (мин, макс)
TUNABLE = {
    "take_profit_pct":         (5.0,  200.0),
    "dca_target_profit_pct":   (5.0,  100.0),
    "dca_drop_trigger_pct":    (5.0,   60.0),
    "smart_buy_pullback_pct":  (0.2,    5.0),
    "profit_protect_drop_pct": (0.3,   20.0),
    "buy_threshold":           (0.42,   0.72),
    "sell_threshold":          (0.55,   0.82),
    "min_ai_confidence":       (45.0,   90.0),
}

# ─── внутреннее состояние ───────────────────────────────────────────
_lock        = threading.Lock()
_history:   list[dict]    = []      # последние N диалогов
_last_advice: Optional[dict] = None  # последний разбор советника
_auto_apply: bool = False            # авто-применение рекомендаций
_running:    bool = False            # идёт ли запрос прямо сейчас
_MAX_HISTORY = 10

SYSTEM_PROMPT = """Ты — эксперт по алгоритмической торговле криптовалютой. Специализируешься на DEX-рынках (DeDust / TON).
Твоя задача: анализировать текущее состояние торгового бота GRINCH-GRAM и рекомендовать точные изменения параметров для МАКСИМИЗАЦИИ прибыли.

ПРАВИЛА (СТРОГО):
1. Никогда не рекомендуй действия, которые могут привести к убыточной сделке.
2. Режим ONLY_PROFIT_EXIT=True — НЕЛЬЗЯ отключать (не упоминай).
3. Анализируй win_rate, avg_profit, sharpe, режим рынка (RANGING/UPTREND/DOWNTREND).
4. Рекомендуй конкретные числа в пределах допустимых диапазонов.
5. Объясняй ПОЧЕМУ каждое изменение улучшит прибыль на русском языке.

ДОПУСТИМЫЕ ПАРАМЕТРЫ для изменения:
- take_profit_pct: [5..200] % — цель прибыли на сделку
- dca_target_profit_pct: [5..100] % — цель прибыли DCA-портфеля
- dca_drop_trigger_pct: [5..60] % — докупка при падении на N%
- smart_buy_pullback_pct: [0.2..5] % — откат перед покупкой
- profit_protect_drop_pct: [0.3..20] % — откат от пика для защиты прибыли
- buy_threshold: [0.42..0.72] — порог BUY (вероятность роста)
- sell_threshold: [0.55..0.82] — порог SELL (вероятность падения)
- min_ai_confidence: [45..90] % — мин. уверенность AI для входа

ФОРМАТ ОТВЕТА — строго JSON (без markdown, без лишнего текста):
{
  "analysis": "краткий анализ ситуации на русском (2-3 предложения)",
  "recommendations": [
    {
      "param": "take_profit_pct",
      "current": 15.0,
      "suggested": 18.0,
      "reason": "почему это улучшит прибыль"
    }
  ],
  "market_verdict": "НАКАПЛИВАТЬ | ОСТОРОЖНО | АКТИВНО_ТОРГОВАТЬ | ПАУЗА",
  "confidence": 0.75
}
Если изменений не нужно — верни пустой список recommendations.
"""


def _get_client():
    """Клиент OpenAI, направленный на Groq."""
    if not GROQ_API_KEY:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
    except Exception as e:
        logger.error(f"[Advisor] Ошибка инициализации клиента: {e}")
        return None


def _build_snapshot() -> dict:
    """Собирает текущее состояние бота для отправки советнику."""
    snap = {}
    try:
        from config import Config
        snap["config"] = {
            "take_profit_pct":         Config.TAKE_PROFIT_PCT,
            "dca_mode":                Config.DCA_MODE,
            "dca_target_profit_pct":   Config.DCA_TARGET_PROFIT_PCT,
            "dca_drop_trigger_pct":    Config.DCA_DROP_TRIGGER_PCT,
            "smart_buy_pullback_pct":  Config.SMART_BUY_PULLBACK_PCT,
            "profit_protect_drop_pct": Config.PROFIT_PROTECT_DROP_PCT,
            "min_ai_confidence":       Config.MIN_AI_CONFIDENCE,
            "only_profit_exit":        Config.ONLY_PROFIT_EXIT,
        }
    except Exception:
        pass
    try:
        import ai_engine as ae
        snap["ai_engine"] = {
            "buy_threshold":   ae.BUY_THRESHOLD,
            "sell_threshold":  ae.SELL_THRESHOLD,
            "profit_bias_pct": ae.PROFIT_BIAS_PCT,
            "vr_trend_thresh": ae.VR_TREND_THRESH,
            "ev_min_trades":   ae.EV_MIN_TRADES,
            "model":           ae.GROQ_MODEL if hasattr(ae, "GROQ_MODEL") else "RF+ET+GB+HGB+XGB+MLP",
        }
    except Exception:
        snap["ai_engine"] = {}
    try:
        from experience_manager import experience_manager
        report = experience_manager.get_report()
        snap["performance"] = {
            "total_trades": report.get("total_trades", 0),
            "win_rate":     report.get("win_rate", 0),
            "avg_profit":   report.get("avg_profit_pct", 0),
            "sharpe":       report.get("sharpe", 0),
            "best_regime":  report.get("best_regime", "?"),
        }
    except Exception:
        snap["performance"] = {}
    try:
        from trader import trader
        analysis = trader.exchange.analyze() if hasattr(trader, "exchange") else {}
        snap["market"] = {
            "price":    analysis.get("price", 0),
            "regime":   analysis.get("regime", "?"),
            "atr_pct":  analysis.get("atr_pct", 0),
            "rsi":      analysis.get("rsi", 50),
            "adx":      analysis.get("adx", 0),
            "signal":   analysis.get("signal", "HOLD"),
            "prob_up":  analysis.get("prob_up", 0),
            "prob_down":analysis.get("prob_down", 0),
            "pump":     analysis.get("pump", "NONE"),
        }
        stats = trader.stats
        snap["trading_state"] = {
            "open_positions": stats.get("total_trades", 0),
            "total_pnl_ton":  stats.get("total_profit", 0),
            "portfolio_pct":  stats.get("portfolio_pct", 0),
        }
    except Exception as ex:
        snap["market"] = {}
        snap["trading_state"] = {}
    snap["timestamp"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return snap


def _parse_response(text: str) -> dict:
    """Парсит JSON из ответа LLM (устойчиво к мусору вокруг)."""
    text = text.strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1 or end == 0:
        return {"analysis": text, "recommendations": [], "market_verdict": "ОСТОРОЖНО", "confidence": 0.5}
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return {"analysis": text, "recommendations": [], "market_verdict": "ОСТОРОЖНО", "confidence": 0.5}


def _apply_recommendations(recs: list[dict]) -> list[str]:
    """Применяет рекомендованные изменения в рамках допустимых диапазонов."""
    applied = []
    if not recs:
        return applied
    try:
        from config import Config
        import ai_engine as ae
        from settings_store import update_section
    except Exception as ex:
        logger.error(f"[Advisor] Импорт для применения: {ex}")
        return applied

    config_updates = {}
    for rec in recs:
        param   = rec.get("param", "")
        val_raw = rec.get("suggested")
        if param not in TUNABLE or val_raw is None:
            continue
        try:
            val = float(val_raw)
        except (TypeError, ValueError):
            continue
        lo, hi = TUNABLE[param]
        val = max(lo, min(hi, val))

        if param == "buy_threshold":
            ae.BUY_THRESHOLD = val
            applied.append(f"buy_threshold → {val:.3f}")
        elif param == "sell_threshold":
            ae.SELL_THRESHOLD = val
            applied.append(f"sell_threshold → {val:.3f}")
        elif param == "take_profit_pct":
            Config.TAKE_PROFIT_PCT = val
            config_updates["take_profit_pct"] = str(val)
            applied.append(f"take_profit_pct → {val:.1f}%")
        elif param == "dca_target_profit_pct":
            Config.DCA_TARGET_PROFIT_PCT = val
            config_updates["dca_target_profit_pct"] = str(val)
            applied.append(f"dca_target_profit_pct → {val:.1f}%")
        elif param == "dca_drop_trigger_pct":
            Config.DCA_DROP_TRIGGER_PCT = val
            config_updates["dca_drop_trigger_pct"] = str(val)
            applied.append(f"dca_drop_trigger_pct → {val:.1f}%")
        elif param == "smart_buy_pullback_pct":
            Config.SMART_BUY_PULLBACK_PCT = val
            config_updates["smart_buy_pullback_pct"] = str(val)
            applied.append(f"smart_buy_pullback_pct → {val:.2f}%")
        elif param == "profit_protect_drop_pct":
            Config.PROFIT_PROTECT_DROP_PCT = val
            config_updates["profit_protect_drop_pct"] = str(val)
            applied.append(f"profit_protect_drop_pct → {val:.1f}%")
        elif param == "min_ai_confidence":
            Config.MIN_AI_CONFIDENCE = val
            config_updates["min_ai_confidence"] = str(val)
            applied.append(f"min_ai_confidence → {val:.0f}%")

    # Гарантируем ONLY_PROFIT_EXIT
    try:
        Config.ONLY_PROFIT_EXIT = True
    except Exception:
        pass

    if config_updates:
        try:
            update_section("config", config_updates)
        except Exception as ex:
            logger.warning(f"[Advisor] settings_store: {ex}")

    return applied


def run_advisor(auto_apply: bool = False, user_message: str = "") -> dict:
    """
    Запускает советника: собирает снимок → отправляет Groq → разбирает → применяет.
    Возвращает результат (словарь).
    """
    global _running, _last_advice, _history, _auto_apply

    if not GROQ_API_KEY:
        return {"ok": False, "error": "GROQ_API_KEY не задан. Получи бесплатный ключ на console.groq.com"}

    client = _get_client()
    if not client:
        return {"ok": False, "error": "Не удалось инициализировать Groq клиент"}

    with _lock:
        if _running:
            return {"ok": False, "error": "Советник уже работает, подожди…"}
        _running = True

    try:
        snap = _build_snapshot()
        snap_str = json.dumps(snap, ensure_ascii=False, indent=2)

        user_content = (
            f"Текущее состояние торгового бота:\n```json\n{snap_str}\n```"
        )
        if user_message.strip():
            user_content += f"\n\nДополнительный вопрос: {user_message.strip()}"

        messages = [
            {"role": "system",  "content": SYSTEM_PROMPT},
            {"role": "user",    "content": user_content},
        ]

        logger.info("[Advisor] Отправляю запрос Groq LLaMA…")
        t0 = time.time()
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.3,
            max_completion_tokens=1024,
        )
        elapsed = round(time.time() - t0, 1)
        raw = resp.choices[0].message.content or ""
        logger.info(f"[Advisor] Ответ получен за {elapsed}s: {raw[:120]}…")

        parsed = _parse_response(raw)
        applied = []
        if auto_apply or _auto_apply:
            applied = _apply_recommendations(parsed.get("recommendations", []))
            if applied:
                logger.info(f"[Advisor] Применено: {', '.join(applied)}")

        result = {
            "ok":           True,
            "timestamp":    datetime.utcnow().strftime("%H:%M:%S"),
            "elapsed_s":    elapsed,
            "analysis":     parsed.get("analysis", ""),
            "recommendations": parsed.get("recommendations", []),
            "market_verdict":  parsed.get("market_verdict", "ОСТОРОЖНО"),
            "confidence":      parsed.get("confidence", 0.5),
            "applied":         applied,
            "snapshot":        snap,
        }
        with _lock:
            _last_advice = result
            _history.append({
                "ts":      result["timestamp"],
                "verdict": result["market_verdict"],
                "applied": applied,
                "analysis": result["analysis"],
            })
            if len(_history) > _MAX_HISTORY:
                _history = _history[-_MAX_HISTORY:]
        return result

    except Exception as ex:
        logger.error(f"[Advisor] Ошибка: {ex}")
        return {"ok": False, "error": str(ex)}
    finally:
        with _lock:
            _running = False


def get_status() -> dict:
    """Статус советника для дашборда."""
    with _lock:
        return {
            "enabled":      bool(GROQ_API_KEY),
            "running":      _running,
            "auto_apply":   _auto_apply,
            "last_advice":  _last_advice,
            "history":      list(_history),
            "model":        GROQ_MODEL,
        }


def toggle_auto_apply() -> bool:
    global _auto_apply
    with _lock:
        _auto_apply = not _auto_apply
        return _auto_apply


def reload_key():
    """Перечитывает ключ из окружения (после его добавления)."""
    global GROQ_API_KEY
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    return bool(GROQ_API_KEY)
