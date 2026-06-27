"""
Постоянное хранилище настроек дашборда.

Все параметры, которые пользователь меняет в дашборде (порог ликвидатора,
торговые настройки и т.д.), сохраняются в settings.json и применяются при
следующем запуске приложения. Так значения не сбрасываются на дефолтные
после перезапуска.

Файл хранится рядом с кодом (settings.json) и организован по секциям:
    {
      "config":     { "TRADE_AMOUNT": 1.0, "STOP_LOSS_PCT": 5.0, ... },
      "liquidator": { "sell_rise_pct": 50.0 }
    }
"""
import json
import os
import threading

_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(_DATA_DIR, exist_ok=True)
_SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(_DATA_DIR, "settings.json"))
_lock = threading.Lock()


def load_settings() -> dict:
    """Прочитать все настройки. При отсутствии/повреждении файла → пустой dict."""
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def get_section(section: str) -> dict:
    """Вернуть одну секцию настроек (или пустой dict)."""
    sec = load_settings().get(section, {})
    return sec if isinstance(sec, dict) else {}


def update_section(section: str, updates: dict) -> dict:
    """Слить updates в секцию и атомарно записать на диск. Возвращает секцию."""
    with _lock:
        data = load_settings()
        sec = data.get(section, {})
        if not isinstance(sec, dict):
            sec = {}
        sec.update(updates)
        data[section] = sec
        _write_atomic(data)
        return sec


def _write_atomic(data: dict):
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _SETTINGS_FILE)
