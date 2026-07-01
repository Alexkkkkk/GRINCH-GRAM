"""
db_backup.py — ежедневный бэкап всех таблиц pghost.ru → JSON-файлы.

• Запускается в фоновом потоке при старте приложения.
• Делает снапшот раз в 24 часа (или сразу при первом запуске).
• Хранит последние KEEP_DAYS бэкапов, старые удаляет автоматически.
• Если БД недоступна — молча пропускает, не ломает запуск.

Структура файлов:
  backups/
    2026-07-01_100000/
      bot_settings.json
      bot_trades.json
      bot_equity.json
      bot_open_trades.json
      bot_ai_state.json
      bot_wallets.json
      bot_wallet_meta.json
      _meta.json          ← время, количество строк
"""

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

BACKUP_DIR  = Path("backups")
KEEP_DAYS   = 7          # сколько бэкапов хранить
INTERVAL_S  = 24 * 3600  # раз в сутки

TABLES = [
    "bot_settings",
    "bot_trades",
    "bot_equity",
    "bot_open_trades",
    "bot_ai_state",
    "bot_wallets",
    "bot_wallet_meta",
]


# ── Утилиты ──────────────────────────────────────────────────────────────────

def _jdefault(o):
    """JSON-сериализатор для datetime и прочего."""
    if isinstance(o, datetime):
        return o.isoformat()
    try:
        import numpy as np
        if isinstance(o, np.integer):  return int(o)
        if isinstance(o, np.floating): return float(o)
        if isinstance(o, np.ndarray):  return o.tolist()
    except ImportError:
        pass
    return str(o)


def _dump_table(cur, table: str) -> list:
    """Читает всю таблицу и возвращает список dict'ов."""
    cur.execute(f"SELECT * FROM {table}")
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _rotate(keep: int):
    """Удаляет самые старые папки бэкапов, оставляя `keep` штук."""
    if not BACKUP_DIR.exists():
        return
    dirs = sorted(
        [d for d in BACKUP_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
    )
    for old in dirs[:-keep] if len(dirs) > keep else []:
        try:
            shutil.rmtree(old)
            log.info(f"[Backup] Удалён старый бэкап: {old.name}")
        except Exception as e:
            log.warning(f"[Backup] Не удалось удалить {old}: {e}")


# ── Основная функция бэкапа ───────────────────────────────────────────────────

def run_backup() -> bool:
    """Делает снапшот всех таблиц. Возвращает True при успехе."""
    try:
        import db_store
        if not db_store.is_available():
            log.warning("[Backup] PostgreSQL недоступен — бэкап пропущен")
            return False

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest = BACKUP_DIR / timestamp
        dest.mkdir(parents=True, exist_ok=True)

        meta = {"timestamp": timestamp, "tables": {}}
        conn = db_store._pool.getconn()
        try:
            with conn.cursor() as cur:
                for table in TABLES:
                    try:
                        rows = _dump_table(cur, table)
                        out = dest / f"{table}.json"
                        out.write_text(
                            json.dumps(rows, ensure_ascii=False,
                                       indent=2, default=_jdefault),
                            encoding="utf-8",
                        )
                        meta["tables"][table] = len(rows)
                        log.info(f"[Backup] {table}: {len(rows)} строк → {out.name}")
                    except Exception as e:
                        log.warning(f"[Backup] {table}: ошибка дампа — {e}")
                        meta["tables"][table] = f"ERROR: {e}"
        finally:
            db_store._pool.putconn(conn)

        (dest / "_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _rotate(KEEP_DAYS)
        log.info(f"[Backup] ✅ Бэкап завершён → backups/{timestamp}/")
        return True

    except Exception as e:
        log.error(f"[Backup] Критическая ошибка: {e}")
        return False


# ── Фоновый поток ─────────────────────────────────────────────────────────────

def _worker():
    """Бесконечный цикл: бэкап при старте, потом раз в 24 часа."""
    # Небольшая пауза чтобы дать БД подняться полностью
    time.sleep(30)
    while True:
        run_backup()
        next_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info(f"[Backup] Следующий бэкап через 24 часа (старт: {next_run})")
        time.sleep(INTERVAL_S)


_started = False
_lock    = threading.Lock()


def start():
    """Запускает фоновый поток бэкапа. Безопасно вызывать несколько раз."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    t = threading.Thread(target=_worker, name="db-backup", daemon=True)
    t.start()
    log.info("[Backup] 🗄️ Авто-бэкап БД запущен (каждые 24 ч → backups/)")
