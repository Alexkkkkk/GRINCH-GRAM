"""Configuration for GRINCH-GRAM Spot Grid Bot."""
import os


class Config:
    # Flask
    SECRET_KEY = "grinch-grid-secret-key-change-in-production"
    HOST = "0.0.0.0"
    PORT = 5000
    DEBUG = False

    # Binance API (set via environment or edit here)
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
    USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"

    # Grid Trading
    SYMBOL = "AUDIOUSDT"
    GRID_COUNT = 40
    TOTAL_INVESTMENT = 1000.0
    UPPER_PRICE = 0.0
    LOWER_PRICE = 0.0

    # Profit calculation
    FEE_PCT = 0.1
    MIN_ORDER_USDT = 10.0

    # Intervals
    TICK_INTERVAL = 10
    RECENTER_THRESHOLD = 1.5
    RECENTER_COOLDOWN = 1800

    # Database
    DB_PATH = "grid_bot.db"
