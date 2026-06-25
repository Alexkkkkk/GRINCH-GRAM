import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    EXCHANGE = os.getenv("EXCHANGE", "binance")
    API_KEY = os.getenv("API_KEY", "")
    API_SECRET = os.getenv("API_SECRET", "")
    SYMBOL = os.getenv("SYMBOL", "GRINCH/USDT")
    TIMEFRAME = os.getenv("TIMEFRAME", "1h")
    TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "1"))

    # ── 1 сделка за раз: весь капитал в одну позицию ──
    # Торговля по 1 TON на сделку — концентрируемся на одном лучшем входе
    MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "1"))

    # ── Комиссия DeDust DEX ──
    # 0.3% за каждую сторону: вход 0.3% + выход 0.3% = 0.6% суммарно
    FEE_PCT = float(os.getenv("FEE_PCT", "0.3"))
    FEE_ROUND_TRIP = FEE_PCT * 2   # = 0.6%

    # ── Цели: +20% НЕТТО (после всех комиссий) ──────────────────────────
    # Gross TP = 20% + 0.6% комиссии = 20.6% от цены входа
    # Пример: вход 0.000380 TON → выход при 0.000458 TON → чистая прибыль +20%
    TARGET_NET_PCT  = float(os.getenv("TARGET_NET_PCT",  "20.0"))  # желаемая прибыль
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "20.6"))  # gross = net + fees
    STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "5.0"))   # стоп шире → даём дышать до 20%

    # ── Прогрессивный трейлинг-стоп (защита прибыли на пути к 20%) ──────
    # Этап 1 (прибыль > 5%):  поднимаем стоп на уровень безубытка
    # Этап 2 (прибыль > 10%): трейлинг 6% от максимума → защита минимум +4%
    # Этап 3 (прибыль > 15%): трейлинг 4% от максимума → защита минимум +11%
    # Этап 4 (прибыль > 20%): трейлинг 2% от максимума → фиксируем ≥18% нетто
    TRAIL_BREAKEVEN_AT  = float(os.getenv("TRAIL_BREAKEVEN_AT",  "5.0"))   # % прибыли → стоп в безубыток
    TRAIL_STAGE2_AT     = float(os.getenv("TRAIL_STAGE2_AT",    "10.0"))   # % → трейлинг 6%
    TRAIL_STAGE2_PCT    = float(os.getenv("TRAIL_STAGE2_PCT",    "6.0"))
    TRAIL_STAGE3_AT     = float(os.getenv("TRAIL_STAGE3_AT",    "15.0"))   # % → трейлинг 4%
    TRAIL_STAGE3_PCT    = float(os.getenv("TRAIL_STAGE3_PCT",    "4.0"))
    TRAIL_STAGE4_AT     = float(os.getenv("TRAIL_STAGE4_AT",    "20.0"))   # % → трейлинг 2%
    TRAIL_STAGE4_PCT    = float(os.getenv("TRAIL_STAGE4_PCT",    "2.0"))
    TRAILING_STOP_PCT   = float(os.getenv("TRAILING_STOP_PCT",   "7.0"))   # начальный трейлинг (до 5%)

    # ── ATR-цели: динамические ────────────────────────────────────────────
    USE_DYNAMIC_TARGETS = os.getenv("USE_DYNAMIC_TARGETS", "true").lower() == "true"
    # Стоп = 2.5×ATR — шире чем раньше, чтобы сделка дышала до 20%
    ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "2.5"))
    # Тейк = мин 20.6% — ATR×multiplier используется только если он ВЫШЕ 20.6%
    ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "8.0"))

    # ── Фильтры качества входа ──
    # Не покупать в нисходящем тренде
    TREND_FILTER = os.getenv("TREND_FILTER", "true").lower() == "true"
    # RSI 78 — для мем-монеты GRINCH RSI 68-75 это норма в памп; блокируем только экстремум
    RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "78"))
    # AI уверенность мин 62% — только высококонвикционные сигналы
    MIN_AI_CONFIDENCE = float(os.getenv("MIN_AI_CONFIDENCE", "62"))
    # AI-овверрайд только при 78%+ — очень сильный сигнал против тренда
    AI_OVERRIDE_CONFIDENCE = float(os.getenv("AI_OVERRIDE_CONFIDENCE", "78"))
    # AI жёсткий овверрайд: при ≥93% уверенности игнорируем RSI/аномалию (только DOWNTREND блокирует)
    AI_HARD_OVERRIDE_CONFIDENCE = float(os.getenv("AI_HARD_OVERRIDE_CONFIDENCE", "93"))
    # Mean Reversion Override: RSI < 25 + AI > 85% → входим даже в DOWNTREND (отскок от дна)
    RSI_OVERSOLD_REVERSAL = float(os.getenv("RSI_OVERSOLD_REVERSAL", "25"))
    REVERSAL_AI_MIN       = float(os.getenv("REVERSAL_AI_MIN", "85"))

    DEMO_MODE  = os.getenv("DEMO_MODE",  "false").lower() == "true"
    SECRET_KEY = os.getenv("SECRET_KEY", "grinch-gram-secret-2024")
    # EQ-адрес выводится из TON_MNEMONIC (WalletV5R1 / W5 — кошелёк TonKeeper)
    TON_WALLET = os.getenv("TON_WALLET", "EQDDgb2BTM-KCjntOoUg6uHllvnu3KGqEquKw6IySVP3hGXJ")
    # Адрес контракта токена GRINCH (TON-джеттон)
    GRINCH_TOKEN_ADDRESS = os.getenv("GRINCH_TOKEN_ADDRESS", "EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL")
    # Мнемоника TON-кошелька (24 слова через пробел) — хранить только в секретах!
    TON_MNEMONIC = os.getenv("TON_MNEMONIC", "")
    # Режим торговли: "demo" | "dedust"
    TRADE_MODE = os.getenv("TRADE_MODE", "dedust")
