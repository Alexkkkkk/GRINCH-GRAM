"""Configuration for GRINCH-GRAM Spot Grid Bot."""

import os


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "grinch-grid-secret-key-change-in-production")
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "5000"))
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
    USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
    SYMBOL = os.getenv("GRID_SYMBOL", "AUDIOUSDT")
    GRID_COUNT = int(os.getenv("GRID_COUNT", "40"))
    TOTAL_INVESTMENT = float(os.getenv("TOTAL_INVESTMENT", "1000"))
    UPPER_PRICE = float(os.getenv("UPPER_PRICE", "0"))
    LOWER_PRICE = float(os.getenv("LOWER_PRICE", "0"))
    FEE_PCT = 0.1
    MIN_ORDER_USDT = 10.0
    TICK_INTERVAL = int(os.getenv("TICK_INTERVAL", "10"))
    RECENTER_THRESHOLD = float(os.getenv("RECENTER_THRESHOLD", "1.5"))
    RECENTER_COOLDOWN = int(os.getenv("RECENTER_COOLDOWN", "1800"))
    DB_PATH = os.getenv("DB_PATH", "grid_bot.db")
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
    GITHUB_REPO = os.getenv("GITHUB_REPO", "Alexkkkkk/GRINCH-GRAM")
    REPORT_ERRORS = os.getenv("REPORT_ERRORS", "true").lower() == "true"
