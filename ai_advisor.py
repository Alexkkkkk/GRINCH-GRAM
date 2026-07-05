"""
ai_advisor.py — Мета-ИИ советник с полной автономией.
Groq LLaMA 3.3-70B (бесплатно) анализирует торговлю и
автоматически адаптирует ВСЕ параметры бота после каждой сделки.
"""
import os, json, logging, threading, time, re
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
AUTO_INTERVAL_MIN    = 3    # авто-запуск каждые N минут (было 5, стало чаще — активнее торговля)
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
    "min_profit_ton_abs":      (2.0,    50.0),   # минимальная АБСОЛЮТНАЯ прибыль в TON
    # ── Трейлинг: уровни активации стадий ───────────────────────
    "trail_breakeven_at":      (3.0,    25.0),   # прибыль % → переход в безубыток
    "trail_stage2_at":         (8.0,    35.0),   # прибыль % → стадия 2
    "trail_stage3_at":         (15.0,   50.0),   # прибыль % → стадия 3
    "trail_stage4_at":         (25.0,   80.0),   # прибыль % → стадия 4
    "smart_tp_tight_trail_pct":(2.0,    20.0),   # тугой трейл в Smart TP режиме
    # ── DCA расширенные параметры ────────────────────────────────
    "dca_pullback_wait_pct":   (5.0,    50.0),   # % падения от пика для нового DCA-цикла
    "dca_max_entries":         (2.0,    20.0),   # макс. DCA-входов за цикл
    # ── Крупные продажи ──────────────────────────────────────────
    "large_sell_dca_ton":      (5.0,   500.0),   # TON для закупки на сигнале крупной продажи
    # ── Защита прибыли ───────────────────────────────────────────
    "profit_protect_ton":      (0.5,    50.0),   # мин. TON прибыли для активации защиты
    # ── AI фильтры входа ─────────────────────────────────────────
    "rsi_overbought":          (65.0,   90.0),   # RSI-уровень перекупленности (блок BUY)
    "ai_autonomous_min_conf":  (45.0,   80.0),   # мин. уверенность для авто-входа AI
    "ai_full_rights_min_conf": (50.0,   85.0),   # мин. уверенность для полных прав AI (без ATR-фильтра)
    "short_min_ai_conf":       (50.0,   90.0),   # мин. уверенность для шорт-позиции
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
    "trail_breakeven_at":      "Прибыль % для перехода стопа в безубыток",
    "trail_stage2_at":         "Прибыль % для активации трейлинга стадии 2",
    "trail_stage3_at":         "Прибыль % для активации трейлинга стадии 3",
    "trail_stage4_at":         "Прибыль % для активации трейлинга стадии 4",
    "smart_tp_tight_trail_pct":"Тугой трейлинг в режиме Smart TP (%)",
    "dca_pullback_wait_pct":   "Падение от пика перед новым DCA-циклом (%)",
    "dca_max_entries":         "Макс. DCA-входов за один цикл (шт.)",
    "large_sell_dca_ton":      "TON для закупки при сигнале крупной продажи",
    "profit_protect_ton":      "Мин. прибыль TON для активации защиты прибыли",
    "rsi_overbought":          "RSI-уровень перекупленности (блок входа)",
    "ai_autonomous_min_conf":  "Мин. уверенность AI для автономного входа (%)",
    "ai_full_rights_min_conf": "Мин. уверенность AI для полных прав (без ATR-фильтра) (%)",
    "short_min_ai_conf":       "Мин. уверенность AI для открытия шорта (%)",
}

# ── Внутреннее состояние ───────────────────────────────────────────────────
_lock         = threading.RLock()
_history:     deque     = deque(maxlen=20)
_last_advice: Optional[dict] = None
_auto_apply:  bool = True          # полная автономия по умолчанию
_running:     bool = False
_trades_since_last_run: int = 0
_last_auto_run_ts:     float = 0.0
_next_auto_run_ts:     float = 0.0
_total_adaptations:    int   = 0
_adaptation_log:       deque = deque(maxlen=50)
_rate_limit:           Optional[dict] = None   # инфо о лимите токенов Groq (если получен 429)

# ── Фоновый поток автономии ────────────────────────────────────────────────
_bg_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


SYSTEM_PROMPT = """Ты — автономный AI-агент управления торговым ботом GRINCH-GRAM (DEX: DeDust).
Токен: GRINCH/TON | Адрес: EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL
Ссылка: https://dedust.io/coins/EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL

Режим: ПОЛНЫЙ КОНТРОЛЬ — все рекомендации применяются АВТОМАТИЧЕСКИ.
МИССИЯ: зарабатывать МИНИМУМ 2 TON с каждой закрытой сделки. Только в плюс. DCA всегда ВКЛ.

╔══════════════════════════════════════════════════════════════════╗
║  🧬 DNA МОНЕТЫ — ЧИТАЙ ПЕРЕД КАЖДЫМ АНАЛИЗОМ                   ║
╚══════════════════════════════════════════════════════════════════╝
Pepe Grinch (GRINCH) — мем-монета TON, пул DeDust GRINCH/GRAM (1% комиссия):
▸ ВОЗРАСТ: создана 10 июня 2026 (~25 дней). Выпускной лаунчпад 100%. NOVA-монета.
▸ ПАРА: GRINCH/GRAM (GRAM = нативный токен TON-блокчейна ≈ $1.75). НЕ путать с TON.
▸ ЛИКВИДНОСТЬ: $40,000-50,000 (МАЛАЯ). Смотри snap.dex.liquidity_usd для актуального.
▸ РЫНОЧНАЯ КЕПКА: ~$830,000 micro-cap → потенциал 5-20x при хайпе.
▸ ПАТТЕРН ЦИКЛА: памп +20-45% → коррекция -6-18% → новый памп (классика мем-монеты).
▸ ДАВЛЕНИЕ ПОКУПАТЕЛЕЙ: buy/sell ratio 1.30-1.55 стабильно (быки перевешивают).
▸ ATR реальный: 5%/свеча 15м → трейлинг-стоп < 12% = шум рынка, НЕ сигнал.
▸ ОБЪЁМ 24ч: ~$25,000-30,000 → ставка >$900/вход уже движет рынком.
▸ КОНКУРЕНТЫ: второй пул STON.fi (ликвидность $1) — полностью игнорировать.

╔══════════════════════════════════════════════════════════════════╗
║  📊 ШАГ 0: ОПРЕДЕЛИ СТАДИЮ РЫНКА (ДЕЛАЙ ПЕРВЫМ)                ║
╚══════════════════════════════════════════════════════════════════╝
Читай dex.change_h24, dex.change_h1, dex.change_h6, dex.ratio_h1, dex.market_stage:

🟢 КОРРЕКЦИЯ (лучшее окно DCA-накопления):
  • change_h24 > +15% И (change_h1 < -4% ИЛИ change_h6 < -5%)
  • dca_drop_trigger_pct = 10-12% (стандарт 25% пропустит ВСЮ коррекцию!)
  • smart_buy_enabled = false, DCA максимально агрессивен.
  • take_profit_pct = 25-40% (следующий памп сопоставим с предыдущим).

🚀 АКТИВНЫЙ ПАМП (держи и расти):
  • change_h1 > +8% ИЛИ vol_ratio.cur > 2.5
  • take_profit_pct = 35-60%, smart_tp = ВКЛ, расширяй trail, НЕ продавай рано.
  • ai_size_mult до 1.3, min_ai_confidence снизь до 48-50.

📈 НАКОПЛЕНИЕ (постепенный вход):
  • change_h24: +5% до +15%, ratio_h24 > 1.2
  • dca_drop_trigger_pct = 15-20%, стандартные параметры, steady entry.

➡️ БОКОВИК (жди импульса):
  • |change_h24| < 5%, |change_h1| < 2%
  • Снизь ставку, min_ai_confidence до 65+, НЕ форсируй вход.

🔴 ДАМП (не входи):
  • change_h24 < -15% ИЛИ (ratio_h1 < 0.7 И change_h1 < -8%)
  • ai_size_mult = 0.3, min_ai_confidence = 80. Ждём RSI < 30 для разворота.

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 1: ЧИТАЙ analytics_buffer — ИСТОРИЯ РЫНКА ЗА ~25 МИН      ║
╚══════════════════════════════════════════════════════════════════╝
▸ price.mini_candles — 8 мини-свечей (блоки тиков):
  "Δ%">0 = рост, <0 = падение. Читай как импульсный график.
  price.direction: "↑ РОСТ" / "↓ ПАДЕНИЕ" / "→ БОКОВИК"
  price.std_% — волатильность: >5% = высокая, <2% = низкая

▸ indicators (cur=текущий, avg=средний за окно):
  rsi.cur < 35 → перепродан → сигнал к покупке DCA
  rsi.cur > 70 → перекуплен → близко к продаже
  adx.avg > 25 → сильный тренд → агрессивнее ставки
  atr_%.avg → реальная волатильность = ориентир для take_profit
  vol_ratio.cur > 1.5 → объёмный всплеск → подтверждение сигнала

▸ regime.dist_% — режимы рынка за буфер:
  UPTREND >40% → повышай take_profit_pct, smart_tp=true
  DOWNTREND >30% → DCA обязателен, снижай dca_drop_trigger_pct до 10-15%
  VOLATILE >30% → уменьши ставку, повысь min_ai_confidence

▸ ai_signals — качество работы AI:
  buy_rate_% < 15% → AI слишком осторожен → снизь buy_threshold до 0.44-0.46
  blocked_% > 70% → разбери top_blocks: "ATR спокойный" → снизь buy_threshold
  avg_conf < 55% → AI неуверен → повысь profit_bias_pct

▸ dca_analytics:
  max_profit_% > 2×dca_target_profit_pct → потенциал рынка ВЫШЕ → повысь цель!
  avg_win_ton < 2 → цель слишком низкая → повысь dca_target_profit_pct

▸ smart_money.cur > 0.3 → умные деньги ПОКУПАЮТ → агрессивнее
  smart_money.cur < -0.3 → распродажа → осторожнее, снизь ставку

▸ dex — данные с DexScreener/DeDust (многотаймфреймные):
  ratio_h24 > 1.3 → бычий рынок → снизь min_ai_confidence на 3-5%, +0.1 ai_size_mult
  ratio_h1 > 1.5 → краткосрочный импульс покупок → агрессивнее входи немедленно
  ratio_h1 < 0.7 → продавцы доминируют сейчас → пауза, жди разворота
  recent_flow_usd > 0 → чистый приток денег → бычий сигнал
  change_h6 → промежуточный фрейм: КОРРЕКЦИЯ = h24>+15% И h6<-5%
  market_stage → предрассчитанная стадия: CORRECTION/PUMP/ACCUMULATION/RANGING/DUMP
  liquidity_usd < $30,000 → уменьши ставку в 2 раза (проскальзывание критично)
  volume_h24_usd >> $25,000 (норма) → событие на рынке, подтверждает вход

▸ recent_ticks (последние 12 тиков — читай хронологически):
  "pump" ≠ "" → был памп → повысь min_ai_confidence
  "mom" = EXPLOSIVE/SURGE → импульс → увеличь dca_stake_ton
  "blk" → причина блокировки BUY → устрани главный барьер

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 2: БАЛАНС + ЛИКВИДНОСТЬ ПУЛА (ПРИОРИТЕТ №1)               ║
╚══════════════════════════════════════════════════════════════════╝
Читай wallet.spendable_ton И dex.liquidity_usd:

РАСЧЁТ СТАВКИ ОТ БАЛАНСА:
  spendable < 50 TON  → ставка = spendable × 0.20 (мин 5 TON)
  spendable 50-200    → ставка = spendable × 0.25
  spendable 200-500   → ставка = spendable × 0.30
  spendable > 500     → ставка = spendable × 0.35 (макс 500 TON)

ОГРАНИЧЕНИЕ MICRO-CAP ПУЛА (железное для GRINCH):
  max_stake_usd = liquidity_usd × 0.02  (максимум 2% ликвидности за вход)
  Пример: liquidity=$47,000 → max=$940 ≈ ~540 GRAM/TON-эквивалент
  liquidity_usd < $30,000 → делай ставку ÷ 2 (проскальзывание резко растёт)
  Если расчётная ставка > max_stake → снижай dca_stake_ton до безопасного уровня.

trade_amount = dca_stake_ton. НИКОГДА не ставь > 40% баланса за один вход.

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 3: ПРАВИЛО 2 TON (ЖЕЛЕЗНОЕ)                                ║
╚══════════════════════════════════════════════════════════════════╝
min_profit_ton_abs ≥ 2.0 TON всегда.
dca_target_profit_pct = max(current, (2.0 / dca_stake_ton × 100) + 2%)
Примеры: 10 TON → мин 22%, 20 TON → мин 12%, 50 TON → мин 6%, 100 TON → мин 4%

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 4: РЕЖИМ РЫНКА → ПАРАМЕТРЫ                                 ║
╚══════════════════════════════════════════════════════════════════╝
UPTREND/BREAKOUT: снизь buy_threshold 0.02, повысь take_profit_pct, smart_tp=true
DOWNTREND: DCA обязателен, снизь dca_drop_trigger_pct до 10-15%
RANGING: стандартные параметры, profit_protect=true
VOLATILE/SQUEEZE: ai_size_mult 0.5, повысь min_ai_confidence до 70+

СПЕЦИАЛЬНО ДЛЯ GRINCH — коррекция после пампа (частый паттерн!):
  Условие: dex.change_h24 > +15% И dex.change_h1 < -4%
  → dca_drop_trigger_pct = 10-12% (НЕ 25% — иначе промахнёмся через всё окно!)
  → smart_buy_enabled = false (входить при каждом триггере без ожидания)
  → take_profit_pct = 25-40% (следующий памп будет сопоставим)
  → large_sell_dca_enabled = true (продажи китов = дешёвая закупка)

╔══════════════════════════════════════════════════════════════════╗
║  ШАГ 5: MONEY MANAGEMENT                                         ║
╚══════════════════════════════════════════════════════════════════╝
win_rate > 65% + avg_conf > 70% → ai_size_mult до 1.4, увеличь ставку
losses > 0 → ai_size_mult 0.4-0.6, уменьши ставку
Шаг изменения ai_size_mult за один запуск ≤ 0.3.

╔══════════════════════════════════════════════════════════════════╗
║  ЖЕЛЕЗНЫЕ ПРАВИЛА (нельзя нарушать никогда)                      ║
╚══════════════════════════════════════════════════════════════════╝
• ONLY_PROFIT_EXIT = True всегда
• dca_mode = true всегда (не выключать!)
• buy_threshold < sell_threshold всегда
• min_profit_ton_abs ≥ 2.0 TON всегда
• ev_min_trades — только целые числа
• dca_stake_ton ≤ 2% ликвидности пула (micro-cap защита)

ДОПУСТИМЫЕ ДИАПАЗОНЫ (все параметры под контролем советника):
— Основные: take_profit_pct:[5-200] dca_target_profit_pct:[5-100] dca_drop_trigger_pct:[5-60]
— Smart BUY: smart_buy_pullback_pct:[0.2-5]
— Защита: profit_protect_drop_pct:[0.3-20] profit_protect_ton:[0.5-50]
— AI-вход: min_ai_confidence:[40-90] ai_autonomous_min_conf:[45-80] ai_full_rights_min_conf:[50-85]
— RSI: rsi_overbought:[65-90]
— AI Engine: buy_threshold:[0.40-0.75] sell_threshold:[0.52-0.85] profit_bias_pct:[0.010-0.060]
— AI Engine: vr_trend_thresh:[1.05-1.40] ev_min_trades:[5-25] retrain_every:[1-6]
— Трейлинг %: trailing_stop_pct:[2-25] trail_stage2_pct:[2-20] trail_stage3_pct:[1.5-15] trail_stage4_pct:[1-10]
— Трейлинг AT: trail_breakeven_at:[3-25] trail_stage2_at:[8-35] trail_stage3_at:[15-50] trail_stage4_at:[25-80]
— Smart TP: smart_tp_min_conf:[50-90] smart_tp_tight_trail_pct:[2-20]
— Шорт: short_trail_pct:[3-25] short_min_ai_conf:[50-90]
— DCA: dca_stake_ton:[5-1000] dca_pullback_wait_pct:[5-50] dca_max_entries:[2-20]
— Ставки: trade_amount:[5-1000] ai_size_mult:[0.3-1.5] large_sell_dca_ton:[5-500]
— Прибыль: min_profit_ton_abs:[2.0-50.0]

СТРАТЕГИИ: dca_mode=ВСЕГДА_true | short_trading_enabled=только_DOWNTREND
smart_buy_enabled=выкл_при_BREAKOUT_и_КОРРЕКЦИИ | smart_tp_enabled=вкл_при_conf>70%
profit_protect_enabled=ВСЕГДА | large_sell_dca_enabled=при_VOLATILE_и_micro-cap_пуле

ФОРМАТ ОТВЕТА — строго JSON без markdown:
{
  "analysis": "7-9 предложений: стадия рынка GRINCH (DNA-секция), что показывает analytics_buffer (тренд/индикаторы/режим), buy/sell ratios по таймфреймам, ликвидность пула, баланс кошелька, обоснование каждого изменённого параметра",
  "recommendations": [
    {"param": "dca_drop_trigger_pct", "current": 25, "suggested": 12, "reason": "КОРРЕКЦИЯ: h1=-6.2% после h24=+32% — стандарт 25% пропустит всё окно накопления"},
    {"param": "dca_stake_ton", "current": 100, "suggested": 75, "reason": "spendable=300×25%=75; ≤2% ликвидности $47k=OK"},
    {"param": "take_profit_pct", "current": 15, "suggested": 30, "reason": "паттерн GRINCH: после +32% следующий памп сопоставим"}
  ],
  "strategy_toggles": {"dca_mode": true, "smart_buy_enabled": false},
  "market_verdict": "НАКАПЛИВАТЬ | АКТИВНО_ТОРГОВАТЬ | ОСТОРОЖНО | ПАУЗА",
  "confidence": 0.85,
  "next_check_min": 3
}
ОБЯЗАТЕЛЬНО в каждом запуске:
1) Определи стадию рынка GRINCH (ШАГ 0) — это меняет ВСЕ остальные параметры
2) Скорректируй dca_drop_trigger_pct под стадию (КОРРЕКЦИЯ → 10-12%, не 25%!)
3) Скорректируй dca_stake_ton из баланса + проверь 2% лимит ликвидности
4) Убедись min_profit_ton_abs ≥ 2.0 и next_check_min ≤ 3 при активном рынке
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
        def _g(attr, default=None):
            return getattr(Config, attr, default)
        snap["config"] = {
            # ── Основные торговые параметры ──
            "take_profit_pct":          _g("TAKE_PROFIT_PCT"),
            "dca_mode":                 _g("DCA_MODE"),
            "dca_target_profit_pct":    _g("DCA_TARGET_PROFIT_PCT"),
            "dca_drop_trigger_pct":     _g("DCA_DROP_TRIGGER_PCT", 12.0),
            "dca_pullback_wait_pct":    _g("DCA_PULLBACK_WAIT_PCT", 25.0),
            "dca_max_entries":          _g("DCA_MAX_ENTRIES", 10),
            "dca_stake_ton":            _g("DCA_STAKE_TON", 100.0),
            "trade_amount":             _g("TRADE_AMOUNT", 100.0),
            "min_profit_ton_abs":       _g("MIN_PROFIT_TON_ABS", 2.0),
            # ── AI фильтры входа ──
            "min_ai_confidence":        _g("MIN_AI_CONFIDENCE"),
            "ai_autonomous_min_conf":   _g("AI_AUTONOMOUS_MIN_CONF", 55.0),
            "ai_full_rights_min_conf":  _g("AI_FULL_RIGHTS_MIN_CONF", 62.0),
            "rsi_overbought":           _g("RSI_OVERBOUGHT", 78.0),
            # ── Трейлинг ──
            "trailing_stop_pct":        _g("TRAILING_STOP_PCT"),
            "trail_breakeven_at":       _g("TRAIL_BREAKEVEN_AT", 10.0),
            "trail_stage2_at":          _g("TRAIL_STAGE2_AT", 18.0),
            "trail_stage2_pct":         _g("TRAIL_STAGE2_PCT"),
            "trail_stage3_at":          _g("TRAIL_STAGE3_AT", 28.0),
            "trail_stage3_pct":         _g("TRAIL_STAGE3_PCT"),
            "trail_stage4_at":          _g("TRAIL_STAGE4_AT", 40.0),
            "trail_stage4_pct":         _g("TRAIL_STAGE4_PCT"),
            "smart_tp_min_conf":        _g("SMART_TP_MIN_CONF"),
            "smart_tp_tight_trail_pct": _g("SMART_TP_TIGHT_TRAIL_PCT", 6.0),
            # ── Защита прибыли ──
            "profit_protect_drop_pct":  _g("PROFIT_PROTECT_DROP_PCT"),
            "profit_protect_ton":       _g("PROFIT_PROTECT_TON", 3.0),
            # ── Smart BUY ──
            "smart_buy_pullback_pct":   _g("SMART_BUY_PULLBACK_PCT"),
            # ── Крупные продажи ──
            "large_sell_dca_ton":       _g("LARGE_SELL_DCA_TON", 100.0),
            # ── Шорт ──
            "short_min_ai_conf":        _g("SHORT_MIN_AI_CONF", 65.0),
            "short_trail_pct":          _g("SHORT_TRAIL_PCT"),
            # ── Прочее ──
            "ai_size_mult":             _g("AI_SIZE_MULT"),
            "only_profit_exit":         _g("ONLY_PROFIT_EXIT"),
            "fee_round_trip_pct":       _g("FEE_ROUND_TRIP"),
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
        # ── Определяем стадию рынка по multi-таймфреймным данным ───────────
        ch24 = m.get("change_h24") or 0.0
        ch6  = m.get("change_h6")  or 0.0
        ch1  = m.get("change_h1")  or 0.0
        r_h1 = m.get("ratio_h1")
        if ch24 > 15 and (ch1 < -4 or ch6 < -5):
            _stage = "CORRECTION"     # памп + текущая коррекция → лучшее окно DCA
        elif ch1 > 8 or (r_h1 and r_h1 > 2.0):
            _stage = "PUMP"           # активный памп прямо сейчас
        elif ch24 > 5 and (m.get("ratio_h24") or 1.0) > 1.2:
            _stage = "ACCUMULATION"   # плавный рост с перевесом покупателей
        elif ch24 < -15 or (r_h1 and r_h1 < 0.7 and ch1 < -8):
            _stage = "DUMP"           # дамп — не входить
        else:
            _stage = "RANGING"        # боковик — ждать импульса
        snap["dex"] = {
            "source":            m.get("source", "?"),
            "market_stage":      _stage,
            "volume_h24_usd":    m.get("volume_h24"),
            "volume_h6_usd":     m.get("volume_h6"),
            "volume_h1_usd":     m.get("volume_h1"),
            "liquidity_usd":     m.get("liquidity"),
            "fdv_usd":           m.get("fdv"),
            "change_m5_pct":     m.get("change_m5"),
            "change_h1_pct":     ch1,
            "change_h6_pct":     ch6,
            "change_h24_pct":    ch24,
            "buys_h24":          buys,
            "sells_h24":         sells,
            "ratio_h24":         m.get("ratio_h24") or (round(buys / sells, 3) if sells else None),
            "buys_h6":           m.get("buys_h6"),
            "sells_h6":          m.get("sells_h6"),
            "ratio_h6":          m.get("ratio_h6"),
            "buys_h1":           m.get("buys_h1"),
            "sells_h1":          m.get("sells_h1"),
            "ratio_h1":          r_h1,
            "recent_trades_n":   len(recent),
            "recent_buy_count":  buy_cnt,
            "recent_sell_count": sell_cnt,
            "recent_flow_usd":   round(flow_usd, 2),
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
            # Гарантируем абсолютный минимум 2 TON
            val = max(val, 2.0)
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

        # ── Трейлинг: уровни активации стадий ────────────────────
        elif param == "trail_breakeven_at":
            Config.TRAIL_BREAKEVEN_AT = val
            config_upd["trail_breakeven_at"] = str(val)
        elif param == "trail_stage2_at":
            Config.TRAIL_STAGE2_AT = val
            config_upd["trail_stage2_at"] = str(val)
        elif param == "trail_stage3_at":
            Config.TRAIL_STAGE3_AT = val
            config_upd["trail_stage3_at"] = str(val)
        elif param == "trail_stage4_at":
            Config.TRAIL_STAGE4_AT = val
            config_upd["trail_stage4_at"] = str(val)
        elif param == "smart_tp_tight_trail_pct":
            Config.SMART_TP_TIGHT_TRAIL_PCT = val
            config_upd["smart_tp_tight_trail_pct"] = str(val)

        # ── DCA расширенные ───────────────────────────────────────
        elif param == "dca_pullback_wait_pct":
            Config.DCA_PULLBACK_WAIT_PCT = val
            config_upd["dca_pullback_wait_pct"] = str(val)
        elif param == "dca_max_entries":
            Config.DCA_MAX_ENTRIES = max(2, int(round(val)))
            val = Config.DCA_MAX_ENTRIES
            config_upd["dca_max_entries"] = str(val)

        # ── Крупные продажи ───────────────────────────────────────
        elif param == "large_sell_dca_ton":
            Config.LARGE_SELL_DCA_TON = val
            config_upd["large_sell_dca_ton"] = str(val)

        # ── Защита прибыли ────────────────────────────────────────
        elif param == "profit_protect_ton":
            Config.PROFIT_PROTECT_TON = val
            config_upd["profit_protect_ton"] = str(val)

        # ── AI фильтры входа ──────────────────────────────────────
        elif param == "rsi_overbought":
            Config.RSI_OVERBOUGHT = val
            config_upd["rsi_overbought"] = str(val)
        elif param == "ai_autonomous_min_conf":
            Config.AI_AUTONOMOUS_MIN_CONF = val
            config_upd["ai_autonomous_min_conf"] = str(val)
        elif param == "ai_full_rights_min_conf":
            Config.AI_FULL_RIGHTS_MIN_CONF = val
            config_upd["ai_full_rights_min_conf"] = str(val)
        elif param == "short_min_ai_conf":
            Config.SHORT_MIN_AI_CONF = val
            config_upd["short_min_ai_conf"] = str(val)

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
        # Гарантируем мин. абсолютную прибыль 2 TON
        if not hasattr(Config, "MIN_PROFIT_TON_ABS") or Config.MIN_PROFIT_TON_ABS < 2.0:
            Config.MIN_PROFIT_TON_ABS = 2.0
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
        suggested_next = max(2, min(60, suggested_next))

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
        _record_rate_limit(str(ex))
        return {"ok": False, "error": str(ex)}
    finally:
        with _lock:
            _running = False


def _record_rate_limit(err_text: str) -> None:
    """Парсит текст ошибки Groq (429 rate_limit_exceeded) и сохраняет лимит/сброс."""
    global _rate_limit
    if "rate_limit" not in err_text and "429" not in err_text:
        # Не лимит — если предыдущая ошибка лимита устарела (>1ч), не трогаем её.
        return
    try:
        limit_m  = re.search(r"Limit\s+(\d+)", err_text)
        used_m   = re.search(r"Used\s+(\d+)", err_text)
        req_m    = re.search(r"Requested\s+(\d+)", err_text)
        wait_m   = re.search(r"try again in\s+([\dhms.]+)", err_text)
        reset_s  = 0.0
        if wait_m:
            parts = re.findall(r"([\d.]+)([hms])", wait_m.group(1))
            for val, unit in parts:
                val = float(val)
                reset_s += val * (3600 if unit == "h" else 60 if unit == "m" else 1)
        with _lock:
            _rate_limit = {
                "limited":     True,
                "limit":       int(limit_m.group(1)) if limit_m else None,
                "used":        int(used_m.group(1))  if used_m  else None,
                "requested":   int(req_m.group(1))   if req_m   else None,
                "reset_at_ts": time.time() + reset_s if reset_s else None,
                "detected_ts": time.time(),
                "raw":         err_text[:300],
            }
    except Exception:
        pass


def _rate_limit_status() -> Optional[dict]:
    """Возвращает инфо о лимите Groq, сбрасывая её, если время ожидания уже прошло."""
    with _lock:
        rl = _rate_limit
    if not rl:
        return None
    reset_ts = rl.get("reset_at_ts")
    if reset_ts and time.time() >= reset_ts:
        return None  # лимит должен был уже сброситься
    out = dict(rl)
    out["reset_in_sec"] = max(0, int(reset_ts - time.time())) if reset_ts else None
    return out


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
            "rate_limit":       _rate_limit_status(),
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
