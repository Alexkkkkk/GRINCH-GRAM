"""
db_store.py — PostgreSQL persistence layer для GRINCH-GRAM.

Единый модуль работы с БД. Остальные модули (settings_store, experience_manager,
wallet_tracker) вызывают функции отсюда и не знают о деталях подключения.

• CONNECTION_POOL — Thread-safe пул соединений psycopg2.
• Схема создаётся при первом запуске (CREATE TABLE IF NOT EXISTS).
• При отсутствии DATABASE_URL или ошибке подключения — все функции вернут
  None / пустой dict / [] и ни одна не сломает запуск бота.
"""

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime

import psycopg2
try:
    import numpy as _np
    class _NpEncoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, _np.integer): return int(o)
            if isinstance(o, _np.floating): return float(o)
            if isinstance(o, _np.bool_): return bool(o)
            if isinstance(o, _np.ndarray): return o.tolist()
            return super().default(o)
    def _jdumps(obj, **kw): return json.dumps(obj, cls=_NpEncoder, **kw)
except ImportError:
    def _jdumps(obj, **kw): return json.dumps(obj, **kw)
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# ── БД берётся из переменной окружения DATABASE_URL (Replit PostgreSQL) ─────
DATABASE_URL = os.environ.get("DATABASE_URL")

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()
_available = False   # True только если пул успешно создан

# ── DDL ──────────────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS bot_settings (
    section    VARCHAR(100) NOT NULL,
    key        VARCHAR(200) NOT NULL,
    value      TEXT,
    updated_at TIMESTAMP   DEFAULT NOW(),
    PRIMARY KEY (section, key)
);

CREATE TABLE IF NOT EXISTS bot_trades (
    id         VARCHAR(100) PRIMARY KEY,
    data       JSONB        NOT NULL,
    closed_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_equity (
    id         BIGSERIAL    PRIMARY KEY,
    ts         TIMESTAMP    NOT NULL,
    ton        DOUBLE PRECISION,
    grinch     DOUBLE PRECISION,
    grinch_usd DOUBLE PRECISION,
    equity_ton DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS bot_equity_ts ON bot_equity (ts);

CREATE TABLE IF NOT EXISTS bot_open_trades (
    trade_id   VARCHAR(100) PRIMARY KEY,
    data       JSONB        NOT NULL,
    updated_at TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_ai_state (
    key        VARCHAR(200) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_wallets (
    address    VARCHAR(200) PRIMARY KEY,
    data       JSONB,
    updated_at TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_wallet_meta (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP    DEFAULT NOW()
);
"""


# ── Инициализация пула ────────────────────────────────────────────────────────
def _init_pool():
    global _pool, _available
    if not DATABASE_URL:
        logger.warning("[DB] DATABASE_URL не задан — работаем без PostgreSQL")
        return
    try:
        p = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=16,
            dsn=DATABASE_URL,
            connect_timeout=10,
        )
        with p.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute(_DDL)
            conn.commit()
            p.putconn(conn)
        _pool = p
        _available = True
        print("[DB] ✅ PostgreSQL подключён и схема готова")
    except Exception as e:
        print(f"[DB] ⚠️ Ошибка подключения к PostgreSQL: {e} — используем JSON-файлы")
        _available = False


def is_available() -> bool:
    return _available


@contextmanager
def _conn():
    """Context-manager: берёт соединение из пула, auto-commit/rollback."""
    global _pool, _available
    if not _available or _pool is None:
        raise RuntimeError("DB not available")
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _pool.putconn(conn)


# ── Инициализируем при импорте ────────────────────────────────────────────────
with _pool_lock:
    _init_pool()


# ═══════════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

def settings_get_section(section: str) -> dict:
    if not _available:
        return {}
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT key, value FROM bot_settings WHERE section = %s",
                    (section,)
                )
                return {row["key"]: _decode(row["value"]) for row in cur.fetchall()}
    except Exception as e:
        logger.warning(f"[DB] settings_get_section error: {e}")
        return {}


def settings_update_section(section: str, updates: dict):
    if not _available:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                for key, val in updates.items():
                    cur.execute("""
                        INSERT INTO bot_settings (section, key, value, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (section, key) DO UPDATE
                          SET value = EXCLUDED.value, updated_at = NOW()
                    """, (section, key, _encode(val)))
    except Exception as e:
        logger.warning(f"[DB] settings_update_section error: {e}")


def settings_get(section: str, key: str):
    """Читает одно значение из bot_settings. Возвращает None если не найдено."""
    if not _available:
        return None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM bot_settings WHERE section=%s AND key=%s",
                    (section, key)
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        logger.warning(f"[DB] settings_get error: {e}")
        return None


def settings_get_all() -> dict:
    if not _available:
        return {}
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT section, key, value FROM bot_settings")
                result: dict = {}
                for row in cur.fetchall():
                    s = row["section"]
                    result.setdefault(s, {})[row["key"]] = _decode(row["value"])
                return result
    except Exception as e:
        logger.warning(f"[DB] settings_get_all error: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADES (закрытые сделки)
# ═══════════════════════════════════════════════════════════════════════════════

def trades_upsert(trade: dict):
    if not _available:
        return
    trade_id = str(trade.get("id") or "")
    if not trade_id:
        return
    closed_at_str = trade.get("closed_at") or trade.get("exit_time")
    closed_at = None
    if closed_at_str:
        try:
            closed_at = datetime.fromisoformat(str(closed_at_str))
        except Exception:
            pass
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_trades (id, data, closed_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (id) DO UPDATE
                      SET data = EXCLUDED.data, closed_at = EXCLUDED.closed_at
                """, (trade_id, _jdumps(trade, ensure_ascii=False), closed_at))
    except Exception as e:
        logger.warning(f"[DB] trades_upsert error: {e}")


def trades_get_all(limit: int = 1000) -> list:
    if not _available:
        return []
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT data FROM bot_trades ORDER BY closed_at ASC NULLS LAST LIMIT %s",
                    (limit,)
                )
                return [row["data"] for row in cur.fetchall()]
    except Exception as e:
        logger.warning(f"[DB] trades_get_all error: {e}")
        return []


def trades_count() -> int:
    if not _available:
        return -1
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM bot_trades")
                row = cur.fetchone()
                return row[0] if row else 0
    except Exception:
        return -1


def trades_bulk_insert(trades: list):
    if not _available or not trades:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                for t in trades:
                    tid = str(t.get("id") or "")
                    if not tid:
                        continue
                    closed_at_str = t.get("closed_at") or t.get("exit_time")
                    closed_at = None
                    if closed_at_str:
                        try:
                            closed_at = datetime.fromisoformat(str(closed_at_str))
                        except Exception:
                            pass
                    cur.execute("""
                        INSERT INTO bot_trades (id, data, closed_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                    """, (tid, _jdumps(t, ensure_ascii=False), closed_at))
    except Exception as e:
        logger.warning(f"[DB] trades_bulk_insert error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  EQUITY (кривая баланса)
# ═══════════════════════════════════════════════════════════════════════════════

def equity_insert(point: dict):
    if not _available:
        return
    try:
        ts_str = point.get("t")
        ts = datetime.fromisoformat(ts_str) if ts_str else datetime.utcnow()
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_equity (ts, ton, grinch, grinch_usd, equity_ton)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    ts,
                    point.get("ton"),
                    point.get("grinch"),
                    point.get("grinch_usd"),
                    point.get("equity_ton"),
                ))
    except Exception as e:
        logger.warning(f"[DB] equity_insert error: {e}")


def equity_get_all(limit: int = 3000) -> list:
    if not _available:
        return []
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT ts, ton, grinch, grinch_usd, equity_ton FROM bot_equity"
                    " ORDER BY ts ASC LIMIT %s",
                    (limit,)
                )
                result = []
                for row in cur.fetchall():
                    result.append({
                        "t":          row["ts"].isoformat() if row["ts"] else None,
                        "ton":        row["ton"],
                        "grinch":     row["grinch"],
                        "grinch_usd": row["grinch_usd"],
                        "equity_ton": row["equity_ton"],
                    })
                return result
    except Exception as e:
        logger.warning(f"[DB] equity_get_all error: {e}")
        return []


def equity_count() -> int:
    if not _available:
        return -1
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM bot_equity")
                row = cur.fetchone()
                return row[0] if row else 0
    except Exception:
        return -1


def equity_bulk_insert(points: list):
    if not _available or not points:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                for p in points:
                    ts_str = p.get("t")
                    try:
                        ts = datetime.fromisoformat(ts_str) if ts_str else datetime.utcnow()
                    except Exception:
                        ts = datetime.utcnow()
                    cur.execute("""
                        INSERT INTO bot_equity (ts, ton, grinch, grinch_usd, equity_ton)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        ts,
                        p.get("ton"), p.get("grinch"),
                        p.get("grinch_usd"), p.get("equity_ton"),
                    ))
    except Exception as e:
        logger.warning(f"[DB] equity_bulk_insert error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  OPEN TRADES (открытые позиции)
# ═══════════════════════════════════════════════════════════════════════════════

def open_trades_save(trades: list):
    if not _available:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bot_open_trades")
                for t in trades:
                    tid = str(t.get("id") or "")
                    if not tid:
                        continue
                    cur.execute("""
                        INSERT INTO bot_open_trades (trade_id, data, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (trade_id) DO UPDATE
                          SET data = EXCLUDED.data, updated_at = NOW()
                    """, (tid, _jdumps(t, ensure_ascii=False)))
    except Exception as e:
        logger.warning(f"[DB] open_trades_save error: {e}")


def open_trades_get() -> list:
    if not _available:
        return []
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT data FROM bot_open_trades ORDER BY updated_at ASC")
                return [row["data"] for row in cur.fetchall()]
    except Exception as e:
        logger.warning(f"[DB] open_trades_get error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  AI STATE (контрольные параметры + опыт ИИ)
# ═══════════════════════════════════════════════════════════════════════════════

def ai_state_set(key: str, value):
    if not _available:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_ai_state (key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE
                      SET value = EXCLUDED.value, updated_at = NOW()
                """, (key, _encode(value)))
    except Exception as e:
        logger.warning(f"[DB] ai_state_set({key}) error: {e}")


def ai_state_get(key: str, default=None):
    if not _available:
        return default
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM bot_ai_state WHERE key = %s", (key,))
                row = cur.fetchone()
                return _decode(row[0]) if row else default
    except Exception as e:
        logger.warning(f"[DB] ai_state_get({key}) error: {e}")
        return default


def ai_state_get_all() -> dict:
    if not _available:
        return {}
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT key, value FROM bot_ai_state")
                return {row["key"]: _decode(row["value"]) for row in cur.fetchall()}
    except Exception as e:
        logger.warning(f"[DB] ai_state_get_all error: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
#  WALLETS (кошельки умных денег)
# ═══════════════════════════════════════════════════════════════════════════════

def wallets_save(wallets: dict, events: list, seen: list, last_poll: float):
    if not _available:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                for addr, data in wallets.items():
                    cur.execute("""
                        INSERT INTO bot_wallets (address, data, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (address) DO UPDATE
                          SET data = EXCLUDED.data, updated_at = NOW()
                    """, (addr, _jdumps(data, ensure_ascii=False)))
                for key, val in [
                    ("events",    _jdumps(events[-5000:], ensure_ascii=False)),
                    ("seen",      _jdumps(list(seen)[-50000:], ensure_ascii=False)),
                    ("last_poll", str(last_poll)),
                ]:
                    cur.execute("""
                        INSERT INTO bot_wallet_meta (key, value, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (key) DO UPDATE
                          SET value = EXCLUDED.value, updated_at = NOW()
                    """, (key, val))
    except Exception as e:
        logger.warning(f"[DB] wallets_save error: {e}")


def wallets_load() -> tuple[dict, list, set, float]:
    """Возвращает (wallets, events, seen_set, last_poll)."""
    if not _available:
        return {}, [], set(), 0.0
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT address, data FROM bot_wallets")
                wallets = {row["address"]: row["data"] for row in cur.fetchall()}
                cur.execute("SELECT key, value FROM bot_wallet_meta")
                meta = {row["key"]: row["value"] for row in cur.fetchall()}

        events    = json.loads(meta.get("events", "[]"))
        seen      = set(json.loads(meta.get("seen", "[]")))
        last_poll = float(meta.get("last_poll", "0") or 0)
        return wallets, events, seen, last_poll
    except Exception as e:
        logger.warning(f"[DB] wallets_load error: {e}")
        return {}, [], set(), 0.0


def wallets_count() -> int:
    if not _available:
        return -1
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM bot_wallets")
                row = cur.fetchone()
                return row[0] if row else 0
    except Exception:
        return -1


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _encode(val) -> str:
    if val is None:
        return "null"
    if isinstance(val, (dict, list)):
        return _jdumps(val, ensure_ascii=False)
    return str(val)


def _decode(raw: str | None):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
