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
try:
    os.makedirs(_DATA_DIR, exist_ok=True)   # M4 fix: не кидаем исключение на старте если нет прав
except OSError as _e:
    logging.getLogger(__name__).warning("[Settings] Cannot create DATA_DIR %s: %s", _DATA_DIR, _e)
_SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(_DATA_DIR, "settings.json"))
_lock = threading.Lock()


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
_migration_done = False
_migration_lock = threading.Lock()


def migrate_to_db():
    """Если в DB нет настроек, но JSON существует — переносим однократно.

    M11-fix: флаг + лок предотвращают двойную миграцию при одновременном
    старте нескольких воркеров (Gunicorn pre-fork).
    """
    global _migration_done
    if _migration_done:
        return
    with _migration_lock:
        if _migration_done:   # double-check под локом
            return
        db = _db()
        if not db:
            return
        try:
            existing = db.settings_get_all()
            if existing:
                return
            data = _load_json()
            if not data:
                return
            for section, updates in data.items():
                if isinstance(updates, dict) and updates:
                    db.settings_update_section(section, updates)
            logger.info("[Settings] ✅ Настройки мигрированы JSON → PostgreSQL")
        except Exception as e:
            logger.warning(f"[Settings] migrate_to_db error: {e}")
        finally:
            _migration_done = True


# ─── JSON helpers ─────────────────────────────────────────────────────────────
def _load_json() -> dict:
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def _write_atomic(data: dict):
    """Атомарная запись с fsync — защита от потери данных при сбое питания/перезапуска."""
    tmp = _SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())   # M3 fix: гарантируем запись на диск до rename
        os.replace(tmp, _SETTINGS_FILE)
    except Exception as e:
        logger.error("[Settings] atomic write failed: %s", e)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── Запускаем миграцию при импорте ──────────────────────────────────────────
try:
    migrate_to_db()
except Exception as _mig_e:
    logging.getLogger(__name__).warning("[Settings] migrate_to_db error at import: %s", _mig_e)  # L4 fix
