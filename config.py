import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    EXCHANGE = os.getenv("EXCHANGE", "binance")
    API_KEY = os.getenv("API_KEY", "")
    API_SECRET = os.getenv("API_SECRET", "")
    SYMBOL = os.getenv("SYMBOL", "TON/USDT")
    TIMEFRAME = os.getenv("TIMEFRAME", "1h")
    TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "100"))
    MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "4.0"))
    DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
    SECRET_KEY = os.getenv("SECRET_KEY", "grinch-gram-secret-2024")
    TON_WALLET = os.getenv("TON_WALLET", "EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL")
