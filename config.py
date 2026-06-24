import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    EXCHANGE = os.getenv("EXCHANGE", "binance")
    API_KEY = os.getenv("API_KEY", "")
    API_SECRET = os.getenv("API_SECRET", "")
    SYMBOL = os.getenv("SYMBOL", "GRINCH/USDT")
    TIMEFRAME = os.getenv("TIMEFRAME", "1h")
    TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "3"))

    # ── 1 сделка за раз: весь капитал в одну позицию ──
    # С 5.95 TON и торговлей по 3 TON — концентрируемся на одном лучшем входе
    MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "1"))

    # ── Фиксированные цели (резерв если ATR отключён) ──
    STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "3.0"))
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "7.0"))

    # ── Реальная комиссия DeDust DEX — 0.3% за своп с каждой стороны ──
    # При полном цикле: вход 0.3% + выход 0.3% = 0.6% минимальная прибыль для безубытка
    FEE_PCT = float(os.getenv("FEE_PCT", "0.3"))

    # ── Трейлинг-стоп: широкий для волатильного GRINCH ──
    # 2.5% от максимума — фиксирует прибыль, не срабатывает от шума
    TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "2.5"))

    # ── ATR-цели: динамические, на основе реальной волатильности ──
    USE_DYNAMIC_TARGETS = os.getenv("USE_DYNAMIC_TARGETS", "true").lower() == "true"
    # Стоп = 2×ATR — даёт сделке дышать при высокой волатильности GRINCH
    ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "2.0"))
    # Тейк = 4×ATR — соотношение R:R = 1:2 после вычета комиссий
    ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "4.0"))

    # ── Фильтры качества входа ──
    # Не покупать в нисходящем тренде
    TREND_FILTER = os.getenv("TREND_FILTER", "true").lower() == "true"
    # RSI 68 — не покупать у перегрева (строже чем 72)
    RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "68"))
    # AI уверенность мин 62% — только высококонвикционные сигналы
    MIN_AI_CONFIDENCE = float(os.getenv("MIN_AI_CONFIDENCE", "62"))
    # AI-овверрайд только при 78%+ — очень сильный сигнал против тренда
    AI_OVERRIDE_CONFIDENCE = float(os.getenv("AI_OVERRIDE_CONFIDENCE", "78"))

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
