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
    MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "4.0"))

    # ── Профит-настройки (заточка под TON / GRINCH) ──
    # Комиссия за ОДНУ сделку (берётся и на входе, и на выходе → полный цикл = 2×FEE_PCT).
    # TON DEX ~0.3% за своп, CEX ~0.1%. По умолчанию 0.1% за сторону.
    FEE_PCT = float(os.getenv("FEE_PCT", "0.1"))
    # Трейлинг-стоп: подтягивает стоп вверх за ценой, фиксируя прибыль
    TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "1.5"))
    # Динамические цели по волатильности (ATR) вместо фиксированных %
    USE_DYNAMIC_TARGETS = os.getenv("USE_DYNAMIC_TARGETS", "true").lower() == "true"
    ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.5"))   # стоп = 1.5×ATR
    ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "3.0"))   # тейк = 3×ATR (R:R 1:2)
    # Фильтры качества входа
    TREND_FILTER = os.getenv("TREND_FILTER", "true").lower() == "true"  # не покупать в нисходящем тренде
    RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "72"))           # не покупать на перекупленности
    MIN_AI_CONFIDENCE = float(os.getenv("MIN_AI_CONFIDENCE", "55"))     # мин. уверенность AI для входа
    AI_OVERRIDE_CONFIDENCE = float(os.getenv("AI_OVERRIDE_CONFIDENCE", "65"))  # AI один может открыть сделку

    DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"
    SECRET_KEY = os.getenv("SECRET_KEY", "grinch-gram-secret-2024")
    # EQ-адрес выводится из TON_MNEMONIC (WalletV5R1 / W5 - кошелёк TonKeeper)
    TON_WALLET = os.getenv("TON_WALLET", "EQDDgb2BTM-KCjntOoUg6uHllvnu3KGqEquKw6IySVP3hGXJ")
    # Адрес контракта токена GRINCH (TON-джеттон) для получения реальной цены через DexScreener
    GRINCH_TOKEN_ADDRESS = os.getenv("GRINCH_TOKEN_ADDRESS", "EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL")
    # Мнемоника TON-кошелька (24 слова через пробел) — для реальной торговли через DeDust
    # Хранить только в секретах Replit, никогда в коде!
    TON_MNEMONIC = os.getenv("TON_MNEMONIC", "")
    # Режим торговли: "demo" | "dedust"
    TRADE_MODE = os.getenv("TRADE_MODE", "dedust")
