"""SQLite database for grid trades, PnL and statistics."""
import sqlite3
import logging
from typing import List, Dict
from config import Config

log = logging.getLogger("grid_db")

class GridDatabase:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or Config.GRID_DB_PATH
        self._init_tables()
    def _connect(self): return sqlite3.connect(self.db_path, check_same_thread=False)
    def _init_tables(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, side TEXT NOT NULL, level_id INTEGER, price REAL NOT NULL, quantity REAL NOT NULL, amount_usdt REAL, profit_usdt REAL DEFAULT 0, fee_usdt REAL DEFAULT 0, order_id TEXT, status TEXT DEFAULT 'filled', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS pnl_history (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, total_profit_usdt REAL DEFAULT 0, roi_pct REAL DEFAULT 0, current_price REAL, recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS grid_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, upper_price REAL, lower_price REAL, grid_count INTEGER, total_investment REAL, step_pct REAL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS grid_levels (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, level_id INTEGER, side TEXT, price REAL, quantity REAL, status TEXT DEFAULT 'waiting', order_id TEXT, paired_level_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                CREATE INDEX IF NOT EXISTS idx_pnl_symbol ON pnl_history(symbol);
            """)
            conn.commit()
    def save_trade(self, symbol: str, side: str, level_id: int, price: float, quantity: float, amount_usdt: float = None, profit_usdt: float = 0, fee_usdt: float = 0, order_id: str = None) -> int:
        with self._connect() as conn:
            cursor = conn.execute("INSERT INTO trades (symbol, side, level_id, price, quantity, amount_usdt, profit_usdt, fee_usdt, order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (symbol, side, level_id, price, quantity, amount_usdt, profit_usdt, fee_usdt, order_id))
            conn.commit(); return cursor.lastrowid
    def get_trades(self, symbol: str = None, limit: int = 100) -> List[Dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if symbol: rows = conn.execute("SELECT * FROM trades WHERE symbol = ? ORDER BY created_at DESC LIMIT ?", (symbol, limit)).fetchall()
            else: rows = conn.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]
    def get_stats(self, symbol: str = None) -> Dict:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row; where = "WHERE symbol = ?" if symbol else ""; params = (symbol,) if symbol else ()
            total_profit = conn.execute(f"SELECT COALESCE(SUM(profit_usdt), 0) as total FROM trades {where}", params).fetchone()["total"]
            buy_count = conn.execute(f"SELECT COUNT(*) as cnt FROM trades {where} AND side = 'buy'", params).fetchone()["cnt"]
            sell_count = conn.execute(f"SELECT COUNT(*) as cnt FROM trades {where} AND side = 'sell'", params).fetchone()["cnt"]
            win_count = conn.execute(f"SELECT COUNT(*) as cnt FROM trades {where} AND profit_usdt > 0", params).fetchone()["cnt"]
            loss_count = conn.execute(f"SELECT COUNT(*) as cnt FROM trades {where} AND profit_usdt < 0", params).fetchone()["cnt"]
            return {"total_profit_usdt": round(total_profit, 4), "buy_trades": buy_count, "sell_trades": sell_count, "win_trades": win_count, "loss_trades": loss_count, "total_trades": buy_count + sell_count}
    def save_pnl_snapshot(self, symbol: str, total_profit: float, roi_pct: float, current_price: float):
        with self._connect() as conn:
            conn.execute("INSERT INTO pnl_history (symbol, total_profit_usdt, roi_pct, current_price) VALUES (?, ?, ?, ?)", (symbol, total_profit, roi_pct, current_price)); conn.commit()
    def get_pnl_history(self, symbol: str = None, hours: int = 24) -> List[Dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if symbol: rows = conn.execute("SELECT * FROM pnl_history WHERE symbol = ? AND recorded_at > datetime('now', ?) ORDER BY recorded_at ASC", (symbol, f"-{hours} hours")).fetchall()
            else: rows = conn.execute("SELECT * FROM pnl_history WHERE recorded_at > datetime('now', ?) ORDER BY recorded_at ASC", (f"-{hours} hours",)).fetchall()
            return [dict(row) for row in rows]
    def save_grid_config(self, symbol: str, upper: float, lower: float, grid_count: int, investment: float, step_pct: float):
        with self._connect() as conn:
            conn.execute("INSERT INTO grid_configs (symbol, upper_price, lower_price, grid_count, total_investment, step_pct) VALUES (?, ?, ?, ?, ?, ?)", (symbol, upper, lower, grid_count, investment, step_pct)); conn.commit()
    def clear_levels(self, symbol: str):
        with self._connect() as conn: conn.execute("DELETE FROM grid_levels WHERE symbol = ?", (symbol,)); conn.commit()
    def save_level(self, symbol: str, level_id: int, side: str, price: float, quantity: float, status: str, order_id: str = None, paired_level_id: int = None):
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO grid_levels (symbol, level_id, side, price, quantity, status, order_id, paired_level_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (symbol, level_id, side, price, quantity, status, order_id, paired_level_id)); conn.commit()
    def get_levels(self, symbol: str) -> List[Dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM grid_levels WHERE symbol = ? ORDER BY price ASC", (symbol,)).fetchall()
            return [dict(row) for row in rows]
