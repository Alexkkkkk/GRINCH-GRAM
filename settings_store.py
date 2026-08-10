"""
Постоянное хранилище настроек дашборда.

Первичное хранилище: PostgreSQL (db_store).
Резервное хранилище: settings.json (всегда пишется как локальный backup).

При отсутствии DB или ошибке — прозрачный fallback на JSON.
"""
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(_DATA_DIR, exist_ok=True)
_SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(_DATA_DIR, "settings.json"))
_lock = threading.Lock()
_migration_lock = threading.Lock()
_migration_done = False


def _db():
    try:
        import db_store
        return db_store if db_store.is_available() else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API — интерфейс не изменился (обратная совместимость)
# ═══════════════════════════════════════════════════════════════════════════════

def load_settings() -> dict:
    """Прочитать все настройки. Порядок: DB → JSON → пустой dict."""
    db = _db()
    if db:
        try:
            data = db.settings_get_all()
            if data:
                return data
        except Exception as e:
            logger.warning(f"[Settings] DB read error: {e}")
    return _load_json()


def get_section(section: str) -> dict:
    """Вернуть одну секцию настроек (или пустой dict)."""
    db = _db()
    if db:
        try:
            sec = db.settings_get_section(section)
            if sec:
                return sec
        except Exception as e:
            logger.warning(f"[Settings] DB get_section error: {e}")
    sec = _load_json().get(section, {})
    return sec if isinstance(sec, dict) else {}


def update_section(section: str, updates: dict) -> dict:
    """Слить updates в секцию, сохранить в DB + JSON. Возвращает секцию."""
    with _lock:
        # JSON — всегда (локальный backup)
        data = _load_json()
        sec = data.get(section, {})
        if not isinstance(sec, dict):
            sec = {}
        sec.update(updates)
        data[section] = sec
        _write_atomic(data)

        # DB — если доступна
        db = _db()
        if db:
            try:
                db.settings_update_section(section, updates)
            except Exception as e:
                logger.warning(f"[Settings] DB write error: {e}")

        return sec


# ─── Migration: JSON → DB при первом запуске с PostgreSQL ────────────────────
def migrate_to_db():
    """Если в DB нет настроек, но JSON существует — переносим однократно."""
    global _migration_done
    if _migration_done:
        return
    with _migration_lock:
        if _migration_done:
            return
        if _migrate_to_db_locked():
            _migration_done = True


def _migrate_to_db_locked():
    """Внутренняя миграция; вызывается под _migration_lock."""
    db = _db()
    if not db:
        return False
    try:
        existing = db.settings_get_all()
        if existing:
            return True
        data = _load_json()
        if not data:
            return True
        for section, updates in data.items():
            if isinstance(updates, dict) and updates:
                db.settings_update_section(section, updates)
        logger.info("[Settings] ✅ Настройки мигрированы JSON → PostgreSQL")
        return True
    except Exception as e:
        logger.warning(f"[Settings] migrate_to_db error: {e}")
        return False


# ─── JSON helpers ─────────────────────────────────────────────────────────────
def _load_json() -> dict:
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def _write_atomic(data: dict):
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _SETTINGS_FILE)


# ─── Запускаем миграцию при импорте ──────────────────────────────────────────
try:
    migrate_to_db()
except Exception:
    pass
