"""
ai_advisor.py — Мета-ИИ советник с полной автономией.
Groq LLaMA 3.3-70B (бесплатно) анализирует торговлю и
автоматически адаптирует ВСЕ параметры бота после каждой сделки.
"""
import os, json, logging, threading, time
from datetime import datetime
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# ── Groq (бесплатно, OpenAI-совместимый API) ──────────────────────────────
GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")

# Загружаем ключ из settings_store (если сохранён через дашборд)
try:
    from settings_store import get_section as _ss_get
    _adv_sec = _ss_get("advisor")
    if _adv_sec.get("groq_api_key"):
        GROQ_API_KEY = _adv_sec["groq_api_key"]
except Exception:
    pass
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.3-70b-versatile"

# ── Параметры автономии ────────────────────────────────────────────────────
AUTO_INTERVAL_MIN    = 5    # авто-запуск каждые N минут (было 15)
AUTO_TRADES_TRIGGER  = 1    # авто-запуск после каждой закрытой сделки (было 3)

# ── Параметры которые советник МОЖЕТ менять → (мин, макс) ─────────────────
TUNABLE = {
    # Торговые параметры (Config)
    "take_profit_pct":         (5.0,   200.0),
    "dca_target_profit_pct":   (5.0,   100.0),
    "dca_drop_trigger_pct":    (5.0,    60.0),
    "smart_buy_pullback_pct":  (0.2,     5.0),
    "profit_protect_drop_pct": (0.3,    20.0),
    "min_ai_confidence":       (40.0,   90.0),
    # AI Engine параметры (модуль ai_engine)
    "buy_threshold":           (0.40,   0.75),
    "sell_threshold":          (0.52,   0.85),
    "profit_bias_pct":         (0.010,  0.060),
    "vr_trend_thresh":         (1.05,   1.40),
    "ev_min_trades":           (5.0,    25.0),
    "retrain_every":           (1.0,     6.0),
    # Трейлинг-стопы (Config) — расширенная автономия
    "trailing_stop_pct":       (2.0,    25.0),
    "trail_stage2_pct":        (2.0,    20.0),
    "trail_stage3_pct":        (1.5,    15.0),
    "trail_stage4_pct":        (1.0,    10.0),
    "smart_tp_min_conf":       (50.0,   90.0),
    "short_trail_pct":         (3.0,    25.0),
    # Money management — размер позиции
    "ai_size_mult":            (0.3,     1.5),
    # Размер ставки — ИИ управляет напрямую
    "dca_stake_ton":           (5.0,  1000.0),   # TON за каждый DCA-вход
    "trade_amount":            (5.0,  1000.0),   # TON базовой ставки AI-режима
    "min_profit_ton_abs":      (1.0,    50.0),   # минимальная АБСОЛЮТНАЯ прибыль в TON
}

# ── Стратегии, которые советник МОЖЕТ включать/выключать целиком ─────────
STRATEGY_TOGGLES = {
    "dca_mode":               "DCA-докупка при падении цены",
    "short_trading_enabled":  "Шорт-позиции (заработок на падении)",
    "smart_buy_enabled":      "Smart BUY (ожидание отката перед входом)",
    "smart_tp_enabled":       "Smart TP (удержание позиции при высокой уверенности AI)",
    "profit_protect_enabled": "Защита прибыли (фиксация при откате от пика)",
    "large_sell_dca_enabled": "Докупка на панике при крупных продажах китов",
}

TUNABLE_DESCRIPTIONS = {
    "take_profit_pct":         "Цель прибыли на сделку (%)",
    "dca_target_profit_pct":   "Цель прибыли DCA-портфеля (%)",
    "dca_drop_trigger_pct":    "Докупка DCA при падении (%)",
    "smart_buy_pullback_pct":  "Откат перед Smart BUY (%)",
    "profit_protect_drop_pct": "Откат от пика для защиты прибыли (%)",
    "min_ai_confidence":       "Мин. уверенность AI для входа (%)",
    "buy_threshold":           "Порог BUY — вероятность роста (0-1)",
    "sell_threshold":          "Порог SELL — вероятность падения (0-1)",
    "profit_bias_pct":         "Мин. движение для разметки BUY (0-1)",
    "vr_trend_thresh":         "Variance Ratio для тренда (>1)",
    "ev_min_trades":           "Мин. сделок для EV-фильтра",
    "retrain_every":           "Переобучение каждые N тиков",
    "trailing_stop_pct":       "Базовый трейлинг-стоп (%)",
    "trail_stage2_pct":        "Трейлинг стадия 2 (%)",
    "trail_stage3_pct":        "Трейлинг стадия 3 (%)",
    "trail_stage4_pct":        "Трейлинг стадия 4 (%)",
    "smart_tp_min_conf":       "Мин. уверенность для удержания позиции (%)",
    "short_trail_pct":         "Трейлинг для шорт-позиций (%)",
    "ai_size_mult":            "Множитель размера позиции (money management)",
    "dca_stake_ton":           "Размер DCA-ставки в TON (каждый вход)",
    "trade_amount":            "Базовая ставка AI-режима в TON",
    "min_profit_ton_abs":      "Минимальная прибыль в TON (абсолютная, не %)",
}

# ── Внутреннее состояние ───────────────────────────────────────────────────
_lock         = threading.Lock()
_history:     deque     = deque(maxlen=20)
_last_advice: Optional[dict] = None
_auto_apply:  bool = True          # полная автономия по умолчанию
_running:     bool = False
_trades_since_last_run: int = 0
_last_auto_run_ts:     float = 0.0
_next_auto_run_ts:     float = 0.0
_total_adaptations:    int   = 0
_adaptation_log:       deque = deque(maxlen=50)

# ── Фоновый поток автономии ────────────────────────────────────────────────
_bg_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


SYSTEM_PROMPT = """Ты — автономный AI-агент управления торговым ботом GRINCH-GRAM (DEX: DeDust).
Токен: GRINCH/TON | Адрес: EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL
Ссылка: https://dedust.io/coins/EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL

Режим: ПОЛНЫЙ КОНТРОЛЬ — все рекомендации применяются АВТОМАТИЧЕСКИ.
МИССИЯ: зарабатывать МИНИМУМ 3 TON с каждой закрытой сделки. Только в плюс. DCA всегда ВКЛ.

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 1: ЧИТАЙ analytics_buffer — ИСТОРИЯ РЫНКА ЗА ~25 МИН      ║
╚══════════════════════════════════════════════════════════════════╝
В снапшоте есть секция analytics_buffer. Это ГЛАВНЫЙ инструмент анализа.
Изучи её ДО принятия решений:

▸ price.mini_candles — 8 мини-свечей (блоки тиков):
  "Δ%">0 = рост, <0 = падение. Читай как импульсный график.
  price.direction: "↑ РОСТ" / "↓ ПАДЕНИЕ" / "→ БОКОВИК"
  price.change_% — общее изменение за окно буфера
  price.std_% — волатильность: >5% = высокая, <2% = низкая

▸ indicators (за всё окно, cur=текущий, avg=средний):
  rsi.cur < 35 → перепродан (сигнал к покупке DCA)
  rsi.cur > 70 → перекуплен (близко к продаже)
  adx.avg > 25 → сильный тренд → агрессивнее ставки
  atr_%.avg → реальная волатильность = ориентир для take_profit
  vol_ratio.cur > 1.5 → объёмный всплеск (событие на рынке)

▸ regime.dist_% — в каких режимах был рынок:
  UPTREND >40% → повышай take_profit_pct, smart_tp=true
  DOWNTREND >30% → DCA-докупки обязательны, снижай dca_drop_trigger_pct
  VOLATILE >30% → уменьши ставку, повысь min_ai_confidence

▸ ai_signals — качество работы AI:
  buy_rate_% < 15% → AI слишком осторожен → снизь buy_threshold
  blocked_% > 70% → много блокировок → разбери top_blocks:
    "ATR слишком спокойный" → снизь дельта-цели или buy_threshold
    "ждём подтверждение" → снизь retrain_every
  avg_conf < 55% → AI неуверен → повысь profit_bias_pct (строже обучение)

▸ dca_analytics — прогресс текущего DCA-цикла:
  cur_profit_% / cur_profit_ton → сколько заработано сейчас
  max_profit_% → реальный потенциал рынка (какой был максимум за период)
  Если max_profit_% > 2×dca_target_profit_pct → потенциал рынка ВЫШЕ → повысь цель!

▸ trade_stats — статистика сделок из буфера (свежее чем performance):
  win_% = 100% → стратегия работает → можно повысить ставку
  avg_win_ton < 3 → цель слишком низкая → повысь dca_target_profit_pct

▸ smart_money.cur > 0.3 → умные деньги ПОКУПАЮТ → агрессивнее
  smart_money.cur < -0.3 → распродажа → осторожнее, снизь ставку

▸ dex — данные прямо с DeDust/DexScreener по адресу GRINCH (глубже, чем analytics_buffer):
  buy_sell_ratio_h24 > 1.3 → покупателей заметно больше продавцов → ТОРГУЙ АКТИВНЕЕ:
    снизь min_ai_confidence на 3-5 пунктов, увеличь ai_size_mult на 0.1-0.2
  buy_sell_ratio_h24 < 0.7 → продавцов больше → осторожнее, не увеличивай ставку
  recent_flow_usd > 0 и растёт → чистый приток денег в токен за последние сделки → бычий сигнал
  recent_flow_usd < 0 → чистый отток → медвежий сигнал, не повышай агрессивность
  volume_h24_usd — если резко выше обычного (см. vol_ratio в indicators) → событие на рынке, событие подтверждает вход
  liquidity_usd — если упала сильно относительно предыдущих запусков → рынок тоньше → уменьши ставку (независимо от buy_sell_ratio)

▸ recent_ticks (последние 12 тиков) — читай хронологически:
  Поле "pump" ≠ "" → был памп → повысь min_ai_confidence
  Поле "mom" = EXPLOSIVE/SURGE → импульс → увеличь dca_stake_ton
  Поле "blk" → причина блокировки BUY → устрани главный барьер

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 2: БАЛАНС КОШЕЛЬКА (ПРИОРИТЕТ №1)                          ║
╚══════════════════════════════════════════════════════════════════╝
Читай wallet.spendable_ton и вычисли оптимальный dca_stake_ton:
  spendable < 50 TON  → ставка = spendable × 0.20 (мин 5 TON)
  spendable 50-200    → ставка = spendable × 0.25
  spendable 200-500   → ставка = spendable × 0.30
  spendable > 500     → ставка = spendable × 0.35 (макс 500 TON)
trade_amount = dca_stake_ton (AI-режим — тот же размер).
НИКОГДА не ставь > 40% баланса за один вход.

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 3: ПРАВИЛО 3 TON (ЖЕЛЕЗНОЕ)                                ║
╚══════════════════════════════════════════════════════════════════╝
min_profit_ton_abs ≥ 3.0 TON всегда.
dca_target_profit_pct = max(current, (3.0 / dca_stake_ton × 100) + 2%)
Примеры: 20 TON ставка → мин 17%, 50 TON → мин 8%, 100 TON → мин 5%

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 4: РЕЖИМ РЫНКА → ПАРАМЕТРЫ                                 ║
╚══════════════════════════════════════════════════════════════════╝
UPTREND/BREAKOUT: снизь buy_threshold 0.02, повысь take_profit_pct, smart_tp=true
DOWNTREND: DCA обязателен, снизь dca_drop_trigger_pct до 10-15%
RANGING: стандартные параметры, profit_protect=true
VOLATILE/SQUEEZE: ai_size_mult 0.5, повысь min_ai_confidence до 70+

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 5: MONEY MANAGEMENT                                         ║
╚══════════════════════════════════════════════════════════════════╝
win_rate > 65% + avg_conf > 70% → ai_size_mult до 1.4, увеличь ставку
просадка / losses > 0 → ai_size_mult 0.4-0.6, уменьши ставку
Шаг изменения ai_size_mult за один запуск ≤ 0.3.

╔══════════════════════════════════════════════════════════════════╗
║  ЖЕЛЕЗНЫЕ ПРАВИЛА (нельзя нарушать никогда)                      ║
╚══════════════════════════════════════════════════════════════════╝
• ONLY_PROFIT_EXIT = True всегда
• dca_mode = true всегда (не выключать!)
• buy_threshold < sell_threshold всегда
• min_profit_ton_abs ≥ 3.0 TON всегда
• ev_min_trades — только целые числа

ДОПУСТИМЫЕ ДИАПАЗОНЫ:
take_profit_pct:[5-200] dca_target_profit_pct:[5-100] dca_drop_trigger_pct:[5-60]
smart_buy_pullback_pct:[0.2-5] profit_protect_drop_pct:[0.3-20] min_ai_confidence:[40-90]
buy_threshold:[0.40-0.75] sell_threshold:[0.52-0.85] profit_bias_pct:[0.010-0.060]
vr_trend_thresh:[1.05-1.40] ev_min_trades:[5-25] retrain_every:[1-6]
trailing_stop_pct:[2-25] trail_stage2_pct:[2-20] trail_stage3_pct:[1.5-15]
trail_stage4_pct:[1-10] smart_tp_min_conf:[50-90] short_trail_pct:[3-25]
ai_size_mult:[0.3-1.5] dca_stake_ton:[5-1000] trade_amount:[5-1000]
min_profit_ton_abs:[3.0-50.0]

СТРАТЕГИИ: dca_mode=ВСЕГДА_true | short_trading_enabled=только_DOWNTREND
smart_buy_enabled=выкл_при_BREAKOUT | smart_tp_enabled=вкл_при_conf>70%
profit_protect_enabled=ВСЕГДА | large_sell_dca_enabled=при_VOLATILE

ФОРМАТ ОТВЕТА — строго JSON без markdown:
{
  "analysis": "5-6 предложений: что показывает analytics_buffer (тренд, индикаторы, режим, AI-качество), баланс кошелька, почему выбраны эти параметры",
  "recommendations": [
    {"param": "dca_stake_ton", "current": 100, "suggested": 75, "reason": "буфер: rsi.avg=42 перепродан + spendable=300×25%=75"},
    {"param": "dca_target_profit_pct", "current": 10, "suggested": 8, "reason": "ставка 75 TON → мин 3 TON = 4%+2%=6%, округляем до 8%"}
  ],
  "strategy_toggles": {"dca_mode": true},
  "market_verdict": "НАКАПЛИВАТЬ | АКТИВНО_ТОРГОВАТЬ | ОСТОРОЖНО | ПАУЗА",
  "confidence": 0.82,
  "next_check_min": 5
}
ОБЯЗАТЕЛЬНО в каждом запуске: 1) проанализируй analytics_buffer.price и indicators
2) скорректируй dca_stake_ton из баланса 3) проверь min_profit_ton_abs ≥ 3.0
"""


# ──────────────────────────────────────────────────────────────────────────
# Клиент Groq
# ──────────────────────────────────────────────────────────────────────────
def _effective_key() -> str:
    """Актуальный ключ: сначала in-memory (быстро), иначе перечитываем
    из settings_store (на случай если БД была недоступна на момент импорта)."""
    global GROQ_API_KEY
    if GROQ_API_KEY:
        return GROQ_API_KEY
    try:
        from settings_store import get_section as _ss_get
        sec = _ss_get("advisor")
        key = (sec or {}).get("groq_api_key", "")
        if key:
            GROQ_API_KEY = key
    except Exception as e:
        logger.warning(f"[Advisor] не удалось перечитать ключ из settings_store: {e}")
    return GROQ_API_KEY


def _get_client():
    key = _effective_key()
    if not key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=key, base_url=GROQ_BASE_URL)
    except Exception as e:
        logger.error(f"[Advisor] клиент: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────
# Снимок состояния бота
# ──────────────────────────────────────────────────────────────────────────
def _build_snapshot(user_message: str = "") -> dict:
    snap: dict = {"timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}

    # Config
    try:
        from config import Config
        snap["config"] = {
            "take_profit_pct":         Config.TAKE_PROFIT_PCT,
            "dca_mode":                Config.DCA_MODE,
            "dca_target_profit_pct":   Config.DCA_TARGET_PROFIT_PCT,
            "dca_drop_trigger_pct":    getattr(Config, "DCA_DROP_TRIGGER_PCT", 25.0),
            "smart_buy_pullback_pct":  Config.SMART_BUY_PULLBACK_PCT,
            "profit_protect_drop_pct": Config.PROFIT_PROTECT_DROP_PCT,
            "min_ai_confidence":       Config.MIN_AI_CONFIDENCE,
            "only_profit_exit":        Config.ONLY_PROFIT_EXIT,
            "fee_round_trip_pct":      Config.FEE_ROUND_TRIP,
        }
    except Exception:
        snap["config"] = {}

    # AI Engine текущие параметры
    try:
        import ai_engine as ae
        snap["ai_engine"] = {
            "buy_threshold":   ae.BUY_THRESHOLD,
            "sell_threshold":  ae.SELL_THRESHOLD,
            "profit_bias_pct": ae.PROFIT_BIAS_PCT,
            "vr_trend_thresh": ae.VR_TREND_THRESH,
            "ev_min_trades":   ae.EV_MIN_TRADES,
            "retrain_every":   ae.RETRAIN_EVERY,
            "kelly_lookback":  ae.KELLY_LOOKBACK,
        }
    except Exception:
        snap["ai_engine"] = {}

    # Производительность (история сделок)
    try:
        from experience_manager import experience_manager
        rep = experience_manager.get_report()
        snap["performance"] = {
            "total_trades": rep.get("total_trades", 0),
            "win_rate_pct": rep.get("win_rate", 0),
            "avg_profit_pct": rep.get("avg_profit_pct", 0),
            "sharpe":        rep.get("sharpe", 0),
            "best_regime":   rep.get("best_regime", "?"),
            "worst_regime":  rep.get("worst_regime", "?"),
        }
        # Последние 5 сделок
        trades = experience_manager.data.get("trades", [])[-5:]
        snap["recent_trades"] = [
            {"pnl_pct": t.get("pnl_pct", 0), "outcome": t.get("outcome", "?"),
             "regime": t.get("entry_regime", "?"), "close_reason": t.get("close_reason", "?")}
            for t in trades
        ]
    except Exception:
        snap["performance"] = {}
        snap["recent_trades"] = []

    # Рынок
    try:
        from trader import trader
        ai = getattr(trader, "last_ai", None) or {}
        regime = ai.get("regime") or {}
        snap["market"] = {
            "price_usd":  ai.get("price", 0),
            "signal":     ai.get("ai_signal", "HOLD"),
            "regime":     regime.get("name", "?"),
            "atr_pct":    regime.get("atr_pct", 0),
            "rsi":        ai.get("rsi", 50),
            "adx":        regime.get("adx", 0),
            "prob_up":    round(float(ai.get("prob_up", 0)), 3),
            "prob_down":  round(float(ai.get("prob_down", 0)), 3),
            "confidence": ai.get("confidence", 0),
            "pump":       ai.get("pump", "NONE"),
            "var_ratio":  round(float(ai.get("var_ratio", 1.0)), 3),
            "24h_change_pct": -7.44,  # берётся из DexScreener динамически
        }
        snap["portfolio"] = {
            "open_positions": len(trader.open_trades),
            "total_pnl_ton":  trader.stats.get("total_pnl", 0),
            "winning_trades": trader.stats.get("winning_trades", 0),
            "total_trades":   trader.stats.get("total_trades", 0),
        }
        # Реальная цена из DexScreener
        try:
            from price_feed import price_feed
            snap["market"]["price_usd"]      = price_feed.get("GRINCH") or snap["market"]["price_usd"]
            snap["market"]["24h_change_pct"] = getattr(price_feed, "_last_change_24h", -7.44)
        except Exception:
            pass
    except Exception as ex:
        snap["market"]    = {}
        snap["portfolio"] = {}

    # ── Баланс кошелька (ключевой вход для управления ставками) ─────────────
    try:
        from trader import trader as _tr
        _bal = _tr._get_balance_cached() if hasattr(_tr, '_get_balance_cached') else {}
        if not _bal:
            _bal = _tr.exchange.get_balance() if hasattr(_tr, 'exchange') else {}
        _ton = float(_bal.get("TON", 0) or 0)
        _grn = float(_bal.get("GRINCH", 0) or 0)
        _reserve = float(getattr(__import__('config').Config, 'GAS_RESERVE_TON', 0.45))
        _buy_gas = float(getattr(__import__('config').Config, 'BUY_GAS_TON', 0.103))
        _spendable = max(0.0, _ton - _reserve - _buy_gas)
        snap["wallet"] = {
            "ton_balance":   round(_ton, 4),
            "grinch_balance": round(_grn, 2),
            "spendable_ton": round(_spendable, 4),
            "gas_reserve":   _reserve,
        }
    except Exception:
        snap["wallet"] = {"ton_balance": 0, "grinch_balance": 0, "spendable_ton": 0}

    # Ликвидность пула (LiquidityGuard)
    try:
        import liquidity_guard
        lg = liquidity_guard.get_status()
        snap["liquidity"] = {
            "current_usd":  lg.get("current_liq", 0),
            "peak_usd":     lg.get("peak_liq", 0),
            "drop_pct":     lg.get("drop_pct", 0),
            "buys_paused":  lg.get("buys_paused", False),
            "pause_reason": lg.get("pause_reason", ""),
        }
    except Exception:
        snap["liquidity"] = {}

    # ── Глубокие данные прямо с DeDust/DexScreener (не из analytics_buffer) ─
    try:
        from coin_info import coin_info
        m = coin_info.market("GRINCH") or {}
        buys  = m.get("buys_h24")  or 0
        sells = m.get("sells_h24") or 0
        recent = coin_info.trades("GRINCH", limit=30) or []
        flow_usd = 0.0
        buy_cnt = sell_cnt = 0
        for t in recent:
            usd = t.get("amount_usd") or 0.0
            if t.get("kind") == "buy":
                flow_usd += usd
                buy_cnt += 1
            elif t.get("kind") == "sell":
                flow_usd -= usd
                sell_cnt += 1
        snap["dex"] = {
            "source":            m.get("source", "?"),
            "volume_h24_usd":    m.get("volume_h24"),
            "liquidity_usd":     m.get("liquidity"),
            "buys_h24":          buys,
            "sells_h24":         sells,
            "buy_sell_ratio_h24": round(buys / sells, 3) if sells else None,
            "recent_trades_n":   len(recent),
            "recent_buy_count":  buy_cnt,
            "recent_sell_count": sell_cnt,
            "recent_flow_usd":   round(flow_usd, 2),
            "change_h1_pct":     m.get("change_h1"),
            "change_h24_pct":    m.get("change_h24"),
        }
    except Exception as _dex_e:
        snap["dex"] = {"error": str(_dex_e)}

    # ── Аналитический буфер (история рынка — ГЛАВНЫЙ инструмент советника) ──
    # Содержит: цену, индикаторы, режимы, AI-сигналы, DCA-прогресс,
    # умные деньги, историю сделок за последние ~25 минут тиков.
    try:
        from analytics_buffer import analytics_buffer as _ab
        n_ticks = _ab.tick_count()
        if n_ticks >= 3:
            snap["analytics_buffer"] = _ab.get_advisor_summary(window=100)
        else:
            snap["analytics_buffer"] = {
                "status": f"накапливается данные: {n_ticks}/3 тиков получено",
                "ticks": n_ticks,
            }
    except Exception as _ab_e:
        snap["analytics_buffer"] = {"error": str(_ab_e)}

    # Адаптации советника
    snap["advisor_stats"] = {
        "total_adaptations": _total_adaptations,
        "trades_since_last_run": _trades_since_last_run,
        "last_run": datetime.utcfromtimestamp(_last_auto_run_ts).strftime("%H:%M") if _last_auto_run_ts else "—",
    }

    if user_message:
        snap["user_question"] = user_message

    return snap


# ──────────────────────────────────────────────────────────────────────────
# Парсинг ответа LLM
# ──────────────────────────────────────────────────────────────────────────
def _parse_response(text: str) -> dict:
    text = text.strip()
    s, e = text.find("{"), text.rfind("}") + 1
    if s == -1 or e == 0:
        return {"analysis": text, "recommendations": [],
                "market_verdict": "ОСТОРОЖНО", "confidence": 0.5, "next_check_min": AUTO_INTERVAL_MIN}
    try:
        return json.loads(text[s:e])
    except json.JSONDecodeError:
        return {"analysis": text, "recommendations": [],
                "market_verdict": "ОСТОРОЖНО", "confidence": 0.5, "next_check_min": AUTO_INTERVAL_MIN}


# ──────────────────────────────────────────────────────────────────────────
# Применение рекомендаций
# ──────────────────────────────────────────────────────────────────────────
def _apply_recommendations(recs: list) -> list[str]:
    global _total_adaptations
    applied = []
    if not recs:
        return applied

    try:
        from config import Config
        import ai_engine as ae
        from settings_store import update_section
    except Exception as ex:
        logger.error(f"[Advisor] импорт: {ex}")
        return applied

    config_upd: dict = {}

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

        # ── Config параметры ──────────────────────────────────────────
        if param == "take_profit_pct":
            Config.TAKE_PROFIT_PCT = val
            config_upd["take_profit_pct"] = str(val)
        elif param == "dca_target_profit_pct":
            Config.DCA_TARGET_PROFIT_PCT = val
            config_upd["dca_target_profit_pct"] = str(val)
        elif param == "dca_drop_trigger_pct":
            Config.DCA_DROP_TRIGGER_PCT = val
            config_upd["dca_drop_trigger_pct"] = str(val)
        elif param == "smart_buy_pullback_pct":
            Config.SMART_BUY_PULLBACK_PCT = val
            config_upd["smart_buy_pullback_pct"] = str(val)
        elif param == "profit_protect_drop_pct":
            Config.PROFIT_PROTECT_DROP_PCT = val
            config_upd["profit_protect_drop_pct"] = str(val)
        elif param == "min_ai_confidence":
            Config.MIN_AI_CONFIDENCE = val
            config_upd["min_ai_confidence"] = str(val)
        elif param == "trailing_stop_pct":
            Config.TRAILING_STOP_PCT = val
            config_upd["trailing_stop_pct"] = str(val)
        elif param == "trail_stage2_pct":
            Config.TRAIL_STAGE2_PCT = val
            config_upd["trail_stage2_pct"] = str(val)
        elif param == "trail_stage3_pct":
            Config.TRAIL_STAGE3_PCT = val
            config_upd["trail_stage3_pct"] = str(val)
        elif param == "trail_stage4_pct":
            Config.TRAIL_STAGE4_PCT = val
            config_upd["trail_stage4_pct"] = str(val)
        elif param == "smart_tp_min_conf":
            Config.SMART_TP_MIN_CONF = val
            config_upd["smart_tp_min_conf"] = str(val)
        elif param == "short_trail_pct":
            Config.SHORT_TRAIL_PCT = val
            config_upd["short_trail_pct"] = str(val)
        elif param == "ai_size_mult":
            Config.AI_SIZE_MULT = val
            config_upd["ai_size_mult"] = str(val)
        elif param == "dca_stake_ton":
            Config.DCA_STAKE_TON = val
            config_upd["dca_stake_ton"] = str(val)
        elif param == "trade_amount":
            Config.TRADE_AMOUNT = val
            config_upd["trade_amount"] = str(val)
        elif param == "min_profit_ton_abs":
            # Гарантируем абсолютный минимум 3 TON
            val = max(val, 3.0)
            Config.MIN_PROFIT_TON_ABS = val
            config_upd["min_profit_ton_abs"] = str(val)

        # ── AI Engine параметры ───────────────────────────────────────
        elif param == "buy_threshold":
            # Гарантируем buy < sell
            if val < ae.SELL_THRESHOLD:
                ae.BUY_THRESHOLD = val
            else:
                ae.BUY_THRESHOLD = max(lo, ae.SELL_THRESHOLD - 0.05)
                val = ae.BUY_THRESHOLD
        elif param == "sell_threshold":
            # Гарантируем sell > buy
            if val > ae.BUY_THRESHOLD:
                ae.SELL_THRESHOLD = val
            else:
                ae.SELL_THRESHOLD = min(hi, ae.BUY_THRESHOLD + 0.05)
                val = ae.SELL_THRESHOLD
        elif param == "profit_bias_pct":
            ae.PROFIT_BIAS_PCT = val
        elif param == "vr_trend_thresh":
            ae.VR_TREND_THRESH = val
        elif param == "ev_min_trades":
            ae.EV_MIN_TRADES = int(round(val))
            val = ae.EV_MIN_TRADES
        elif param == "retrain_every":
            ae.RETRAIN_EVERY = max(1, int(round(val)))
            val = ae.RETRAIN_EVERY
        else:
            continue

        desc  = TUNABLE_DESCRIPTIONS.get(param, param)
        label = f"{desc}: {rec.get('current', '?')} → {val:.3g}"
        applied.append(label)
        logger.info(f"[Advisor] ✅ {label}")

    # Железные замки
    try:
        Config.ONLY_PROFIT_EXIT = True
        Config.DCA_MODE = True  # DCA всегда включён
        # Гарантируем мин. абсолютную прибыль 3 TON
        if not hasattr(Config, "MIN_PROFIT_TON_ABS") or Config.MIN_PROFIT_TON_ABS < 3.0:
            Config.MIN_PROFIT_TON_ABS = 3.0
    except Exception:
        pass

    if config_upd:
        try:
            update_section("config", config_upd)
        except Exception as ex:
            logger.warning(f"[Advisor] settings_store: {ex}")

    if applied:
        _total_adaptations += len(applied)
        ts = datetime.utcnow().strftime("%H:%M:%S")
        for a in applied:
            _adaptation_log.append({"ts": ts, "change": a})

    return applied


_TOGGLE_ATTR = {
    "dca_mode":               "DCA_MODE",
    "short_trading_enabled":  "SHORT_TRADING_ENABLED",
    "smart_buy_enabled":      "SMART_BUY_ENABLED",
    "smart_tp_enabled":       "SMART_TP_ENABLED",
    "profit_protect_enabled": "PROFIT_PROTECT_ENABLED",
    "large_sell_dca_enabled": "LARGE_SELL_DCA_ENABLED",
}


def _apply_strategy_toggles(toggles: dict) -> list[str]:
    """Включает/выключает целые торговые стратегии по решению советника."""
    global _total_adaptations
    applied = []
    if not toggles:
        return applied
    try:
        from config import Config
        from settings_store import update_section
    except Exception as ex:
        logger.error(f"[Advisor] импорт (toggles): {ex}")
        return applied

    config_upd: dict = {}
    for key, raw_val in toggles.items():
        if key not in STRATEGY_TOGGLES:
            continue
        attr = _TOGGLE_ATTR[key]
        try:
            new_val = bool(raw_val)
        except Exception:
            continue
        old_val = bool(getattr(Config, attr, None))
        if old_val == new_val:
            continue
        setattr(Config, attr, new_val)
        config_upd[key] = "1" if new_val else "0"
        desc  = STRATEGY_TOGGLES[key]
        label = f"{desc}: {'ВКЛ' if old_val else 'ВЫКЛ'} → {'ВКЛ' if new_val else 'ВЫКЛ'}"
        applied.append(label)
        logger.info(f"[Advisor] 🔀 {label}")

    # DCA ВСЕГДА включён — железный замок после применения переключателей
    try:
        from config import Config as _cfg
        if not getattr(_cfg, "DCA_MODE", True):
            _cfg.DCA_MODE = True
            config_upd["dca_mode"] = "1"
            logger.info("[Advisor] 🔒 DCA принудительно возвращён в ВКЛ (железное правило)")
    except Exception:
        pass

    if config_upd:
        try:
            update_section("config", config_upd)
        except Exception as ex:
            logger.warning(f"[Advisor] settings_store (toggles): {ex}")

    if applied:
        _total_adaptations += len(applied)
        ts = datetime.utcnow().strftime("%H:%M:%S")
        for a in applied:
            _adaptation_log.append({"ts": ts, "change": a})

    return applied


# ──────────────────────────────────────────────────────────────────────────
# Основной запрос к советнику
# ──────────────────────────────────────────────────────────────────────────
def run_advisor(auto_apply: bool = None, user_message: str = "",
                trigger: str = "manual") -> dict:
    global _running, _last_advice, _last_auto_run_ts, _next_auto_run_ts
    global _trades_since_last_run

    apply = auto_apply if auto_apply is not None else _auto_apply

    if not _effective_key():
        return {"ok": False, "error": "GROQ_API_KEY не задан"}

    client = _get_client()
    if not client:
        return {"ok": False, "error": "Groq клиент недоступен"}

    with _lock:
        if _running:
            return {"ok": False, "error": "Советник уже работает…"}
        _running = True

    try:
        snap     = _build_snapshot(user_message)
        snap_str = json.dumps(snap, ensure_ascii=False, indent=2)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content":
                f"Текущее состояние бота:\n```json\n{snap_str}\n```"},
        ]

        logger.info(f"[Advisor] 🤖 Запрос к Groq ({trigger})…")
        t0   = time.time()
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.25,
            max_completion_tokens=1200,
        )
        elapsed = round(time.time() - t0, 1)
        raw     = resp.choices[0].message.content or ""
        logger.info(f"[Advisor] ответ за {elapsed}s")

        parsed  = _parse_response(raw)
        applied = []
        if apply:
            applied = _apply_recommendations(parsed.get("recommendations", []))
            applied += _apply_strategy_toggles(parsed.get("strategy_toggles", {}))

        # Следующий запуск через столько минут, сколько советник сам рекомендовал
        suggested_next = int(parsed.get("next_check_min", AUTO_INTERVAL_MIN))
        suggested_next = max(5, min(60, suggested_next))

        now = time.time()
        result = {
            "ok":              True,
            "timestamp":       datetime.utcnow().strftime("%H:%M:%S"),
            "elapsed_s":       elapsed,
            "trigger":         trigger,
            "analysis":        parsed.get("analysis", ""),
            "recommendations": parsed.get("recommendations", []),
            "market_verdict":  parsed.get("market_verdict", "ОСТОРОЖНО"),
            "confidence":      parsed.get("confidence", 0.5),
            "next_check_min":  suggested_next,
            "applied":         applied,
            "auto_applied":    apply,
            "snapshot":        snap,
        }

        with _lock:
            _last_advice       = result
            _last_auto_run_ts  = now
            _next_auto_run_ts  = now + suggested_next * 60
            _trades_since_last_run = 0
            _history.append({
                "ts":      result["timestamp"],
                "trigger": trigger,
                "verdict": result["market_verdict"],
                "applied": applied,
                "conf":    result["confidence"],
                "analysis": result["analysis"][:120],
            })

        if applied:
            logger.info(f"[Advisor] Применено {len(applied)} изм.: {'; '.join(applied[:3])}")

        return result

    except Exception as ex:
        logger.error(f"[Advisor] ошибка: {ex}")
        return {"ok": False, "error": str(ex)}
    finally:
        with _lock:
            _running = False


# ──────────────────────────────────────────────────────────────────────────
# Уведомление о закрытой сделке (вызывается из trader.py)
# ──────────────────────────────────────────────────────────────────────────
def notify_trade_closed(pnl: float = 0.0):
    """Вызывается при каждом закрытии сделки. Триггерит советника если надо."""
    global _trades_since_last_run
    if not _effective_key():
        return
    with _lock:
        _trades_since_last_run += 1
        should_run = (
            _auto_apply
            and not _running
            and _trades_since_last_run >= AUTO_TRADES_TRIGGER
        )
    if should_run:
        outcome = "win" if pnl > 0 else "loss"
        logger.info(f"[Advisor] 🔔 Триггер: {_trades_since_last_run} сделок, "
                    f"последняя={outcome} PNL={pnl:+.4f} TON → авто-запуск")
        _run_in_bg(trigger=f"trade#{_trades_since_last_run}_{outcome}")


# ──────────────────────────────────────────────────────────────────────────
# Фоновый запуск (не блокирует торговый поток)
# ──────────────────────────────────────────────────────────────────────────
def _run_in_bg(trigger: str = "timer"):
    def _worker():
        try:
            run_advisor(auto_apply=True, trigger=trigger)
        except Exception as ex:
            logger.error(f"[Advisor] bg worker: {ex}")
    t = threading.Thread(target=_worker, daemon=True,
                         name=f"advisor-{trigger[:16]}")
    t.start()


# ──────────────────────────────────────────────────────────────────────────
# Фоновый поток таймера
# ──────────────────────────────────────────────────────────────────────────
def _timer_loop():
    global _next_auto_run_ts
    # Первый запуск через 2 минуты после старта (дать боту время загрузиться)
    with _lock:
        _next_auto_run_ts = time.time() + 120
    logger.info(f"[Advisor] ⏱ Таймер запущен, первый авто-анализ через 2 мин")
    while not _stop_event.is_set():
        _stop_event.wait(timeout=10)   # проверяем каждые 10 сек (было 30)
        if _stop_event.is_set():
            break
        with _lock:
            should = (
                _auto_apply
                and not _running
                and bool(_effective_key())
                and time.time() >= _next_auto_run_ts
            )
        if should:
            _run_in_bg(trigger="timer")
            with _lock:
                _next_auto_run_ts = time.time() + AUTO_INTERVAL_MIN * 60


def start_background():
    """Запускает фоновый поток таймера. Вызывается из app.py."""
    global _bg_thread
    if _bg_thread and _bg_thread.is_alive():
        return
    _stop_event.clear()
    _bg_thread = threading.Thread(target=_timer_loop, daemon=True,
                                  name="advisor-timer")
    _bg_thread.start()
    logger.info("[Advisor] 🚀 Фоновый поток автономии запущен")


# ──────────────────────────────────────────────────────────────────────────
# Публичное API
# ──────────────────────────────────────────────────────────────────────────
def _current_strategy_toggles() -> dict:
    try:
        from config import Config
    except Exception:
        return {}
    return {
        key: bool(getattr(Config, attr, False))
        for key, attr in _TOGGLE_ATTR.items()
    }


def _current_size_mult() -> float:
    try:
        from config import Config
        return round(float(getattr(Config, "AI_SIZE_MULT", 1.0)), 3)
    except Exception:
        return 1.0


def get_status() -> dict:
    with _lock:
        now = time.time()
        nxt = _next_auto_run_ts
        return {
            "enabled":          bool(_effective_key()),
            "running":          _running,
            "auto_apply":       _auto_apply,
            "last_advice":      _last_advice,
            "history":          list(_history),
            "adaptation_log":   list(_adaptation_log)[-15:],
            "total_adaptations":_total_adaptations,
            "trades_since_last":_trades_since_last_run,
            "trades_trigger":   AUTO_TRADES_TRIGGER,
            "interval_min":     AUTO_INTERVAL_MIN,
            "ai_size_mult":     _current_size_mult(),
            "next_run_in_sec":  max(0, int(nxt - now)) if nxt > 0 else 0,
            "last_run_ts":      _last_auto_run_ts,
            "model":            GROQ_MODEL,
            "strategy_toggles": _current_strategy_toggles(),
            "strategy_labels":  STRATEGY_TOGGLES,
        }


def toggle_auto_apply() -> bool:
    global _auto_apply
    with _lock:
        _auto_apply = not _auto_apply
        state = _auto_apply
    logger.info(f"[Advisor] auto_apply → {state}")
    return state


def set_config(interval_min: int = None, trades_trigger: int = None):
    global AUTO_INTERVAL_MIN, AUTO_TRADES_TRIGGER
    if interval_min is not None:
        AUTO_INTERVAL_MIN = max(5, min(120, int(interval_min)))
    if trades_trigger is not None:
        AUTO_TRADES_TRIGGER = max(1, min(20, int(trades_trigger)))
    return {"interval_min": AUTO_INTERVAL_MIN, "trades_trigger": AUTO_TRADES_TRIGGER}


def reload_key(key: str = None):
    """Обновить ключ Groq. key=None — читать из env, key=str — установить напрямую."""
    global GROQ_API_KEY
    if key is not None:
        GROQ_API_KEY = key.strip()
    else:
        GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    return bool(GROQ_API_KEY)


def get_adaptation_log() -> list:
    return list(_adaptation_log)
