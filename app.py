import json
import math
import os
import gc
import resource
import logging
import subprocess

# ── Настройка логирования — как можно раньше, до любых импортов ──────────────
# Это гарантирует, что все log.info/warning/error из любого модуля видны
# в консоли Bothost (и Replit) даже если импорт падает на полпути.
logging.basicConfig(
    level=logging.DEBUG if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG" else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_startup_log = logging.getLogger("startup")
_startup_log.info("=== APP IMPORT START ===")

import numpy as np
_startup_log.info("numpy OK")
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from flask.json.provider import DefaultJSONProvider
from flask_socketio import SocketIO, emit
_startup_log.info("flask OK")
try:
    from flask_compress import Compress
except ImportError:
    Compress = None
try:
    import orjson
except ImportError:
    orjson = None
import threading
import time
_startup_log.info("stdlib OK")
from config import Config
_startup_log.info("config OK")


# ── Загружаем сохранённые настройки дашборда поверх env-дефолтов ─────────────
def _apply_saved_config():
    """При старте восстанавливаем все параметры Config, сохранённые через /api/config."""

    def _bool(v):
        if isinstance(v, bool): return v
        return str(v).lower() in ("true", "1", "yes")

    def _safe_set(attr, v, cast):
        """Безопасно конвертирует и применяет одну настройку — ошибка в одном поле не ломает остальные."""
        try:
            setattr(Config, attr, cast(v))
        except Exception as exc:
            _startup_log.warning(f"[Config] ⚠️ Пропущено {attr}={v!r}: {exc}")

    try:
        from settings_store import get_section, update_section
        saved = get_section("config")
        if not saved:
            _startup_log.info("[Config] Сохранённых настроек нет — сидируем дефолты в DB")
            try:
                _attrs = [
                    "SYMBOL", "TRADE_AMOUNT", "MAX_OPEN_TRADES", "TAKE_PROFIT_PCT",
                    "TRAILING_STOP_PCT", "MIN_AI_CONFIDENCE", "USE_DYNAMIC_TARGETS",
                    "TREND_FILTER", "SMART_BUY_ENABLED", "SMART_BUY_PULLBACK_PCT",
                    "SMART_BUY_MAX_WAIT_TICKS", "SMART_BUY_SKIP_CONF",
                    "SMART_TP_ENABLED", "SMART_TP_MIN_CONF", "SMART_TP_TIGHT_TRAIL_PCT",
                    "MIN_PROFIT_TON", "AI_TP_ADAPT_MIN_TRADES", "AI_TP_CAP_PCT",
                    "DCA_MODE", "DCA_STAKE_TON", "DCA_TARGET_PROFIT_PCT",
                    "DCA_DROP_TRIGGER_PCT", "DCA_PULLBACK_WAIT_PCT", "DCA_MAX_ENTRIES",
                    "DCA_CASCADE_ENABLED", "DCA_CASCADE_LEVEL1_PCT", "DCA_CASCADE_LEVEL2_PCT",
                    "DCA_SMART_REENTRY_ENABLED", "DCA_SMART_REENTRY_PULLBACK_PCT",
                    "DCA_SMART_REENTRY_MIN_AI_CONF", "DCA_COMPOUND_ENABLED",
                    "DCA_COMPOUND_RATIO", "DCA_COMPOUND_MAX_TON",
                    "DCA_ADAPTIVE_TRIGGER_ENABLED", "DCA_ADAPTIVE_FAST_MOVE_PCT",
                    "DCA_ADAPTIVE_FAST_DROP_PCT", "LARGE_SELL_DCA_ENABLED",
                    "LARGE_SELL_DCA_TON", "LARGE_SELL_MIN_TON", "LARGE_SELL_COOLDOWN_SEC",
                    "PROFIT_PROTECT_ENABLED", "PROFIT_PROTECT_TON",
                    "PROFIT_PROTECT_DROP_PCT", "PROFIT_PROTECT_AI_SELL",
                    "FEE_PCT", "DCA_REENTRY_COOLDOWN_SEC", "FAST_REENTRY_PULLBACK_PCT",
                    "SCALP_TARGET_NET_PCT", "ATR_TP_MULT", "ATR_SL_MULT",
                    "STOP_LOSS_PCT", "EV_THRESHOLD", "MIN_AI_CONFIDENCE",
                    "AI_OVERRIDE_CONFIDENCE", "AI_HARD_OVERRIDE_CONFIDENCE",
                    "AI_AUTONOMOUS_MIN_CONF", "AI_FULL_RIGHTS_MIN_CONF",
                    "PROFIT_PROTECT_ENABLED", "LOSS_COOLDOWN_SEC",
                    "DCA_AI_SELL_BLOCK_CONF", "CONFLUENCE_ENABLED",
                    "CONFLUENCE_RSI_MAX", "CONFLUENCE_VOL_MIN_RATIO",
                    "REVERSAL_AI_MIN", "SCALP_MIN_AI_CONF", "SCALP_TP_PCT",
                    "SHORT_MIN_AI_CONF", "SMART_MONEY_BLOCK",
                    "FAST_REENTRY_MIN_CONF", "DCA_REENTRY_COOLDOWN_SEC",
                    # Trail-параметры (ранее отсутствовали — не персистировались в DB)
                    "SCALP_MAX_ATR_PCT", "SHORT_TRAIL_PCT",
                    "TRAIL_STAGE2_AT", "TRAIL_STAGE2_PCT",
                    "TRAIL_STAGE3_AT", "TRAIL_STAGE3_PCT",
                    "TRAIL_STAGE4_AT", "TRAIL_STAGE4_PCT",
                ]
                defaults = {}
                for _a in _attrs:
                    _v = getattr(Config, _a, None)
                    if _v is not None:
                        defaults[_a] = _v
                # DEAD_HOURS_UTC — список, сохраняем как строку
                defaults["DEAD_HOURS_UTC"] = ",".join(str(h) for h in Config.DEAD_HOURS_UTC)
                defaults["DEAD_HOURS_DROP_MULT"] = Config.DEAD_HOURS_DROP_MULT
                if defaults:
                    update_section("config", defaults)
                    _startup_log.info(f"[Config] ✅ Сохранено {len(defaults)} дефолтных настроек в DB")
            except Exception as _seed_err:
                _startup_log.warning(f"[Config] ⚠️ Не удалось сидировать дефолты: {_seed_err}")
            return

        applied = 0
        for attr, cast in [
            ("SYMBOL",            str),
            ("TRADE_AMOUNT",      float),
            ("MAX_OPEN_TRADES",   lambda v: int(float(v))),
            ("TAKE_PROFIT_PCT",   float),
            ("TRAILING_STOP_PCT", float),
            ("MIN_AI_CONFIDENCE", float),
            ("USE_DYNAMIC_TARGETS", _bool),
            ("TREND_FILTER",      _bool),
            # Smart BUY
            ("SMART_BUY_ENABLED",        _bool),
            ("SMART_BUY_PULLBACK_PCT",   float),
            ("SMART_BUY_MAX_WAIT_TICKS", lambda v: int(float(v))),
            ("SMART_BUY_SKIP_CONF",      float),
            # Smart TP
            ("SMART_TP_ENABLED",         _bool),
            ("SMART_TP_MIN_CONF",        float),
            ("SMART_TP_TIGHT_TRAIL_PCT", float),
            # Авто-TP
            ("MIN_PROFIT_TON",          float),
            ("AI_TP_ADAPT_MIN_TRADES",  lambda v: int(float(v))),
            ("AI_TP_CAP_PCT",           float),
            # DCA стратегия
            ("DCA_MODE",             _bool),
            ("DCA_STAKE_TON",        float),
            ("DCA_TARGET_PROFIT_PCT",float),
            ("DCA_DROP_TRIGGER_PCT", float),
            ("DCA_PULLBACK_WAIT_PCT",float),
            ("DCA_MAX_ENTRIES",      lambda v: int(float(v))),
            ("DCA_CASCADE_ENABLED",  _bool),
            ("DCA_CASCADE_LEVEL1_PCT",float),
            ("DCA_CASCADE_LEVEL2_PCT",float),
            ("DCA_SMART_REENTRY_ENABLED",      _bool),
            ("DCA_SMART_REENTRY_PULLBACK_PCT", float),
            ("DCA_SMART_REENTRY_MIN_AI_CONF",  float),
            ("DCA_COMPOUND_ENABLED",  _bool),
            ("DCA_COMPOUND_RATIO",    float),
            ("DCA_COMPOUND_MAX_TON",  float),
            ("DCA_ADAPTIVE_TRIGGER_ENABLED", _bool),
            ("DCA_ADAPTIVE_FAST_MOVE_PCT",   float),
            ("DCA_ADAPTIVE_FAST_DROP_PCT",   float),
            # Детектор крупных продаж
            ("LARGE_SELL_DCA_ENABLED",  _bool),
            ("LARGE_SELL_DCA_TON",      float),
            ("LARGE_SELL_MIN_TON",      float),
            ("LARGE_SELL_COOLDOWN_SEC", lambda v: int(float(v))),
            # Защита прибыли
            ("PROFIT_PROTECT_ENABLED",  _bool),
            ("PROFIT_PROTECT_TON",      float),
            ("PROFIT_PROTECT_DROP_PCT", float),
            ("PROFIT_PROTECT_AI_SELL",  _bool),
            # FEE_PCT — особый: синхронизирует FEE_ROUND_TRIP
            ("FEE_PCT", float),
            # Параметры, которые хранятся в БД, но ранее не восстанавливались
            ("DCA_REENTRY_COOLDOWN_SEC",    lambda v: int(float(v))),
            ("FAST_REENTRY_PULLBACK_PCT",   float),
            ("SCALP_TARGET_NET_PCT",        float),
            # ATR-множители динамических целей
            ("ATR_TP_MULT",                 float),
            ("ATR_SL_MULT",                 float),
            # Trail-параметры (ранее отсутствовали в restore-loop)
            ("SCALP_MAX_ATR_PCT",  float),
            ("SHORT_TRAIL_PCT",    float),
            ("TRAIL_STAGE2_AT",    float),
            ("TRAIL_STAGE2_PCT",   float),
            ("TRAIL_STAGE3_AT",    float),
            ("TRAIL_STAGE3_PCT",   float),
            ("TRAIL_STAGE4_AT",    float),
            ("TRAIL_STAGE4_PCT",   float),
            # ALL-IN на дне
            ("ALLIN_ON_BOTTOM",    _bool),
            ("ALLIN_BOTTOM_CONF",  float),
            ("ALLIN_RSI_MAX",      float),
            ("ALLIN_MIN_FREE_TON", float),
        ]:
            if (v := saved.get(attr)) is not None:
                _safe_set(attr, v, cast)
                if attr == "FEE_PCT":
                    Config.FEE_ROUND_TRIP = Config.FEE_PCT * 2
                applied += 1

        # DEAD_HOURS_UTC — может быть строкой "0,22,23" или списком [0,22,23]
        if (raw_dh := saved.get("DEAD_HOURS_UTC")) is not None:
            try:
                if isinstance(raw_dh, list):
                    Config.DEAD_HOURS_UTC = [int(h) for h in raw_dh]
                else:
                    # строка вида "0,22,23" — убираем скобки на случай сериализации списка
                    _dh_str = str(raw_dh).strip().strip("[]")
                    Config.DEAD_HOURS_UTC = [
                        int(h.strip()) for h in _dh_str.split(",") if h.strip().lstrip("-").isdigit()
                    ]
                applied += 1
            except Exception as _dh_err:
                _startup_log.warning(f"[Config] ⚠️ Не удалось восстановить DEAD_HOURS_UTC: {_dh_err}")
        if (v := saved.get("DEAD_HOURS_DROP_MULT")) is not None:
            _safe_set("DEAD_HOURS_DROP_MULT", v, float)
            applied += 1

        _startup_log.info(f"[Config] ✅ Восстановлено {applied} сохранённых настроек из settings_store")
    except Exception as e:
        _startup_log.warning(f"[Config] ⚠️ Не удалось загрузить сохранённые настройки: {e}")
    finally:
        # Гарантия: все защиты «только в плюс» всегда включены при старте,
        # независимо от того, что лежит в DB (например, после тестового свопа
        # «вернуть настройки назад» мог сохранить их отключёнными).
        Config.ONLY_PROFIT_EXIT       = True   # никогда не продаём в убыток
        Config.PROFIT_PROTECT_ENABLED = True   # защита прибыли (откат от пика)
        Config.PROFIT_PROTECT_AI_SELL = True   # AI SELL тоже триггерит защиту

        # BUG-FIX гарантия: трейл-пороги не опускаются ниже ATR-откалиброванных минимумов.
        # Защищает от старых значений в DB. Логика max(): выше → сохраняем; ниже порога → зажимаем.
        # Калибровка 20.07.2026: ATR(15m)=2.225%, ATR(1h)хар.≈4-5%, памп до +50% за свечу.
        # Этапы расширены: Stage2=10% (2×ATR_1h), Stage3=7.5%, Stage4=6.0%
        Config.TRAIL_STAGE2_PCT         = max(Config.TRAIL_STAGE2_PCT,        10.0)
        Config.TRAIL_STAGE3_PCT         = max(Config.TRAIL_STAGE3_PCT,         7.5)
        Config.TRAIL_STAGE4_PCT         = max(Config.TRAIL_STAGE4_PCT,         6.0)
        # ATR(15m)=2.225% → SHORT_TRAIL ≥ 4×ATR=8.9% → 9%; SMART_TP_TIGHT ≥ 7%
        Config.SHORT_TRAIL_PCT          = max(Config.SHORT_TRAIL_PCT,          9.0)
        Config.SMART_TP_TIGHT_TRAIL_PCT = max(Config.SMART_TP_TIGHT_TRAIL_PCT, 7.0)
        # SCALP_MAX_ATR_PCT: ATR(15m)=2.225% < 3.0% → скальп уже включён без guard,
        # но держим 3.0 как минимум — запас на случай роста волатильности.
        Config.SCALP_MAX_ATR_PCT        = max(Config.SCALP_MAX_ATR_PCT,        3.0)


_apply_saved_config()
_startup_log.info("saved config applied")

from database import db
_startup_log.info("database OK")
from trader import Trader
_startup_log.info("trader OK")
from ton_tracker import TONTracker
_startup_log.info("ton_tracker OK")
from coin_info import coin_info
_startup_log.info("coin_info OK")
from price_feed import price_feed
_startup_log.info("price_feed OK")
from analytics_buffer import analytics_buffer
_startup_log.info("analytics_buffer OK — все модули загружены")

log = logging.getLogger(__name__)


def _numpy_default(o):
    if isinstance(o, (np.integer,)):  return int(o)
    if isinstance(o, (np.floating,)): return float(o)
    if isinstance(o, (np.bool_,)):    return bool(o)
    if isinstance(o, np.ndarray):     return o.tolist()
    if isinstance(o, set):            return list(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class NumpyJSONProvider(DefaultJSONProvider):
    """Сериализация через orjson (в разы быстрее stdlib json на C-уровне),
    с fallback на стандартный json, если orjson недоступен."""

    def dumps(self, obj, **kwargs):
        if orjson is not None:
            try:
                return orjson.dumps(
                    obj, default=_numpy_default, option=orjson.OPT_SERIALIZE_NUMPY
                ).decode("utf-8")
            except TypeError:
                pass
        return json.dumps(obj, default=_numpy_default, **kwargs)

    def loads(self, s, **kwargs):
        if orjson is not None:
            try:
                return orjson.loads(s)
            except Exception:
                pass
        return json.loads(s, **kwargs)


app = Flask(__name__)
app.json_provider_class = NumpyJSONProvider
app.json = NumpyJSONProvider(app)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600 if os.environ.get("FLASK_ENV") == "production" or not app.debug else 0


@app.after_request
def _add_static_cache_headers(resp):
    # Статика (JS/CSS/шрифты) — кэшируем на клиенте, чтобы не гонять её по сети
    # на каждый запрос страницы (браузер и так проверит по ETag при заходе).
    if request.path.startswith("/static/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=3600")
    return resp
if Compress is not None:
    app.config["COMPRESS_MIMETYPES"] = [
        "text/html", "text/css", "text/xml",
        "application/json", "application/javascript", "text/javascript",
    ]
    app.config["COMPRESS_LEVEL"] = 6
    app.config["COMPRESS_MIN_SIZE"] = 500
    Compress(app)
def _resolve_secret_key():
    """Надёжный ключ сессий: env → постоянный файл → случайный.
    Слабый зашитый ключ по умолчанию не используется (иначе cookie подделать).
    Файл хранится в DATA_DIR (на Bothost = /app/data, переживает рестарт контейнера).
    """
    import secrets as _secrets
    key = os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY")
    if key and key != "grinch-gram-secret-2024":
        return key
    # Используем DATA_DIR (персистентный на Bothost) вместо рабочей директории
    _data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    os.makedirs(_data_dir, exist_ok=True)
    path = os.path.join(_data_dir, ".session_secret")
    try:
        if os.path.exists(path):
            with open(path) as f:
                saved = f.read().strip()
            if saved:
                return saved
        generated = _secrets.token_hex(32)
        with open(path, "w") as f:
            f.write(generated)
        return generated
    except Exception:
        return _secrets.token_hex(32)


_startup_log.info("resolve_secret_key start")
_SECRET_KEY = _resolve_secret_key()
app.config["SECRET_KEY"] = _SECRET_KEY
app.secret_key = _SECRET_KEY
_startup_log.info("secret_key OK")

# ── База данных — приоритет у внешней БД (EXTERNAL_DATABASE_URL), иначе Replit PostgreSQL (DATABASE_URL) ───
# Если ни одна переменная не задана — мультипользовательские функции отключены,
# но бот продолжает работу (JSON-файлы + db_store.py как всегда).
_DATABASE_URL = os.environ.get("EXTERNAL_DATABASE_URL") or os.environ.get("DATABASE_URL")
_db_available = False
UserWallet = None  # заглушка; перезаписывается ниже если DB доступна

_startup_log.info("DB setup start — URL: %s", ("SET (" + _DATABASE_URL.split("@")[-1] + ")") if _DATABASE_URL else "NOT SET")
if _DATABASE_URL:
    try:
        app.config["SQLALCHEMY_DATABASE_URI"] = _DATABASE_URL
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_recycle": 300,   # пересоздаём соединения раз в 5 мин (pghost обрывает долгие idle)
            "pool_pre_ping": True, # проверяем соединение перед выдачей из пула
            "pool_timeout": 10,    # максимум 10с ждём свободного соединения из пула
            "max_overflow": 5,     # не более 5 соединений сверх pool_size
            "connect_args": {
                # Таймаут TCP-соединения к pghost.ru
                "connect_timeout": 10,
                # TCP keepalives — обнаруживают мёртвые соединения за ~60с
                # вместо ожидания ОС-таймаута (могут быть минуты)
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                # statement_timeout — убивает зависший SQL-запрос через 9с.
                # Синхронизировано с db_store.py чтобы ни один путь к pghost.ru
                # не мог подвесить процесс дольше, чем на один тик торгового цикла.
                "options": "-c statement_timeout=9000",
            },
        }
        _startup_log.info("db.init_app start")
        db.init_app(app)
        _startup_log.info("db.init_app OK — entering app context")
        with app.app_context():
            _startup_log.info("db.create_all start")
            from models import UserWallet   # noqa: F401
            db.create_all()
            _startup_log.info("db.create_all OK — running migrations")
            # Безопасная миграция — добавляем колонки если их нет (PostgreSQL)
            _new_cols = [
                ("virtual_ton_balance", "FLOAT DEFAULT 0"),
                ("virtual_grinch_held", "FLOAT DEFAULT 0"),
                ("entry_price_ton",     "FLOAT"),
                ("total_deposited",     "FLOAT DEFAULT 0"),
                ("total_withdrawn",     "FLOAT DEFAULT 0"),
                ("last_deposit_at",     "TIMESTAMP"),
                ("last_checked_lt",     "BIGINT DEFAULT 0"),
            ]
            from sqlalchemy import text
            for _col, _ctype in _new_cols:
                try:
                    db.session.execute(text(
                        f"ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS {_col} {_ctype}"
                    ))
                except Exception:
                    pass
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        _db_available = True
        log.info("[DB] SQLAlchemy подключена (%s)", _DATABASE_URL.split("@")[-1] if "@" in _DATABASE_URL else "ok")
        _startup_log.info("DB setup DONE ✅")
    except Exception as _db_err:
        log.warning("[DB] SQLAlchemy недоступна (%s) — мультипользовательские функции отключены", _db_err)
        _startup_log.warning("DB setup FAILED: %s", _db_err)
        _db_available = False
else:
    log.warning("[DB] DATABASE_URL / EXTERNAL_DATABASE_URL не заданы — мультипользовательские функции отключены")
    _startup_log.warning("DB setup SKIPPED — нет URL")

# ── SocketIO ──────────────────────────────────────────────────────────────────
_orig_dumps = json.dumps
def _safe_dumps(obj, **kw):
    if orjson is not None:
        try:
            return orjson.dumps(
                obj, default=_numpy_default, option=orjson.OPT_SERIALIZE_NUMPY
            ).decode("utf-8")
        except TypeError:
            pass
    kw.setdefault("default", _numpy_default)
    return _orig_dumps(obj, **kw)


def _safe_loads(s, **kw):
    if orjson is not None:
        try:
            return orjson.loads(s)
        except Exception:
            pass
    return json.loads(s, **kw)


_startup_log.info("SocketIO init start")
import flask_socketio
flask_socketio.json = type("_J", (), {
    "dumps": staticmethod(_safe_dumps),
    "loads": staticmethod(_safe_loads),
})()

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    allow_upgrades=True, ping_timeout=60, ping_interval=25,
                    json=type("_J", (), {
                        "dumps": staticmethod(_safe_dumps),
                        "loads": staticmethod(json.loads),
                    })())
_startup_log.info("SocketIO OK")

# ── Торговые движки ───────────────────────────────────────────────────────────
_startup_log.info("Trader() init start")
trader = Trader()
_startup_log.info("Trader() OK")
ton    = TONTracker(Config.TON_WALLET)
_startup_log.info("TONTracker OK")

from user_trader import UserTradingManager, encrypt_mnemonic, decrypt_mnemonic
user_mgr = UserTradingManager()
trader.signal_callbacks.append(user_mgr.on_signal)
_startup_log.info("UserTradingManager OK")

from grinch_liquidator import grinch_liquidator
import liquidity_guard
_startup_log.info("grinch_liquidator OK")

from deposit_monitor import DepositMonitor
deposit_monitor = DepositMonitor(Config.TON_WALLET)
_startup_log.info("DepositMonitor OK")

from wallet_tracker import WalletTracker
wallet_tracker = WalletTracker()
# Бот учится у реальных кошельков в пуле — отдаём трекер торговому движку
trader.wallet_tracker = wallet_tracker
_startup_log.info("WalletTracker OK")

from wallet_manager import wallet_manager as _wallet_mgr
_startup_log.info("WalletManager OK")

import data_hub   # запускает фоновый поток обновления при старте приложения
_startup_log.info("DataHub OK")


def _safe_status():
    raw = trader.get_status()
    # orjson сериализует numpy-типы нативно и в 5–10× быстрее обхода _walk.
    # Fallback на рекурсивный _walk только если orjson недоступен.
    if orjson is not None:
        try:
            return orjson.loads(
                orjson.dumps(raw, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS)
            )
        except Exception:
            pass
    def _walk(obj):
        if isinstance(obj, dict):          return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)): return [_walk(v) for v in obj]
        if isinstance(obj, np.integer):    return int(obj)
        if isinstance(obj, np.floating):   return float(obj)
        if isinstance(obj, np.bool_):      return bool(obj)
        if isinstance(obj, np.ndarray):    return obj.tolist()
        return obj
    return _walk(raw)


# ── Фоновые потоки ────────────────────────────────────────────────────────────

# ── Буфер обмена данными: общий «снимок» статуса ──────────────────────────────
# Фоновый поток считает статус один раз и кладёт его в этот буфер. И сокет-
# рассылка, и REST /api/status отдают ГОТОВЫЙ снимок мгновенно — запросы НИКОГДА
# не ждут сети/блокчейна (баланс и он-чейн цена считаются в фоне, а не в
# обработчике запроса). Это убирает подвисания и лишние повторные вычисления.
_status_snapshot = None
_snapshot_lock   = threading.Lock()

def _get_snapshot():
    """Последний готовый снимок статуса (или None, пока буфер не прогрет)."""
    with _snapshot_lock:
        return _status_snapshot

def _status_for_response():
    """Готовый снимок из буфера для любого ответа (страница, REST, сокет).
    Пока буфер холодный (самый первый запрос до первого тика фонового потока) —
    считаем напрямую один раз и сразу прогреваем буфер, чтобы параллельные
    запросы не пересчитывали то же самое."""
    global _status_snapshot
    snap = _get_snapshot()
    if snap is None:
        snap = _safe_status()
        with _snapshot_lock:
            if _status_snapshot is None:
                _status_snapshot = snap
    return snap

_connected_clients = 0
_connected_lock    = threading.Lock()


def _has_dashboard_clients() -> bool:
    with _connected_lock:
        return _connected_clients > 0


def push_updates():
    global _status_snapshot
    while True:
        try:
            # Никто не смотрит дашборд — не тратим CPU на сборку снапшота статуса.
            if _has_dashboard_clients():
                snap = _safe_status()
                with _snapshot_lock:
                    _status_snapshot = snap
                socketio.emit("status_update", snap)
        except Exception as e:
            print(f"[Push] Ошибка: {e}")
        time.sleep(2)


def push_price():
    from price_feed import price_feed
    last, last_symbol = None, None
    while True:
        try:
            if _has_dashboard_clients():
                symbol = Config.SYMBOL
                if symbol != last_symbol:
                    last = None
                    last_symbol = symbol
                price = float(trader.exchange.get_live_price())
                gram  = price_feed.get_grinch_ton_price()
                # Изменение считаем по курсу в GRAM (он же показан в hero)
                change = round((gram - last) / last * 100, 3) if (last and gram) else 0.0
                socketio.emit("price_update",
                              {"symbol": symbol, "price": price, "gram": gram, "change": change})
                if gram and gram > 0:
                    last = gram
        except Exception as e:
            print(f"[Price] Ошибка: {e}")
        time.sleep(2)


# ── Thread Supervisor ────────────────────────────────────────────────────────
# Реестр критических daemon-потоков, которые должны работать постоянно.
# Если поток умрёт (нормальный return или необработанное исключение),
# супервайзер автоматически перезапустит его через RESTART_DELAY секунд.
_supervised: dict[str, dict] = {}   # name → {"target": fn, "thread": Thread, "stop": Event}
_sup_lock    = threading.Lock()
_RESTART_DELAY = 5   # секунд между смертью потока и перезапуском


def _supervise(name: str, target, args=(), kwargs=None):
    """Запустить daemon-поток под наблюдением. При смерти — перезапуск.

    stop_event в _supervised[name]['stop'] позволяет остановить супервайзер
    мгновенно через stop_supervised(name): event.set() прерывает
    прерываемый restart-sleep и завершает _wrapper.
    """
    kwargs    = kwargs or {}
    stop_ev   = threading.Event()

    def _wrapper():
        while not stop_ev.is_set():
            try:
                target(*args, **kwargs)
            except Exception as e:
                print(f"[Supervisor] ⚠️ Поток '{name}' упал с ошибкой: {e}")
            else:
                if not stop_ev.is_set():
                    print(f"[Supervisor] ⚠️ Поток '{name}' завершился (неожиданно)")
            if stop_ev.is_set():
                break
            print(f"[Supervisor] 🔄 Перезапускаю '{name}' через {_RESTART_DELAY}с...")
            stop_ev.wait(timeout=_RESTART_DELAY)   # прерываемый restart-sleep

    t = threading.Thread(target=_wrapper, name=f"sup-{name}", daemon=True)
    t.start()
    with _sup_lock:
        _supervised[name] = {"target": target, "thread": t, "stop": stop_ev}
    return t


def stop_supervised(name: str):
    """Остановить конкретный супервайзированный поток мгновенно."""
    with _sup_lock:
        info = _supervised.get(name)
    if info:
        info["stop"].set()


def get_supervised_status() -> dict:
    """Возвращает dict name→alive для всех supervise'd потоков."""
    with _sup_lock:
        return {name: info["thread"].is_alive() for name, info in _supervised.items()}


def _load_users_bg():
    # Было 3с — было нужно «подождать пока Flask поднимется», но Flask уже
    # слушает к этому моменту. 0.5с достаточно для finalization init-цикла.
    time.sleep(0.5)
    user_mgr.load_from_db(app)
    deposit_monitor.start(app, user_mgr)


_bg_started = False
_bg_lock    = threading.Lock()

def push_training_progress(progress):
    """Вызывается AI-движком на каждом шаге обучения."""
    try:
        socketio.emit("training_progress", progress)
    except Exception:
        pass

def start_background():
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
        # Устанавливаем колбэк прогресса обучения
        trader.on_training_progress = push_training_progress
        # Авто-старт торговли (обучение → торговля)
        trader.start()
        # Критические UI-потоки под наблюдением супервайзера —
        # при падении перезапускаются автоматически через 5с.
        _supervise("push_updates", push_updates)
        _supervise("push_price",   push_price)
        threading.Thread(target=_load_users_bg, daemon=True).start()  # one-shot
        wallet_tracker.start()
        ton.start()
        import db_backup
        db_backup.start()
        # ── AI Советник: запуск фонового потока автономии ──────────────
        try:
            from ai_advisor import start_background as _adv_start
            _adv_start()
        except Exception as _adv_ex:
            print(f"[Advisor] не запущен: {_adv_ex}")
        # ── Алерты: монитор здоровья торгового цикла → Telegram ────────
        try:
            import alerts
            alerts.start_monitor()
            alerts.start_hourly_report(data_dir=os.getenv("DATA_DIR", "/app/data"))
        except Exception as _al_ex:
            print(f"[Alerts] монитор не запущен: {_al_ex}")
        # ── Кошелёк: полное отслеживание TON + GRINCH через БД ──────────
        try:
            _wallet_mgr.start(trader_ref=trader)
        except Exception as _wm_ex:
            print(f"[WalletManager] не запущен: {_wm_ex}")
        # ── Market Scanner: фоновый сканер паттернов ─────────────────────
        try:
            import ai_market_scanner as _mscanner
            def _get_candles_for_scanner():
                try:
                    ohlcv = trader.exchange.get_real_ohlcv(limit=60, currency="token",
                                                           token="base", tf="minute",
                                                           aggregate=15)
                    return ohlcv if ohlcv else trader.exchange.get_ohlcv(limit=60)
                except Exception:
                    return []
            _mscanner.start(_get_candles_for_scanner)
        except Exception as _sc_ex:
            print(f"[Scanner] не запущен: {_sc_ex}")

start_background()


# ════════════════════════════════════════════════════════════════════════════
#  Авторизация — логин / пароль для входа в панель
# ════════════════════════════════════════════════════════════════════════════
import hmac
from datetime import timedelta

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

if not (ADMIN_USERNAME and ADMIN_PASSWORD):
    _startup_log.critical(
        "⛔ ADMIN_USERNAME / ADMIN_PASSWORD не заданы! "
        "Все изменяющие API (POST) заблокированы до их установки."
    )

app.permanent_session_lifetime = timedelta(days=30)

# Публичные пути — доступны без входа (страницы участников платформы).
# Точные пути + узкие префиксы, чтобы случайно не открыть будущие эндпоинты.
_PUBLIC_EXACT = {
    "/login", "/logout", "/favicon.ico",
    "/tonconnect-manifest.json", "/join", "/api/platform/stats",
    "/health",           # health-check от Bothost/Docker без авторизации
    "/api/amm/preview",  # AMM live widget — виджет на дашборде без авторизации
    "/webhook/github",   # GitHub webhook — вызывается GitHub'ом, не пользователем
    "/api/ai-modules",   # статус AI-модулей — нужен до авторизации для дашборда
    "/api/performance",  # read-only торговая статистика — нужна виджетам без авторизации
}
_PUBLIC_PREFIXES = ("/static/", "/dashboard/", "/api/user/")


def _auth_configured():
    return bool(ADMIN_USERNAME and ADMIN_PASSWORD)


def _is_public_path(path):
    return path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES)


@app.before_request
def _ensure_csrf_token():
    """Генерируем CSRF-токен в сессии при первом запросе."""
    import secrets as _s
    if "_csrf" not in session:
        session["_csrf"] = _s.token_hex(32)


@app.before_request
def _require_login():
    # Если логин/пароль не заданы — блокируем state-changing запросы (POST/PUT/DELETE).
    # GET-запросы пропускаем, чтобы дашборд оставался доступным.
    if not _auth_configured():
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            path = request.path or "/"
            if not _is_public_path(path) and path != "/login":
                return jsonify({
                    "ok": False,
                    "error": "Панель не защищена паролем. Задайте ADMIN_USERNAME и ADMIN_PASSWORD."
                }), 403
        return None
    path = request.path or "/"
    if _is_public_path(path):
        return None
    if session.get("logged_in"):
        return None
    if path.startswith("/api") or path.startswith("/socket.io"):
        return jsonify({"ok": False, "error": "Требуется вход"}), 401
    return redirect(url_for("login", next=path))


@app.before_request
def _check_csrf():
    """Проверяем CSRF-токен для всех изменяющих запросов."""
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    path = request.path or "/"
    # Публичные пути и SocketIO — не проверяем
    if _is_public_path(path) or path.startswith("/socket.io") or path == "/login":
        return None
    token = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or ""
    )
    session_token = session.get("_csrf") or ""
    if not session_token or not hmac.compare_digest(token, session_token):
        if path.startswith("/api"):
            return jsonify({"ok": False, "error": "CSRF token invalid"}), 403
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(request.args.get("next") or url_for("index"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if (_auth_configured()
                and hmac.compare_digest(username, ADMIN_USERNAME)
                and hmac.compare_digest(password, ADMIN_PASSWORD)):
            session.permanent = True
            session["logged_in"] = True
            session["user"] = username
            return redirect(request.args.get("next") or url_for("index"))
        error = "Неверный логин или пароль"
    return render_template("login.html", error=error,
                           next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ════════════════════════════════════════════════════════════════════════════
#  TonConnect manifest
# ════════════════════════════════════════════════════════════════════════════

@app.route("/tonconnect-manifest.json")
def tonconnect_manifest():
    # TonConnect требует, чтобы манифест отдавался по HTTPS и поле url совпадало
    # с origin страницы (TonKeeper открывает манифест на телефоне пользователя).
    # Прокси Replit терминирует TLS и НЕ всегда проставляет X-Forwarded-Proto,
    # поэтому request.host_url может вернуть http:// — принудительно ставим https
    # для всех публичных хостов (кроме локальной разработки).
    host = request.host  # домен без схемы, с учётом ProxyFix x_host
    is_local = host.startswith("127.0.0.1") or host.startswith("localhost")
    scheme = "http" if is_local else "https"
    base = f"{scheme}://{host}"
    return jsonify({
        "url":     base,
        "name":    "GRINCH-GRAM",
        "iconUrl": f"{base}/static/img/grinch-icon.svg",
    })


@app.route("/health")
def health():
    """
    Реальная проверка живости, а не просто "процесс запущен":
    считаем сервис нездоровым, если торговый агент включён, но его
    фоновый цикл не тикал дольше 90с (тик раз в 15с + запас на сеть/блокчейн)
    или последний тик завершился с ошибкой.
    """
    # RSS всегда прикладываем к ответу — при следующем OOM на внешнем хостинге
    # (Bothost и т.п.) в его логах health-check будет видно, сколько памяти
    # процесс использовал прямо перед падением, а не только факт падения.
    try:
        rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        rss_mb = None

    if not trader.running:
        return jsonify({"status": "ok", "trader": "stopped", "rss_mb": rss_mb}), 200

    now = time.time()
    age = now - (trader.last_tick_ts or 0)
    if trader.last_tick_ts == 0:
        # Ещё идёт предобучение AI перед первым тиком — это ожидаемо, не ошибка
        return jsonify({"status": "ok", "trader": "starting", "rss_mb": rss_mb}), 200
    if age > 90:
        return jsonify({
            "status": "unhealthy",
            "reason": "trading loop stalled",
            "seconds_since_last_tick": round(age, 1),
            "rss_mb": rss_mb,
        }), 503
    if trader.last_tick_ok is False:
        return jsonify({
            "status": "degraded",
            "reason": "last tick raised an error (see logs)",
            "seconds_since_last_tick": round(age, 1),
            "rss_mb": rss_mb,
        }), 200

    # Проверяем критические потоки-супервайзеры
    sup = get_supervised_status()
    dead_threads = [n for n, alive in sup.items() if not alive]

    return jsonify({
        "status": "ok",
        "trader": "running",
        "seconds_since_last_tick": round(age, 1),
        "rss_mb": rss_mb,
        "threads": sup,
        **({"dead_threads": dead_threads} if dead_threads else {}),
    }), 200


@app.route("/api/admin/self_update", methods=["POST"])
def api_admin_self_update():
    """Само-обновление: скачивает актуальные .py-файлы с GitHub и делает
    graceful-reload gunicorn (SIGHUP мастеру). Требует авторизации.
    Тело запроса (JSON, необязательно):
      {"branch": "main"}  — ветка GitHub (по умолч. main)
    """
    import signal as _sig
    import urllib.request as _ur

    GITHUB_RAW = "https://raw.githubusercontent.com/Alexkkkkk/GRINCH-GRAM"
    UPDATE_FILES = [
        "ai_engine.py", "strategy.py", "experience_manager.py",
        "trader.py", "app.py", "config.py", "alerts.py",
        "analytics_buffer.py", "brain_fusion.py", "dedust_client.py",
        "deposit_monitor.py", "liquidity_guard.py", "price_feed.py",
        "settings_store.py", "user_trader.py", "wallet_manager.py",
        "wallet_tracker.py", "db_store.py", "http_client.py",
        "coin_info.py", "exchange.py",
        "templates/index.html", "templates/join.html", "templates/user_dash.html",
    ]

    branch = (request.json or {}).get("branch", "main") if request.is_json else "main"
    updated, skipped, errors = [], [], []

    base_dir = os.path.dirname(os.path.abspath(__file__))

    for fname in UPDATE_FILES:
        url = f"{GITHUB_RAW}/{branch}/{fname}"
        dest = os.path.join(base_dir, fname)
        try:
            with _ur.urlopen(url, timeout=10) as resp:
                if resp.status != 200:
                    skipped.append(fname)
                    continue
                content = resp.read()
            with open(dest, "wb") as fh:
                fh.write(content)
            updated.append(fname)
        except Exception as e:
            errors.append(f"{fname}: {e}")

    # Graceful reload gunicorn — SIGHUP мастеру перезапускает воркеры без downtime
    reload_ok = False
    try:
        import psutil as _ps
        me = _ps.Process(os.getpid())
        # Ищем мастер-процесс gunicorn (родитель текущего воркера)
        master = me.parent()
        if master and "gunicorn" in (master.name() or "").lower():
            os.kill(master.pid, _sig.SIGHUP)
            reload_ok = True
        else:
            # Возможно, мы и есть единственный процесс (dev-режим)
            for proc in _ps.process_iter(["pid", "name", "cmdline"]):
                cmd = " ".join(proc.info.get("cmdline") or [])
                if "gunicorn" in cmd and "main:app" in cmd:
                    os.kill(proc.pid, _sig.SIGHUP)
                    reload_ok = True
                    break
    except Exception as e:
        errors.append(f"reload: {e}")

    return jsonify({
        "ok": True,
        "branch": branch,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "reload": reload_ok,
        "note": "Воркеры перезапустятся через ~5с. Страница обновится автоматически." if reload_ok
                else "Файлы обновлены. Перезапустите контейнер вручную для применения.",
    })


@app.route("/api/admin/hard_restart", methods=["POST"])
def api_admin_hard_restart():
    """Настоящий перезапуск процесса (не graceful SIGHUP воркеров, которое
    при --preload не подхватывает новый код/шаблоны с диска). Отправляет
    SIGTERM мастеру gunicorn: супервизор/Docker с restart-политикой поднимет
    процесс заново, который импортирует все .py и шаблоны с чистого листа.
    Требует авторизации (тот же before_request, что и остальной /api/admin).
    """
    import signal as _sig
    try:
        import psutil as _ps
        me = _ps.Process(os.getpid())
        master = me.parent()
        target_pid = None
        if master and "gunicorn" in (master.name() or "").lower():
            target_pid = master.pid
        else:
            for proc in _ps.process_iter(["pid", "name", "cmdline"]):
                cmd = " ".join(proc.info.get("cmdline") or [])
                if "gunicorn" in cmd and ("main:app" in cmd or "app:app" in cmd):
                    target_pid = proc.pid
                    break
        if not target_pid:
            return jsonify({"ok": False, "message": "gunicorn master не найден"}), 500
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"ошибка поиска процесса: {e}"}), 500

    def _delayed_term():
        import time as _t
        _t.sleep(1.0)
        try:
            os.kill(target_pid, _sig.SIGTERM)
        except Exception:
            pass

    threading.Thread(target=_delayed_term, daemon=True).start()
    return jsonify({"ok": True, "pid": target_pid,
                    "note": "SIGTERM отправлен мастеру через 1с. Если у процесса есть restart-политика (Docker/supervisor), он поднимется заново с полностью свежим кодом."})


@app.route("/api/admin/fix_open_trades", methods=["POST"])
def api_admin_fix_open_trades():
    """Исправляет некорректные поля открытых позиций в памяти и БД
    (например, TP = 100× цены входа из-за старого бага с маленькой ставкой).
    """
    from config import Config as _Cfg
    fixed = []
    with trader._ot_lock:
        for t in (trader.open_trades or []):
            ep = float(t.get("entry_price") or 0)
            tp = float(t.get("take_profit") or 0)
            st = float(t.get("stake_ton") or 0)
            if ep > 0 and tp / ep > 10:
                mg = _Cfg.required_gross_pct_with_gas(st if st > 0 else None)
                tp_pct = max(_Cfg.TAKE_PROFIT_PCT, mg)
                new_tp = round(ep * (1 + tp_pct / 100), 8)
                t["take_profit"] = new_tp
                fixed.append({"id": str(t.get("id", ""))[:12],
                               "old_tp": tp, "new_tp": new_tp})
    if fixed:
        try:
            trader.exp.save_open_trades(trader._combined_open_trades())
        except Exception:
            pass
    return jsonify({"ok": True, "fixed": fixed, "count": len(fixed)})


_MEMORY_CACHE: dict = {"ts": 0.0, "payload": None}
_MEMORY_CACHE_TTL = 10  # секунд — gc.get_objects() дорогой вызов


@app.route("/api/memory")
def api_memory():
    """
    Диагностика потребления RAM по компонентам — чтобы при следующем
    инциденте (OOM на внешнем хостинге) сразу видеть, что именно раздуто:
    модели AI, буферы опыта, кэши цен/аналитики или количество потоков.
    Не требует psutil — используется resource.getrusage (встроен в Python).
    Кэшируется на 10 сек: gc.get_objects() дорог при частом polling-е.
    """
    _now = time.time()
    if _MEMORY_CACHE["payload"] and (_now - _MEMORY_CACHE["ts"]) < _MEMORY_CACHE_TTL:
        return jsonify({**_MEMORY_CACHE["payload"], "cached": True}), 200

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = round(rss_kb / 1024, 1)  # на Linux ru_maxrss уже в КБ

    ai = getattr(trader, "ai", None)
    ai_info = {}
    if ai is not None:
        try:
            slots = getattr(ai, "_slots", []) or []
            ai_info = {
                "models": [s.name for s in slots],
                "models_count": len(slots),
                "replay_buffer_size": len(getattr(ai, "_replay_X", []) or []),
                "confirmed_trades_buffer": len(getattr(ai, "_confirmed_X", []) or []),
                "retrains_since_start": getattr(ai, "_retrains", 0),
                "trained": bool(getattr(ai, "_trained", False)),
            }
        except Exception as e:
            ai_info = {"error": str(e)}

    analytics_info = {}
    try:
        analytics_info = {
            "ticks_stored": analytics_buffer.tick_count(),
            "trades_closed_stored": analytics_buffer.trade_count(),
        }
    except Exception as e:
        analytics_info = {"error": str(e)}

    cache_info = {}
    try:
        cache_info = {
            "price_feed_cache_entries": len(getattr(price_feed, "_cache", {}) or {}),
            "coin_info_market_cache":   len(getattr(coin_info, "_market_cache", {}) or {}),
            "coin_info_trades_cache":   len(getattr(coin_info, "_trades_cache", {}) or {}),
            "coin_info_pool_cache":     len(getattr(coin_info, "_pool_cache", {}) or {}),
            "coin_info_exch_cache":     len(getattr(coin_info, "_exch_cache", {}) or {}),
        }
    except Exception as e:
        cache_info = {"error": str(e)}

    gc_info = {
        "gc_objects_tracked": len(gc.get_objects()),
        "gc_generation_counts": gc.get_count(),
    }

    payload = {
        "process": {
            "rss_mb": rss_mb,
            "active_threads": threading.active_count(),
            "thread_names": [t.name for t in threading.enumerate()],
        },
        "ai_engine": ai_info,
        "analytics_buffer": analytics_info,
        "caches": cache_info,
        "gc": gc_info,
    }
    _MEMORY_CACHE["payload"] = payload
    _MEMORY_CACHE["ts"]      = time.time()
    return jsonify(payload), 200


# ─────────────────────────── Performance Stats ──────────────────────────────

@app.route("/api/performance")
def api_performance():
    """Расширенная торговая статистика: Sharpe, серия побед, daily P&L, circuit breaker.

    Рассчитывает упрощённый Sharpe Ratio по истории сделок сессии.
    Annualized = (mean_pnl / std_pnl) × √252 (252 торговых дня в году).
    """
    stats = getattr(trader, "stats", {}) or {}
    total = int(stats.get("total_trades",   0))
    wins  = int(stats.get("winning_trades", 0))
    pnl   = float(stats.get("total_pnl",    0.0))

    win_rate = round(wins / total * 100, 1) if total > 0 else 0.0

    # Sharpe Ratio из закрытых сделок (upрощённый, аннуализированный)
    sharpe_ratio = None
    try:
        import numpy as _np
        closed = [t for t in (trader.trades or []) if t.get("pnl") is not None]
        if len(closed) >= 5:
            pnls = _np.array([float(t["pnl"]) for t in closed], dtype=float)
            mean_p = float(_np.mean(pnls))
            std_p  = float(_np.std(pnls))
            if std_p > 0:
                sharpe_ratio = round(mean_p / std_p * (252 ** 0.5), 2)
    except Exception:
        pass

    # Max Drawdown из кумулятивного P&L по закрытым сделкам
    max_drawdown_ton = None
    try:
        closed = sorted(
            [t for t in (trader.trades or []) if t.get("pnl") is not None and t.get("closed_at")],
            key=lambda t: t.get("closed_at", "")
        )
        if len(closed) >= 2:
            import numpy as _np2
            pnls_arr  = _np2.array([float(t["pnl"]) for t in closed], dtype=float)
            cum_pnl   = _np2.cumsum(pnls_arr)
            running_max = _np2.maximum.accumulate(cum_pnl)
            drawdowns   = cum_pnl - running_max
            max_drawdown_ton = round(float(_np2.min(drawdowns)), 4)
    except Exception:
        pass

    # BrainFusion: точность источников и динамические веса
    source_accuracy = {}
    try:
        import brain_fusion as _bf_mod
        _b = _bf_mod.brain
        with _b._lock:
            source_accuracy = {
                "ai":  {"wins": _b._ai_wins,  "total": _b._ai_total,
                         "pct": round(_b._ai_wins / _b._ai_total * 100, 1)
                                if _b._ai_total > 0 else None},
                "ta":  {"wins": _b._ta_wins,  "total": _b._ta_total,
                         "pct": round(_b._ta_wins / _b._ta_total * 100, 1)
                                if _b._ta_total > 0 else None},
                "llm": {"wins": _b._adv_wins, "total": _b._adv_total,
                         "pct": round(_b._adv_wins / _b._adv_total * 100, 1)
                                if _b._adv_total > 0 else None},
            }
    except Exception:
        pass

    return jsonify({
        "ok":                 True,
        "total_trades":       total,
        "winning_trades":     wins,
        "win_rate_pct":       win_rate,
        "total_pnl_ton":      round(pnl, 4),
        "win_streak":         int(stats.get("win_streak",      0)),
        "max_win_streak":     int(stats.get("max_win_streak",  0)),
        "best_trade_ton":     round(float(stats.get("best_trade_ton",  0.0)), 4),
        "worst_trade_ton":    round(float(stats.get("worst_trade_ton", 0.0)), 4),
        "daily_pnl_ton":      round(float(stats.get("daily_pnl",       0.0)), 4),
        "circuit_breaker":    bool(stats.get("circuit_breaker_active", False)),
        "sharpe_ratio":       sharpe_ratio,
        "max_drawdown_ton":   max_drawdown_ton,
        "source_accuracy":    source_accuracy,
    }), 200


_GIT_LAST_UPDATE_CACHE = {"value": None}


def _git_last_update():
    """Дата и время последнего коммита из git-репозитория (последнее обновление с GitHub).

    Успешный результат кешируется навсегда (дата коммита не меняется без перезапуска).
    Неудача НЕ кешируется, чтобы временная недоступность git могла восстановиться
    на следующем запросе, а не залипнуть на "—" до перезапуска процесса.
    """
    if _GIT_LAST_UPDATE_CACHE["value"] is not None:
        return _GIT_LAST_UPDATE_CACHE["value"]
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            iso = out.stdout.strip()
            if iso:
                from datetime import datetime
                dt = datetime.fromisoformat(iso)
                result = dt.strftime("%d.%m.%Y %H:%M")
                _GIT_LAST_UPDATE_CACHE["value"] = result
                return result
    except Exception as e:
        log.debug(f"[GitInfo] не удалось получить дату последнего коммита: {e}")
    return "—"


# ════════════════════════════════════════════════════════════════════════════
#  Главный дашборд
# ════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    try:
        status       = _status_for_response()
        init_price   = status.get("analysis", {}).get("price", 0)
        init_gram    = status.get("grinch_ton", 0)
        init_running = status.get("running", False)
        init_ai      = status.get("ai", {})
        init_balance = status.get("balance", {})
    except Exception:
        init_price, init_gram, init_running, init_ai, init_balance = 0, 0, False, {}, {}
    return render_template("index.html", symbol=Config.SYMBOL, demo=Config.DEMO_MODE,
                           init_price=init_price, init_gram=init_gram, init_running=init_running,
                           init_ai=init_ai, init_balance=init_balance,
                           git_last_update=_git_last_update())


@app.route("/api/status")
def api_status():
    # Отдаём готовый снимок из буфера — мгновенно, без ожидания сети/блокчейна.
    # Пока буфер не прогрет (самый первый запрос) — считаем напрямую один раз.
    return jsonify(_status_for_response())

# ─────────────────────────── AMM Preview ────────────────────────────────────
_AMM_PREVIEW_CACHE = {"ts": 0.0, "payload": None}
_AMM_PREVIEW_TTL   = 12  # сек — TonAPI reserves запрос, не долбим чаще раза в 12с

@app.route("/api/amm/preview")
def api_amm_preview():
    """Реальная CPMM-оценка: сколько TON вернёт пул за весь GRINCH прямо сейчас.

    Кэшируется 12 секунд чтобы не насиловать TonAPI при частом polling-е.
    Возвращает:
      grinch_amount   — GRINCH на кошельке (из открытых позиций)
      expected_ton    — брутто-выход из пула (CPMM с price impact)
      net_ton         — нетто (expected_ton − sell_gas)
      min_net_ton     — минимум для безубыточного выхода (stake + buy_gas * entries)
      shortfall_ton   — дефицит (< 0) или запас (> 0)
      ok              — True если net_ton ≥ min_net_ton
      pool_ton_reserve, pool_grinch_reserve — резервы пула
      sell_gas        — Config.SELL_GAS_TON
      cached          — True если данные из кэша
    """
    now = time.time()
    if _AMM_PREVIEW_CACHE["payload"] and (now - _AMM_PREVIEW_CACHE["ts"]) < _AMM_PREVIEW_TTL:
        payload = dict(_AMM_PREVIEW_CACHE["payload"])
        payload["cached"] = True
        payload["cache_age"] = round(now - _AMM_PREVIEW_CACHE["ts"], 1)
        return jsonify(payload)

    try:
        from config import Config as _Cfg
        dc = trader.exchange._dedust  # DeDustClient instance

        # 1. GRINCH из открытых позиций (снимок под локом — защита от race с tick)
        with trader._ot_lock:
            open_trades = list(trader.open_trades) if trader.open_trades else []
        grinch_amount = sum(t.get("amount", 0) or 0 for t in open_trades)
        total_stake   = sum(t.get("stake_ton", 0) or 0 for t in open_trades)
        n_entries     = max(1, len(open_trades)) if open_trades else 1

        # Если нет открытых позиций — берём реальный баланс кошелька (для индикации)
        if grinch_amount <= 0:
            try:
                from wallet_manager import wallet_manager as _wm
                snap = _wm.get_snapshot() or {}
                grinch_amount = float(snap.get("grinch", 0) or 0)
            except Exception:
                pass

        # 2. Минимум для безубытка (только если есть открытые позиции)
        min_net_ton = (total_stake + _Cfg.BUY_GAS_TON * n_entries) if open_trades else 0.0
        sell_gas    = _Cfg.SELL_GAS_TON

        # 3. Резервы пула + CPMM
        pool_ton = pool_grinch = None
        expected_ton = None
        reserves = None
        if dc is not None:
            try:
                reserves = dc._pool_reserves()
            except Exception:
                reserves = None

        if reserves:
            pool_ton, pool_grinch = reserves
            if grinch_amount > 0:
                expected_ton = dc._cpmm_out(grinch_amount, pool_grinch, pool_ton)
        elif grinch_amount > 0:
            # Fallback: spot-цена из price_feed
            try:
                from price_feed import price_feed as _pf
                gtp = _pf.get_grinch_ton_price()
                if gtp and gtp > 0:
                    expected_ton = grinch_amount * gtp * (1 - _Cfg.FEE_PCT / 100)
            except Exception:
                pass

        net_ton      = round(expected_ton - sell_gas, 4) if expected_ton is not None else None
        shortfall    = round(net_ton - min_net_ton, 4)  if net_ton is not None else None
        amm_ok       = (shortfall >= 0) if shortfall is not None else None
        price_impact = None
        if expected_ton and pool_ton and pool_grinch and grinch_amount > 0:
            spot_price_ton = pool_ton / pool_grinch  # цена GRINCH в TON без impact
            ideal_out      = grinch_amount * spot_price_ton * (1 - _Cfg.FEE_PCT / 100)
            price_impact   = round((1 - expected_ton / ideal_out) * 100, 3) if ideal_out > 0 else None

        payload = {
            "grinch_amount":       round(grinch_amount, 2),
            "expected_ton":        round(expected_ton, 4)  if expected_ton  is not None else None,
            "net_ton":             net_ton,
            "min_net_ton":         round(min_net_ton, 4),
            "shortfall_ton":       shortfall,
            "ok":                  amm_ok,
            "price_impact_pct":    price_impact,
            "pool_ton_reserve":    round(pool_ton, 2)    if pool_ton    is not None else None,
            "pool_grinch_reserve": round(pool_grinch, 0) if pool_grinch is not None else None,
            "sell_gas":            sell_gas,
            "n_entries":           n_entries,
            "has_position":        len(open_trades) > 0,
            "cached":              False,
            "cache_age":           0,
            "ts":                  round(now),
        }
        _AMM_PREVIEW_CACHE["ts"]      = now
        _AMM_PREVIEW_CACHE["payload"] = payload
        return jsonify(payload)

    except Exception as e:
        return jsonify({"error": str(e), "ok": None, "cached": False}), 500

# ─────────────────────────── Living Organism ────────────────────────────────
@app.route("/api/market_hub")
def api_market_hub():
    """Агрегированные рыночные данные из 6 бесплатных источников."""
    try:
        from data_hub import get_snapshot, get_source_status
        snap   = get_snapshot()
        status = get_source_status()
        return jsonify({"ok": True, "data": snap, "sources": status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/organism")
def api_organism():
    """Состояние живого организма QuantumBrain (7 биосистем)."""
    try:
        from organism import organism as _org
        return jsonify(_org.get_state())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/ai-modules")
def api_ai_modules():
    """Статус всех AI-модулей: entry optimizer, TP optimizer, market scanner."""
    result = {}
    try:
        import ai_entry_optimizer as _eo
        result["entry_optimizer"] = _eo.get_status()
    except Exception as e:
        result["entry_optimizer"] = {"error": str(e)}
    try:
        import ai_tp_optimizer as _to
        result["tp_optimizer"] = _to.get_status()
    except Exception as e:
        result["tp_optimizer"] = {"error": str(e)}
    try:
        import ai_market_scanner as _sc
        result["market_scanner"] = _sc.get_status()
        sig = _sc.get_last_signal()
        result["market_scanner"]["active_signal"] = sig
    except Exception as e:
        result["market_scanner"] = {"error": str(e)}
    return jsonify(result)

_CANDLES_CACHE = {"ts": 0.0, "payload": None}
_CANDLES_CACHE_TTL = 30  # сек — свечи 15м, пересчёт чаще 30с бессмысленен

@app.route("/api/candles")
def api_candles():
    now = time.time()
    cached = _CANDLES_CACHE["payload"]
    if cached is not None and (now - _CANDLES_CACHE["ts"]) < _CANDLES_CACHE_TTL:
        return jsonify(cached)

    from strategy import analyze
    # Реальные свечи пары GRINCH/GRAM (цена GRINCH в GRAM/Toncoin) с GeckoTerminal.
    # 15-минутный таймфрейм — как на DeDust.
    ohlcv = trader.exchange.get_real_ohlcv(limit=100, currency="token", token="base",
                                           tf="minute", aggregate=15)
    if not ohlcv:
        ohlcv = trader.exchange.get_ohlcv(limit=100)
    analysis = analyze(ohlcv)
    def _walk(obj):
        if isinstance(obj, dict):          return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)): return [_walk(v) for v in obj]
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)):return float(obj)
        if isinstance(obj, (np.bool_,)):   return bool(obj)
        if isinstance(obj, np.ndarray):    return obj.tolist()
        return obj
    payload = {
        "candles": _walk(analysis.get("candles", [])),
        "price":   _walk(analysis.get("price", 0)),
    }
    _CANDLES_CACHE["ts"] = now
    _CANDLES_CACHE["payload"] = payload
    return jsonify(payload)

@app.route("/api/start", methods=["POST"])
def api_start():
    trader.start()
    return jsonify({"ok": True, "message": "Агент запущен"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    trader.stop()
    return jsonify({"ok": True, "message": "Агент остановлен"})

@app.route("/api/trading/enable", methods=["POST"])
def api_trading_enable():
    trader.enable_trading()
    return jsonify({"ok": True, "trading_enabled": True})

@app.route("/api/trading/disable", methods=["POST"])
def api_trading_disable():
    trader.disable_trading()
    return jsonify({"ok": True, "trading_enabled": False})

@app.route("/api/dca/reset_pullback", methods=["POST"])
def api_dca_reset_pullback():
    """Сбросить ожидание отката DCA — бот сразу начнёт искать вход."""
    trader.dca_wait_pullback = False
    trader.dca_peak_price    = 0.0
    trader._save_volatile_state()
    return jsonify({"ok": True, "dca_wait_pullback": False, "dca_peak_price": 0.0})

@app.route("/api/trade/delete", methods=["POST"])
def api_trade_delete():
    data = request.get_json(silent=True) or {}
    tid = data.get("id")
    if tid is None or tid == "":
        return jsonify({"ok": False, "error": "не указан id позиции"}), 400
    result = trader.delete_trade(tid)
    return jsonify(result), (200 if result.get("ok") else 400)

@app.route("/api/trade/close", methods=["POST"])
def api_trade_close():
    data = request.get_json(silent=True) or {}
    tid = data.get("id")
    if tid is None or tid == "":
        return jsonify({"ok": False, "error": "не указан id позиции"}), 400
    # force=true — принудительное закрытие тестовой/ошибочной позиции;
    # обходит ONLY_PROFIT_EXIT, но исполняет реальный своп на блокчейне.
    force = bool(data.get("force", False))
    result = trader.close_trade(tid, force=force)
    return jsonify(result), (200 if result.get("ok") else 400)

@app.route("/api/ai/decisions")
def api_ai_decisions():
    log = getattr(trader, "decision_log", [])
    return jsonify(list(reversed(log))[:15])


@app.route("/api/filters/status")
def api_filters_status():
    """Статус защитных фильтров: loss cooldown, Confluence, DCA AI-guard."""
    import time as _t
    from config import Config

    # ── Loss cooldown ─────────────────────────────────────────────────────────
    last_loss_ts = getattr(trader, "_last_loss_ts", 0.0) or 0.0
    cd_total     = Config.LOSS_COOLDOWN_SEC
    cd_left      = max(0.0, cd_total - (_t.time() - last_loss_ts)) if last_loss_ts > 0 else 0.0
    cooldown_active = cd_left > 0

    # ── Последний тик: Confluence + текущий RSI / vol_ratio ──────────────────
    last_dec = (getattr(trader, "decision_log", []) or [{}])[-1] if getattr(trader, "decision_log", []) else {}
    rsi_now     = last_dec.get("rsi", None)
    # vol_ratio не сохраняется в decision_log — берём из last_ai
    last_ai  = getattr(trader, "last_ai", {}) or {}
    vol_ratio   = last_ai.get("vol_ratio", None)

    # ── Последние заблокированные сигналы ───────────────────────────────────
    log = list(reversed(getattr(trader, "decision_log", [])))
    blocked_recent = []
    for d in log[:25]:
        if d.get("blocked") and d.get("result") == "HOLD":
            blocked_recent.append({
                "t":      d.get("t", "—"),
                "reason": d.get("reason", ""),
                "conf":   d.get("conf", 0),
                "rsi":    d.get("rsi"),
                "regime": d.get("regime", ""),
            })
        if len(blocked_recent) >= 8:
            break

    # ── DCA AI-guard: проверяем текущее состояние ────────────────────────────
    ai_signal   = last_ai.get("ai_signal", "HOLD")
    ai_conf     = float(last_ai.get("confidence", 0) or 0)
    dca_guard_active = (
        ai_signal == "SELL"
        and ai_conf >= Config.DCA_AI_SELL_BLOCK_CONF
    )

    return jsonify({
        "cooldown": {
            "active":    cooldown_active,
            "seconds_left": int(cd_left),
            "total_sec": cd_total,
            "pct":       round((1 - cd_left / cd_total) * 100, 1) if cd_total > 0 else 100,
        },
        "confluence": {
            "enabled":       Config.CONFLUENCE_ENABLED,
            "rsi_now":       round(rsi_now, 1) if rsi_now is not None else None,
            "rsi_max":       Config.CONFLUENCE_RSI_MAX,
            "rsi_ok":        (rsi_now < Config.CONFLUENCE_RSI_MAX) if rsi_now is not None else None,
            "vol_ratio_now": round(vol_ratio, 2) if vol_ratio is not None else None,
            "vol_min":       Config.CONFLUENCE_VOL_MIN_RATIO,
            "vol_ok":        (vol_ratio >= Config.CONFLUENCE_VOL_MIN_RATIO) if vol_ratio is not None else None,
        },
        "dca_guard": {
            "active":    dca_guard_active,
            "ai_signal": ai_signal,
            "ai_conf":   round(ai_conf, 1),
            "threshold": Config.DCA_AI_SELL_BLOCK_CONF,
        },
        "blocked_recent": blocked_recent,
    })


@app.route("/api/ai/history")
def api_ai_history():
    """История обучения ИИ: накопление примеров в БД + статус тяжёлых моделей."""
    import db_store
    from datetime import datetime, timedelta

    # ── Всего примеров в БД ──────────────────────────────────────────────────
    total_db = db_store.ai_examples_count()

    # ── По дням: последние 30 дней (группировка в Python, не SQL) ───────────
    recent = db_store.ai_examples_get_recent(limit=5000)
    day_counts: dict = {}
    for ex in recent:
        # Примеры не хранят дату — берём только count total/batch; для графика
        # используем порядковые блоки по 10 примеров с накоплением
        pass  # handled below via cumulative

    # Кумулятивный график: каждые N примеров → одна точка (до 60 точек)
    N_POINTS = min(60, max(1, total_db // 5 or 1))
    step = max(1, total_db // N_POINTS)
    cumulative = []
    for i in range(0, total_db, step):
        cumulative.append({"x": i + step, "y": min(i + step, total_db)})
    if not cumulative:
        cumulative = [{"x": 0, "y": 0}]

    # ── Тяжёлые модели из БД ────────────────────────────────────────────────
    deep_meta = db_store.deep_models_meta()
    for m in deep_meta:
        if m.get("trained_at"):
            m["trained_at"] = m["trained_at"].strftime("%d.%m.%Y %H:%M")

    # ── Живой ансамбль ───────────────────────────────────────────────────────
    ai = getattr(trader, "ai", None)
    live_slots = []
    confirmed_buf = 0
    retrains = 0
    trained = False
    if ai is not None:
        try:
            slots = getattr(ai, "_slots", []) or []
            live_slots = [
                {"name": s.name,
                 "accuracy": round(s.accuracy * 100, 1),
                 "weight": round(getattr(s, "weight", 1.0), 3)}
                for s in slots
            ]
            confirmed_buf = len(getattr(ai, "_confirmed_X", []) or [])
            retrains      = getattr(ai, "_retrains", 0)
            trained       = bool(getattr(ai, "_trained", False))
        except Exception:
            pass

    return jsonify({
        "total_db":      total_db,
        "confirmed_buf": confirmed_buf,
        "retrains":      retrains,
        "trained":       trained,
        "cumulative":    cumulative,
        "deep_models":   deep_meta,
        "live_slots":    live_slots,
    })

# ─── Ручной запуск deep-retrain ─────────────────────────────────────────────
import threading as _threading

_deep_retrain_manual_lock = _threading.Lock()
_deep_retrain_manual_state = {"running": False, "last": None, "error": None}


@app.route("/api/ai/deep-retrain", methods=["POST"])
def api_ai_deep_retrain():
    """Запускает глубокое переобучение ИИ вручную (не ждём 2 дня).
    Не блокирует ответ — работает в фоновом потоке. Повторный вызов
    во время активного запуска возвращает статус already_running."""
    global _deep_retrain_manual_state
    with _deep_retrain_manual_lock:
        if _deep_retrain_manual_state["running"]:
            return jsonify({"ok": False, "status": "already_running",
                            "msg": "Переобучение уже идёт"})
        _deep_retrain_manual_state["running"] = True
        _deep_retrain_manual_state["error"]   = None

    def _run():
        global _deep_retrain_manual_state
        try:
            # 1) Лёгкие модели в оперативной памяти
            ai = getattr(trader, "ai", None)
            if ai is not None:
                ai.deep_retrain_from_db(window=2000)
            # 2) Тяжёлые модели в изолированном процессе
            if hasattr(trader, "_run_deep_model_subprocess"):
                trader._run_deep_model_subprocess()
            _deep_retrain_manual_state["last"]    = \
                __import__("datetime").datetime.now().strftime("%d.%m.%Y %H:%M")
            _deep_retrain_manual_state["error"]   = None
        except Exception as exc:
            _deep_retrain_manual_state["error"] = str(exc)
        finally:
            _deep_retrain_manual_state["running"] = False

    _threading.Thread(target=_run, daemon=True, name="manual-deep-retrain").start()
    return jsonify({"ok": True, "status": "started",
                    "msg": "Глубокое переобучение запущено в фоне"})


@app.route("/api/ai/deep-retrain/status")
def api_ai_deep_retrain_status():
    """Возвращает текущий статус ручного deep-retrain."""
    s = _deep_retrain_manual_state
    return jsonify({
        "running": s["running"],
        "last":    s["last"],
        "error":   s["error"],
    })


# ─── Экспорт обучающих примеров в CSV ───────────────────────────────────────
@app.route("/api/ai/examples/export.csv")
def api_ai_examples_export():
    """Потоковая отдача всех обучающих примеров из bot_ai_examples в CSV.
    Использует серверный курсор — не тянет всё в RAM сразу."""
    import csv
    import io
    import db_store

    # Имена признаков из живого AI (если доступны)
    ai = getattr(trader, "ai", None)
    feat_names = (list(getattr(ai, "_feature_names", [])) or []) if ai else []

    def _generate():
        first = True
        header_written = False
        for row in db_store.ai_examples_export_all():
            feats = row["features"] or []
            if first:
                # Строим заголовок
                if feat_names:
                    cols = feat_names[:len(feats)]
                    # если признаков в строке больше, чем в именах — добавляем f_N
                    cols += [f"feat_{i}" for i in range(len(cols), len(feats))]
                else:
                    cols = [f"feat_{i}" for i in range(len(feats))]
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["id", "created_at", "label", "weight"] + cols)
                yield buf.getvalue()
                header_written = True
                first = False
            buf = io.StringIO()
            w = csv.writer(buf)
            ts = row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row["created_at"] else ""
            w.writerow([row["id"], ts, row["label"], round(row["weight"], 6)] + list(feats))
            yield buf.getvalue()
        if not header_written:
            yield "id,created_at,label,weight\n"
            yield "# Примеров пока нет\n"

    total = db_store.ai_examples_count()
    filename = f"ai_examples_{total}.csv"
    return app.response_class(
        _generate(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Accel-Buffering": "no",
        },
    )


# ─── AI Советник (Groq LLaMA) ───────────────────────────────────────────────
@app.route("/api/advisor/status")
def api_advisor_status():
    from ai_advisor import get_status
    return jsonify(get_status())

@app.route("/api/advisor/run", methods=["POST"])
def api_advisor_run():
    from ai_advisor import run_advisor, reload_key
    reload_key()
    data        = request.json or {}
    auto_apply  = bool(data.get("auto_apply", False))
    user_msg    = str(data.get("message", ""))[:500]
    result = run_advisor(auto_apply=auto_apply, user_message=user_msg)
    return jsonify(result)

@app.route("/api/advisor/toggle_auto", methods=["POST"])
def api_advisor_toggle_auto():
    from ai_advisor import toggle_auto_apply
    state = toggle_auto_apply()
    return jsonify({"auto_apply": state})

@app.route("/api/advisor/config", methods=["GET", "POST"])
def api_advisor_config():
    if request.method == "POST":
        err = _require_login()
        if err:
            return err
    from ai_advisor import set_config, AUTO_INTERVAL_MIN, AUTO_TRADES_TRIGGER
    if request.method == "POST":
        data = request.json or {}
        result = set_config(
            interval_min   = data.get("interval_min"),
            trades_trigger = data.get("trades_trigger"),
        )
        return jsonify({"ok": True, **result})
    return jsonify({"ok": True, "interval_min": AUTO_INTERVAL_MIN, "trades_trigger": AUTO_TRADES_TRIGGER})

@app.route("/api/advisor/log")
def api_advisor_log():
    from ai_advisor import get_adaptation_log
    return jsonify(get_adaptation_log())

@app.route("/api/advisor/apikey", methods=["POST"])
def api_advisor_apikey():
    """Обратная совместимость: сохраняет ключ Groq (старый эндпоинт)."""
    err = _require_login()
    if err:
        return err
    from ai_advisor import reload_key
    data = request.json or {}
    key  = str(data.get("key", "")).strip()
    if not key:
        return jsonify({"ok": False, "error": "Ключ не может быть пустым"})
    reload_key(key, provider="groq")
    return jsonify({"ok": True, "enabled": True})

@app.route("/api/advisor/apikey", methods=["GET"])
def api_advisor_apikey_get():
    from ai_advisor import _read_key_file
    stored = _read_key_file()
    masked = ("gsk_" + "•" * 20 + stored[-4:]) if len(stored) > 8 else ("•" * len(stored) if stored else "")
    return jsonify({"ok": True, "has_key": bool(stored), "masked": masked})


# ── Мульти-провайдер API ────────────────────────────────────────────────────

@app.route("/api/advisor/providers", methods=["GET"])
def api_advisor_providers():
    """Список всех AI-провайдеров и их статус (есть ключ / активен)."""
    from ai_advisor import get_providers
    return jsonify(get_providers())


@app.route("/api/advisor/providers/<provider_id>/key", methods=["POST"])
def api_advisor_provider_key(provider_id):
    """Сохранить API-ключ для указанного провайдера."""
    from ai_advisor import reload_key, PROVIDER_CONFIGS
    if provider_id not in PROVIDER_CONFIGS:
        return jsonify({"ok": False, "error": f"Неизвестный провайдер: {provider_id}"}), 400
    data = request.json or {}
    key  = str(data.get("key", "")).strip()
    if not key:
        return jsonify({"ok": False, "error": "Ключ не может быть пустым"}), 400
    reload_key(key, provider=provider_id)
    return jsonify({"ok": True, "provider": PROVIDER_CONFIGS[provider_id]["name"]})


@app.route("/api/advisor/providers/<provider_id>/key", methods=["GET"])
def api_advisor_provider_key_get(provider_id):
    """Проверить наличие ключа у провайдера (без раскрытия значения)."""
    from ai_advisor import PROVIDER_CONFIGS, _read_provider_key
    if provider_id not in PROVIDER_CONFIGS:
        return jsonify({"ok": False, "error": "Неизвестный провайдер"}), 400
    stored = _read_provider_key(provider_id)
    masked = ("•" * 8 + stored[-4:]) if len(stored) > 8 else ("•" * len(stored) if stored else "")
    return jsonify({"ok": True, "has_key": bool(stored), "masked": masked,
                    "provider": PROVIDER_CONFIGS[provider_id]["name"]})


@app.route("/api/advisor/providers/select", methods=["POST"])
def api_advisor_provider_select():
    """Выбрать предпочтительный AI-провайдер (или 'auto' для авто-выбора)."""
    from ai_advisor import set_provider
    data = request.json or {}
    pid  = str(data.get("provider_id", "")).strip()
    if pid.lower() in ("auto", ""):
        pid = None
    result = set_provider(pid)
    return jsonify(result), (200 if result.get("ok") else 400)

@app.route("/api/alerts/config", methods=["POST"])
def api_alerts_config():
    import settings_store
    data     = request.json or {}
    token    = str(data.get("bot_token", "")).strip()
    chat_id  = str(data.get("chat_id", "")).strip()
    enabled  = bool(data.get("enabled", True))
    updates = {"enabled": enabled}
    if token:
        updates["telegram_bot_token"] = token
    if chat_id:
        updates["telegram_chat_id"] = chat_id
    settings_store.update_section("alerts", updates)
    return jsonify({"ok": True})

@app.route("/api/alerts/config", methods=["GET"])
def api_alerts_config_get():
    import settings_store
    sec = settings_store.get_section("alerts")
    token = sec.get("telegram_bot_token", "")
    return jsonify({
        "ok": True,
        "has_token": bool(token),
        "masked_token": ("•" * 20 + token[-4:]) if len(token) > 4 else "",
        "chat_id": sec.get("telegram_chat_id", ""),
        "enabled": bool(sec.get("enabled", True)),
    })

@app.route("/webhook/github", methods=["POST"])
def webhook_github():
    """GitHub Webhook — мгновенный триггер деплоя при push в main.
    Настрой в GitHub: Settings → Webhooks → Payload URL = http://ВАШ_IP/webhook/github
    """
    import hmac, hashlib, subprocess, threading

    # Проверяем подпись (опционально, если задан WEBHOOK_SECRET)
    secret = os.getenv("WEBHOOK_SECRET", "").encode()
    if secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret, request.data, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("[Webhook] ❌ Неверная подпись GitHub")
            return jsonify({"ok": False, "error": "invalid signature"}), 401

    data = request.get_json(silent=True) or {}
    ref  = data.get("ref", "")
    pusher = data.get("pusher", {}).get("name", "?")

    # Реагируем только на push в main/master
    if ref not in ("refs/heads/main", "refs/heads/master"):
        return jsonify({"ok": True, "skip": True, "reason": f"ref={ref} не main"})

    commit_msg = data.get("head_commit", {}).get("message", "?")[:80]
    log.info(f"[Webhook] 🚀 GitHub push от {pusher}: {commit_msg}")

    # Запускаем деплой в фоне — не блокируем ответ GitHub
    # Внимание: deploy.sh запускается на ХОСТЕ через /opt/bot/deploy.sh,
    # но внутри контейнера docker-команды недоступны.
    # Поэтому пишем trigger-файл → хост-демон его читает и запускает деплой.
    def _run_deploy():
        try:
            # Пишем trigger-файл в /app/data (примонтированный volume хоста)
            trigger_path = "/app/data/.deploy_trigger"
            with open(trigger_path, "w") as f:
                f.write(f"{commit_msg}\n")
            log.info(f"[Webhook] ✅ Trigger записан → {trigger_path}")
        except Exception as e:
            log.error(f"[Webhook] ошибка записи trigger: {e}")

    threading.Thread(target=_run_deploy, daemon=True, name="github-deploy").start()
    return jsonify({"ok": True, "queued": True, "commit": commit_msg})


@app.route("/api/alerts/test", methods=["POST"])
def api_alerts_test():
    import alerts
    result = alerts.send_alert("🔔 QuantumBrain: тестовое уведомление. Если вы это видите — Telegram-алерты настроены верно.")
    return jsonify(result), (200 if result.get("ok") else 400)

@app.route("/api/trade/manual_buy", methods=["POST"])
def api_manual_buy():
    data   = request.get_json(silent=True) or {}
    amount = float(data.get("amount", 0)) or None
    result = trader.force_buy(amount_ton=amount)
    return jsonify(result), (200 if result.get("ok") else 400)

@app.route("/api/trade/manual_sell_all", methods=["POST"])
def api_manual_sell_all():
    result = trader.force_sell_all()
    return jsonify(result), (200 if result.get("ok") else 400)

# ════════════════════════════════════════════════════════════════════════════
#  Кошелёк — полное отслеживание TON + GRINCH через БД
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/wallet/full")
def api_wallet_full():
    """Полный статус кошелька: баланс TON + GRINCH, цены, P&L, потенциал, история."""
    try:
        status = _wallet_mgr.get_full_status()
        return jsonify({"ok": True, **status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/wallet/snapshot")
def api_wallet_snapshot():
    """Последний снимок кошелька (быстро, без истории)."""
    try:
        snap = _wallet_mgr.get_snapshot()
        return jsonify({"ok": True, "snapshot": snap})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/wallet/history")
def api_wallet_history():
    """История снимков кошелька из PostgreSQL."""
    try:
        limit = min(int(request.args.get("limit", 200)), 500)
        history = _wallet_mgr.get_history(limit)
        return jsonify({"ok": True, "count": len(history), "history": history})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


_WALLET_ANALYTICS_CACHE: dict = {"ts": 0.0, "payload": None}
_WALLET_ANALYTICS_TTL = 15  # сек — содержит P&L открытой позиции, обновлять раз в 15с достаточно

@app.route("/api/wallet/analytics")
def api_wallet_analytics():
    """Аналитика GRINCH-позиции: сколько монет, по какой цене куплено, P&L по целям."""
    _now = time.time()
    if _WALLET_ANALYTICS_CACHE["payload"] is not None and \
            (_now - _WALLET_ANALYTICS_CACHE["ts"]) < _WALLET_ANALYTICS_TTL:
        return jsonify(_WALLET_ANALYTICS_CACHE["payload"])
    try:
        import db_store
        status = _wallet_mgr.get_full_status()
        snap   = status.get("snapshot", {})

        # Торговая история по GRINCH из БД
        trades = db_store.trades_get_recent(50)
        grinch_trades = [t for t in trades if "GRINCH" in str(t.get("symbol", "GRINCH"))]

        # Статистика: сколько куплено/продано GRINCH суммарно.
        # Все наши сделки — LONG (side="buy"): покупаем GRINCH за TON, затем продаём.
        # Закрытые LONG-сделки означают, что GRINCH был продан обратно за TON.
        buy_trades    = [t for t in grinch_trades
                         if t.get("side") == "buy" or t.get("type") == "buy"]
        closed_buy    = [t for t in buy_trades
                         if t.get("status") == "closed" or t.get("closed_at")]

        total_bought_grinch = sum(t.get("amount", 0) or 0 for t in buy_trades)
        # GRINCH продано = GRINCH из закрытых LONG-позиций (купили → продали обратно за TON)
        total_sold_grinch   = sum(t.get("amount", 0) or 0 for t in closed_buy)
        # Bug-fix #4: включаем BUY_GAS в реально потраченное
        _buy_gas = getattr(Config, "BUY_GAS_TON", 0.103)
        total_ton_spent     = sum(
            (t.get("stake_ton", 0) or t.get("ton_spent", 0) or 0) + _buy_gas
            for t in buy_trades
        )
        # TON получено = ставка + газ покупки + прибыль по каждой закрытой LONG-сделке
        total_ton_received  = sum(
            (t.get("stake_ton", 0) or t.get("ton_spent", 0) or 0) + _buy_gas
            + (t.get("pnl", 0) or 0)
            for t in closed_buy
        )
        # Bug-fix #3: net_pnl считаем напрямую как сумму реализованных PnL —
        # это не зависит от открытых позиций и всегда показывает реальную прибыль
        net_pnl_ton = round(sum((t.get("pnl", 0) or 0) for t in closed_buy), 4)

        # Цена безубытка по открытым позициям
        open_trades_enriched = getattr(trader, "open_trades", [])
        breakeven_ton = None
        if open_trades_enriched:
            be_list = [t.get("breakeven_price") for t in open_trades_enriched if t.get("breakeven_price")]
            if be_list:
                breakeven_ton = be_list[-1]

        _payload = {
            "ok":    True,
            "snapshot": snap,
            "position": {
                "in_position":      status.get("in_position", False),
                "grinch_count":     status.get("grinch_count", 0),
                "entry_price_ton":  status.get("entry_price_ton"),
                "entry_price_usd":  status.get("entry_price_usd"),
                "current_price_ton": status.get("current_price_ton"),
                "current_price_usd": status.get("current_price_usd"),
                "pnl_ton":          status.get("pnl_ton"),
                "pnl_pct":          status.get("pnl_pct"),
                "pnl_usd":          status.get("pnl_usd"),
                "breakeven_ton":    breakeven_ton,
            },
            "potential":   status.get("potential", {}),
            "price_range": status.get("price_range", {}),
            "cumulative": {
                "total_bought_grinch":  round(total_bought_grinch, 2),
                "total_sold_grinch":    round(total_sold_grinch, 2),
                "total_ton_spent":      round(total_ton_spent, 4),
                "total_ton_received":   round(total_ton_received, 4),
                "net_pnl_ton":          net_pnl_ton,
            },
            "recent_trades": grinch_trades[:20],
        }
        _WALLET_ANALYTICS_CACHE["payload"] = _payload
        _WALLET_ANALYTICS_CACHE["ts"] = time.time()
        return jsonify(_payload)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/wallet/refresh", methods=["POST"])
def api_wallet_refresh():
    """Принудительно обновить снимок кошелька прямо сейчас."""
    try:
        _wallet_mgr._poll()
        snap = _wallet_mgr.get_snapshot()
        return jsonify({"ok": True, "snapshot": snap})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/db/sync_status")
def api_db_sync_status():
    import db_store
    ts = getattr(trader, "_last_db_sync_ts", 0)
    secs = int(time.time() - ts) if ts else None
    trades_count = 0
    try:
        trades_count = db_store.trades_count() if db_store.is_available() else 0
    except Exception:
        pass
    return jsonify({
        "ok":      db_store.is_available(),
        "secs_ago": secs,
        "trades":  trades_count,
        "open":    len(getattr(trader, "open_trades", [])),
    })

@app.route("/api/ton")
def api_ton():
    return jsonify(ton.get_data())

@app.route("/api/ton/refresh", methods=["POST"])
def api_ton_refresh():
    ton.refresh()
    return jsonify(ton.get_data())

@app.route("/api/ton/price")
def api_ton_price():
    import urllib.request, json as _json
    try:
        url = "https://api.dexscreener.com/latest/dex/pairs/ton/EQCM3B12QK1e4yZSf8GtBRT0aLMNyEsBc_9Qsof7gbCmkjvi"
        with urllib.request.urlopen(url, timeout=5) as r:
            d = _json.loads(r.read())
            p = d.get("pair", {}).get("priceUsd") or d.get("pairs", [{}])[0].get("priceUsd", "0")
            return jsonify({"price": float(p or 0)})
    except Exception:
        pass
    try:
        url2 = "https://tonapi.io/v2/rates?tokens=ton&currencies=usd"
        with urllib.request.urlopen(url2, timeout=5) as r2:
            d2 = _json.loads(r2.read())
            p2 = d2.get("rates", {}).get("TON", {}).get("prices", {}).get("USD", 0)
            return jsonify({"price": float(p2 or 0)})
    except Exception:
        pass
    return jsonify({"price": 2.44})

@app.route("/api/coin")
def api_coin():
    base = Config.SYMBOL.split("/")[0].upper()
    return jsonify(coin_info.market(base) or {})

@app.route("/api/coin/trades")
def api_coin_trades():
    base = Config.SYMBOL.split("/")[0].upper()
    return jsonify(coin_info.trades(base, limit=25))

@app.route("/api/coin/exchanges")
def api_coin_exchanges():
    base = Config.SYMBOL.split("/")[0].upper()
    return jsonify(coin_info.exchanges(base))

@app.route("/api/wallets")
def api_wallets():
    """Мониторинг кошельков пула GRINCH: кто покупает/продаёт, умные деньги."""
    return jsonify(wallet_tracker.get_stats())

@app.route("/api/liquidator")
def api_liquidator_status():
    return jsonify(grinch_liquidator.get_status())

@app.route("/api/liquidity_guard")
def api_liquidity_guard_status():
    """Постоянный мониторинг ликвидности пула GRINCH — авто-пауза BUY при просадке."""
    return jsonify(liquidity_guard.get_status())

@app.route("/api/equity")
def api_equity():
    """История изменения баланса кошелька (equity curve)."""
    from experience_manager import experience_manager
    with experience_manager._lock:
        pts = list(experience_manager.data.get("equity", []))
    return jsonify({"points": pts})

@app.route("/api/experience")
def api_experience():
    """Состояние долговременной памяти и само-управления ИИ."""
    from experience_manager import experience_manager
    return jsonify(experience_manager.get_report())


_TRADES_ANALYTICS_CACHE: dict = {"ts": 0.0, "payload": None}
_TRADES_ANALYTICS_TTL = 60  # сек — агрегаты 2000 сделок, обновлять раз в минуту достаточно

@app.route("/api/analytics/trades")
def api_analytics_trades():
    """
    Полная аналитика закрытых сделок из PostgreSQL.
    Возвращает агрегаты по режиму рынка, RSI, умным деньгам, уверенности AI —
    для самообучения и понимания, при каких условиях бот торгует в плюс.
    Результат кэшируется на 60 сек — агрегация 2000 записей дорогая.
    """
    _now = time.time()
    if _TRADES_ANALYTICS_CACHE["payload"] is not None and \
            (_now - _TRADES_ANALYTICS_CACHE["ts"]) < _TRADES_ANALYTICS_TTL:
        return jsonify(_TRADES_ANALYTICS_CACHE["payload"])

    import db_store
    trades = db_store.trades_get_all(limit=2000)
    if not trades:
        return jsonify({"ok": True, "count": 0, "trades": [], "summary": {}})

    wins   = [t for t in trades if t.get("outcome") == "win" or (t.get("pnl", 0) > 0)]
    losses = [t for t in trades if t.get("outcome") == "loss" or (t.get("pnl", 0) <= 0)]
    total  = len(trades)

    # Агрегат по рыночному режиму при входе
    regime_stats = {}
    for t in trades:
        r = t.get("entry_regime") or "unknown"
        if r not in regime_stats:
            regime_stats[r] = {"count": 0, "wins": 0, "total_pnl": 0.0}
        regime_stats[r]["count"] += 1
        if t.get("pnl", 0) > 0:
            regime_stats[r]["wins"] += 1
        regime_stats[r]["total_pnl"] = round(regime_stats[r]["total_pnl"] + t.get("pnl", 0), 6)
    for r in regime_stats:
        c = regime_stats[r]["count"]
        regime_stats[r]["win_rate"] = round(regime_stats[r]["wins"] / c * 100, 1) if c else 0

    # Агрегат по уверенности AI (бакеты 0-49, 50-69, 70-89, 90+)
    conf_buckets = {"0-49": {"count": 0, "wins": 0}, "50-69": {"count": 0, "wins": 0},
                    "70-89": {"count": 0, "wins": 0}, "90+": {"count": 0, "wins": 0}}
    for t in trades:
        c = t.get("ai_confidence") or 0
        try:
            c = float(c)
        except Exception:
            c = 0
        bucket = "90+" if c >= 90 else ("70-89" if c >= 70 else ("50-69" if c >= 50 else "0-49"))
        conf_buckets[bucket]["count"] += 1
        if t.get("pnl", 0) > 0:
            conf_buckets[bucket]["wins"] += 1
    for b in conf_buckets:
        n = conf_buckets[b]["count"]
        conf_buckets[b]["win_rate"] = round(conf_buckets[b]["wins"] / n * 100, 1) if n else 0

    # Агрегат по умным деньгам при входе
    sm_stats = {}
    for t in trades:
        label = t.get("entry_sm_label") or "нет данных"
        if label not in sm_stats:
            sm_stats[label] = {"count": 0, "wins": 0, "total_pnl": 0.0}
        sm_stats[label]["count"] += 1
        if t.get("pnl", 0) > 0:
            sm_stats[label]["wins"] += 1
        sm_stats[label]["total_pnl"] = round(sm_stats[label]["total_pnl"] + t.get("pnl", 0), 6)
    for lbl in sm_stats:
        n = sm_stats[lbl]["count"]
        sm_stats[lbl]["win_rate"] = round(sm_stats[lbl]["wins"] / n * 100, 1) if n else 0

    total_pnl = round(sum(t.get("pnl", 0) for t in trades), 6)
    avg_dur   = None
    durs = [t.get("duration_min") for t in trades if t.get("duration_min") is not None]
    if durs:
        avg_dur = round(sum(durs) / len(durs), 1)

    summary = {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0,
        "total_pnl_ton": total_pnl,
        "avg_duration_min": avg_dur,
        "by_regime": regime_stats,
        "by_ai_confidence": conf_buckets,
        "by_smart_money": sm_stats,
    }
    payload = {"ok": True, "count": total, "trades": trades[-50:], "summary": summary}
    _TRADES_ANALYTICS_CACHE["payload"] = payload
    _TRADES_ANALYTICS_CACHE["ts"] = time.time()
    return jsonify(payload)


@app.route("/api/liquidator/sell", methods=["POST"])
def api_liquidator_sell():
    result = grinch_liquidator.force_sell_now()
    return jsonify(result)

@app.route("/api/liquidator/threshold", methods=["POST"])
def api_liquidator_threshold():
    data = request.json or {}
    try:
        pct = float(data.get("pct", 50.0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Некорректное значение порога"}), 400
    grinch_liquidator.set_threshold(pct)
    return jsonify({"ok": True, "sell_rise_pct": grinch_liquidator.sell_rise_pct})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify({
        "symbol": Config.SYMBOL, "timeframe": Config.TIMEFRAME,
        "trade_amount": Config.TRADE_AMOUNT, "max_open_trades": Config.MAX_OPEN_TRADES,
        "take_profit_pct": Config.TAKE_PROFIT_PCT,
        "trailing_stop_pct": Config.TRAILING_STOP_PCT, "fee_pct": Config.FEE_PCT,
        "use_dynamic_targets": Config.USE_DYNAMIC_TARGETS, "trend_filter": Config.TREND_FILTER,
        "atr_tp_mult": Config.ATR_TP_MULT, "atr_sl_mult": Config.ATR_SL_MULT,
        "min_ai_confidence": Config.MIN_AI_CONFIDENCE, "demo_mode": Config.DEMO_MODE,
        "exchange": Config.EXCHANGE, "ton_wallet": Config.TON_WALLET,
        # Smart BUY
        "smart_buy_enabled":        Config.SMART_BUY_ENABLED,
        "smart_buy_pullback_pct":   Config.SMART_BUY_PULLBACK_PCT,
        "smart_buy_max_wait_ticks": Config.SMART_BUY_MAX_WAIT_TICKS,
        "smart_buy_skip_conf":      Config.SMART_BUY_SKIP_CONF,
        # Smart TP
        "smart_tp_enabled":         Config.SMART_TP_ENABLED,
        "smart_tp_min_conf":        Config.SMART_TP_MIN_CONF,
        "smart_tp_tight_trail_pct": Config.SMART_TP_TIGHT_TRAIL_PCT,
        # Авто-TP от ИИ
        "min_profit_ton":       Config.MIN_PROFIT_TON,
        "ai_tp_adapt_min_trades": Config.AI_TP_ADAPT_MIN_TRADES,
        "ai_tp_cap_pct":        Config.AI_TP_CAP_PCT,
        "ai_tp_report": (lambda ctrl: {
            "adapted":       ctrl.get("ai_tp_adapted", False),
            "take_profit_pct": ctrl.get("take_profit_pct", Config.TAKE_PROFIT_PCT),
            "avg_win_pct":   ctrl.get("ai_avg_win_pct", 0.0),
            "floor_pct":     ctrl.get("min_profit_floor_pct", 0.0),
            "trades_used":   ctrl.get("ai_tp_trades_used", 0),
        })(
            (lambda em: em.data.get("control", {}) if hasattr(em, "data") else {})
            (__import__("experience_manager").experience_manager)
        ),
        # DCA стратегия
        "dca_mode":             Config.DCA_MODE,
        "dca_stake_ton":        Config.DCA_STAKE_TON,
        "dca_target_profit_pct": Config.DCA_TARGET_PROFIT_PCT,
        "dca_drop_trigger_pct": Config.DCA_DROP_TRIGGER_PCT,
        "dca_pullback_wait_pct": Config.DCA_PULLBACK_WAIT_PCT,
        "dca_max_entries":      Config.DCA_MAX_ENTRIES,
        # DCA улучшения (4 механизма)
        "dca_cascade_enabled":    Config.DCA_CASCADE_ENABLED,
        "dca_cascade_level1_pct": Config.DCA_CASCADE_LEVEL1_PCT,
        "dca_cascade_level2_pct": Config.DCA_CASCADE_LEVEL2_PCT,
        "dca_smart_reentry_enabled":     Config.DCA_SMART_REENTRY_ENABLED,
        "dca_smart_reentry_pullback_pct": Config.DCA_SMART_REENTRY_PULLBACK_PCT,
        "dca_smart_reentry_min_ai_conf":  Config.DCA_SMART_REENTRY_MIN_AI_CONF,
        "dca_compound_enabled":   Config.DCA_COMPOUND_ENABLED,
        "dca_compound_ratio":     Config.DCA_COMPOUND_RATIO,
        "dca_compound_max_ton":   Config.DCA_COMPOUND_MAX_TON,
        "dca_adaptive_trigger_enabled":  Config.DCA_ADAPTIVE_TRIGGER_ENABLED,
        "dca_adaptive_fast_move_pct":    Config.DCA_ADAPTIVE_FAST_MOVE_PCT,
        "dca_adaptive_fast_drop_pct":    Config.DCA_ADAPTIVE_FAST_DROP_PCT,
        # Детектор крупных продаж
        "large_sell_dca_enabled":  Config.LARGE_SELL_DCA_ENABLED,
        "large_sell_dca_ton":      Config.LARGE_SELL_DCA_TON,
        "large_sell_min_ton":      Config.LARGE_SELL_MIN_TON,
        "large_sell_cooldown_sec": Config.LARGE_SELL_COOLDOWN_SEC,
        # Защита прибыли
        "profit_protect_enabled":  Config.PROFIT_PROTECT_ENABLED,
        "profit_protect_ton":      Config.PROFIT_PROTECT_TON,
        "profit_protect_drop_pct": Config.PROFIT_PROTECT_DROP_PCT,
        "profit_protect_ai_sell":  Config.PROFIT_PROTECT_AI_SELL,
        # Новые защитные параметры
        "loss_cooldown_sec":        Config.LOSS_COOLDOWN_SEC,
        "dca_ai_sell_block_conf":   Config.DCA_AI_SELL_BLOCK_CONF,
        "confluence_enabled":       Config.CONFLUENCE_ENABLED,
        "confluence_rsi_max":       Config.CONFLUENCE_RSI_MAX,
        "confluence_vol_min_ratio": Config.CONFLUENCE_VOL_MIN_RATIO,
        "ev_threshold":             Config.EV_THRESHOLD,
        # ALL-IN на дне
        "allin_on_bottom":    Config.ALLIN_ON_BOTTOM,
        "allin_bottom_conf":  Config.ALLIN_BOTTOM_CONF,
        "allin_rsi_max":      Config.ALLIN_RSI_MAX,
        "allin_min_free_ton": Config.ALLIN_MIN_FREE_TON,
        # Временной фильтр
        "dead_hours_utc":          Config.DEAD_HOURS_UTC,
        "dead_hours_drop_mult":    Config.DEAD_HOURS_DROP_MULT,
    })

@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.json or {}
    def num(key, lo, hi):
        if key not in data: return None
        try:
            v = float(data[key])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v): return None
        return max(lo, min(hi, v))

    errors = []
    for key in ("trade_amount", "take_profit_pct",
                "max_open_trades", "trailing_stop_pct", "fee_pct", "min_ai_confidence"):
        if key in data and num(key, -1e18, 1e18) is None:
            errors.append(key)
    if errors:
        return jsonify({"ok": False, "message": "Некорректные значения: " + ", ".join(errors)}), 400

    if (v := num("trade_amount", 5, 1e9))      is not None: Config.TRADE_AMOUNT     = v
    if (v := num("take_profit_pct", 0.1, 1000))is not None: Config.TAKE_PROFIT_PCT  = v
    if (v := num("max_open_trades", 1, 50))     is not None: Config.MAX_OPEN_TRADES  = int(v)
    if (v := num("trailing_stop_pct", 0, 90))  is not None: Config.TRAILING_STOP_PCT= v
    if (v := num("fee_pct", 0, 10))            is not None:
        Config.FEE_PCT = v
        Config.FEE_ROUND_TRIP = Config.FEE_PCT * 2   # держим комиссию цикла в синхроне
    if (v := num("min_ai_confidence", 0, 100)) is not None: Config.MIN_AI_CONFIDENCE= v

    # Ручное изменение параметров → обновляем опорные значения ИИ, иначе
    # само-управление потянет их обратно к устаревшей базе.
    try:
        from experience_manager import experience_manager
        experience_manager.set_baseline(
            min_conf=Config.MIN_AI_CONFIDENCE if "min_ai_confidence" in data else None,
            trade_amount=Config.TRADE_AMOUNT if "trade_amount" in data else None,
        )
    except Exception:  # noqa: BLE001
        pass

    if "use_dynamic_targets" in data: Config.USE_DYNAMIC_TARGETS = bool(data["use_dynamic_targets"])
    if "trend_filter"        in data: Config.TREND_FILTER        = bool(data["trend_filter"])

    # Smart BUY
    if "smart_buy_enabled"   in data: Config.SMART_BUY_ENABLED   = bool(data["smart_buy_enabled"])
    if (v := num("smart_buy_pullback_pct",   0.1, 5))   is not None: Config.SMART_BUY_PULLBACK_PCT   = v
    if (v := num("smart_buy_max_wait_ticks", 1,   20))  is not None: Config.SMART_BUY_MAX_WAIT_TICKS = int(v)
    if (v := num("smart_buy_skip_conf",      50,  100)) is not None: Config.SMART_BUY_SKIP_CONF      = v
    # Smart TP
    if "smart_tp_enabled"    in data: Config.SMART_TP_ENABLED     = bool(data["smart_tp_enabled"])
    if (v := num("smart_tp_min_conf",        50,  100)) is not None: Config.SMART_TP_MIN_CONF        = v
    if (v := num("smart_tp_tight_trail_pct", 0.5, 10))  is not None: Config.SMART_TP_TIGHT_TRAIL_PCT = v

    # Авто-TP: пользователь задаёт минимальную прибыль в TON
    if (v := num("min_profit_ton", 0.1, 1000)) is not None:
        Config.MIN_PROFIT_TON = v
    if (v := num("ai_tp_adapt_min_trades", 1, 100)) is not None:
        Config.AI_TP_ADAPT_MIN_TRADES = int(v)
    if (v := num("ai_tp_cap_pct", 5, 500)) is not None:
        Config.AI_TP_CAP_PCT = v

    # DCA стратегия
    if "dca_mode" in data:
        new_dca = bool(data["dca_mode"])
        if new_dca != Config.DCA_MODE:
            if trader.open_trades:
                return jsonify({"ok": False, "message": "Нельзя переключить DCA при открытых сделках."}), 409
            Config.DCA_MODE = new_dca
            # Сброс DCA-состояния при смене режима
            trader.dca_wait_pullback  = False
            trader.dca_peak_price     = 0.0
            trader.dca_last_buy_price = 0.0
            trader.dca_entries_count  = 0
            trader.dca_total_stake    = 0.0
            trader.log(f"🔄 DCA режим {'включён' if new_dca else 'выключен'}", "INFO")
    if (v := num("dca_stake_ton",         1,   10000)) is not None: Config.DCA_STAKE_TON         = v
    if (v := num("dca_target_profit_pct", 1,   200))   is not None: Config.DCA_TARGET_PROFIT_PCT = v
    if (v := num("dca_drop_trigger_pct",  1,   90))    is not None: Config.DCA_DROP_TRIGGER_PCT  = v
    if (v := num("dca_pullback_wait_pct", 5,   90))    is not None: Config.DCA_PULLBACK_WAIT_PCT = v
    if (v := num("dca_max_entries",       1,   50))    is not None: Config.DCA_MAX_ENTRIES       = int(v)
    # DCA улучшения
    if "dca_cascade_enabled" in data:
        Config.DCA_CASCADE_ENABLED = bool(data["dca_cascade_enabled"])
    if (v := num("dca_cascade_level1_pct", 5, 100))  is not None: Config.DCA_CASCADE_LEVEL1_PCT = v
    if (v := num("dca_cascade_level2_pct", 5, 200))  is not None: Config.DCA_CASCADE_LEVEL2_PCT = v
    if "dca_smart_reentry_enabled" in data:
        Config.DCA_SMART_REENTRY_ENABLED = bool(data["dca_smart_reentry_enabled"])
    if (v := num("dca_smart_reentry_pullback_pct", 1, 50))  is not None: Config.DCA_SMART_REENTRY_PULLBACK_PCT = v
    if (v := num("dca_smart_reentry_min_ai_conf",  30, 99)) is not None: Config.DCA_SMART_REENTRY_MIN_AI_CONF  = v
    if "dca_compound_enabled" in data:
        Config.DCA_COMPOUND_ENABLED = bool(data["dca_compound_enabled"])
    if (v := num("dca_compound_ratio",   0.01, 0.9))   is not None: Config.DCA_COMPOUND_RATIO   = v
    if (v := num("dca_compound_max_ton", 10,   10000)) is not None: Config.DCA_COMPOUND_MAX_TON  = v
    if "dca_adaptive_trigger_enabled" in data:
        Config.DCA_ADAPTIVE_TRIGGER_ENABLED = bool(data["dca_adaptive_trigger_enabled"])
    if (v := num("dca_adaptive_fast_move_pct", 1, 30)) is not None: Config.DCA_ADAPTIVE_FAST_MOVE_PCT = v
    if (v := num("dca_adaptive_fast_drop_pct", 1, 30)) is not None: Config.DCA_ADAPTIVE_FAST_DROP_PCT = v

    # Fast reentry / Scalp / DCA cooldown
    if (v := num("fast_reentry_pullback_pct",  0.5, 20))  is not None: Config.FAST_REENTRY_PULLBACK_PCT  = v
    if (v := num("scalp_target_net_pct",       0.5, 50))  is not None: Config.SCALP_TARGET_NET_PCT        = v
    if (v := num("dca_reentry_cooldown_sec",   0,   3600)) is not None: Config.DCA_REENTRY_COOLDOWN_SEC   = int(v)
    # ATR-множители динамических целей
    if (v := num("atr_tp_mult", 0.5, 10.0)) is not None: Config.ATR_TP_MULT = v
    if (v := num("atr_sl_mult", 0.5, 10.0)) is not None: Config.ATR_SL_MULT = v

    # Trail-параметры (ранее отсутствовали в API — не персистировались)
    if (v := num("scalp_max_atr_pct",  0.1, 30))  is not None: Config.SCALP_MAX_ATR_PCT = v
    if (v := num("short_trail_pct",    0.5, 50))  is not None: Config.SHORT_TRAIL_PCT    = v
    if (v := num("trail_stage2_at",    1,   100)) is not None: Config.TRAIL_STAGE2_AT    = v
    if (v := num("trail_stage2_pct",   0.5, 50))  is not None: Config.TRAIL_STAGE2_PCT   = v
    if (v := num("trail_stage3_at",    1,   100)) is not None: Config.TRAIL_STAGE3_AT    = v
    if (v := num("trail_stage3_pct",   0.5, 50))  is not None: Config.TRAIL_STAGE3_PCT   = v
    if (v := num("trail_stage4_at",    1,   100)) is not None: Config.TRAIL_STAGE4_AT    = v
    if (v := num("trail_stage4_pct",   0.5, 50))  is not None: Config.TRAIL_STAGE4_PCT   = v

    # Детектор крупных продаж
    if "large_sell_dca_enabled" in data:
        Config.LARGE_SELL_DCA_ENABLED = bool(data["large_sell_dca_enabled"])
    if (v := num("large_sell_dca_ton",      10, 100000)) is not None: Config.LARGE_SELL_DCA_TON      = v
    if (v := num("large_sell_min_ton",      50, 100000)) is not None: Config.LARGE_SELL_MIN_TON      = v
    if (v := num("large_sell_cooldown_sec", 30, 86400))  is not None: Config.LARGE_SELL_COOLDOWN_SEC = int(v)

    # Защита прибыли
    if "profit_protect_enabled" in data:
        Config.PROFIT_PROTECT_ENABLED = bool(data["profit_protect_enabled"])
    if "profit_protect_ai_sell" in data:
        Config.PROFIT_PROTECT_AI_SELL = bool(data["profit_protect_ai_sell"])
    if (v := num("profit_protect_ton",      0.1, 10000)) is not None: Config.PROFIT_PROTECT_TON      = v
    if (v := num("profit_protect_drop_pct", 0.3, 20))    is not None: Config.PROFIT_PROTECT_DROP_PCT = v

    # Новые защитные параметры
    if (v := num("loss_cooldown_sec",        0,  86400)) is not None: Config.LOSS_COOLDOWN_SEC        = int(v)
    if (v := num("dca_ai_sell_block_conf",   0,  100))   is not None: Config.DCA_AI_SELL_BLOCK_CONF   = v
    if "confluence_enabled" in data:
        Config.CONFLUENCE_ENABLED = bool(data["confluence_enabled"])
    if (v := num("confluence_rsi_max",       50, 100))   is not None: Config.CONFLUENCE_RSI_MAX       = v
    if (v := num("confluence_vol_min_ratio", 0,  10))    is not None: Config.CONFLUENCE_VOL_MIN_RATIO = v
    if (v := num("ev_threshold",            -100, 100))  is not None: Config.EV_THRESHOLD             = v

    # ALL-IN на дне: покупка всего баланса при экстремальной перепроданности
    if "allin_on_bottom" in data:
        Config.ALLIN_ON_BOTTOM = bool(data["allin_on_bottom"])
    if (v := num("allin_bottom_conf",   10, 100))   is not None: Config.ALLIN_BOTTOM_CONF  = v
    if (v := num("allin_rsi_max",        5,  50))   is not None: Config.ALLIN_RSI_MAX       = v
    if (v := num("allin_min_free_ton",   1, 10000)) is not None: Config.ALLIN_MIN_FREE_TON  = v

    # Временной фильтр: мёртвые часы
    if "dead_hours_utc" in data:
        _dh_raw = data["dead_hours_utc"]
        try:
            if isinstance(_dh_raw, list):
                Config.DEAD_HOURS_UTC = [int(h) for h in _dh_raw]
            else:
                Config.DEAD_HOURS_UTC = [
                    int(h) for h in str(_dh_raw).split(",") if str(h).strip().lstrip("-").isdigit()
                ]
        except Exception:
            pass
    if (v := num("dead_hours_drop_mult", 1.0, 5.0)) is not None: Config.DEAD_HOURS_DROP_MULT = v

    if "symbol" in data and data["symbol"] != Config.SYMBOL:
        if trader.open_trades:
            return jsonify({"ok": False, "message": "Нельзя сменить пару при открытых сделках."}), 409
        Config.SYMBOL = data["symbol"]

    # Сохраняем текущее состояние настроек на диск, чтобы они пережили перезапуск
    try:
        from settings_store import update_section
        update_section("config", {
            "SYMBOL":            Config.SYMBOL,
            "TRADE_AMOUNT":      Config.TRADE_AMOUNT,
            "MAX_OPEN_TRADES":   Config.MAX_OPEN_TRADES,
            "TAKE_PROFIT_PCT":   Config.TAKE_PROFIT_PCT,
            "TRAILING_STOP_PCT": Config.TRAILING_STOP_PCT,
            "FEE_PCT":           Config.FEE_PCT,
            "MIN_AI_CONFIDENCE": Config.MIN_AI_CONFIDENCE,
            "USE_DYNAMIC_TARGETS": Config.USE_DYNAMIC_TARGETS,
            "TREND_FILTER":      Config.TREND_FILTER,
            # Smart BUY
            "SMART_BUY_ENABLED":        Config.SMART_BUY_ENABLED,
            "SMART_BUY_PULLBACK_PCT":   Config.SMART_BUY_PULLBACK_PCT,
            "SMART_BUY_MAX_WAIT_TICKS": Config.SMART_BUY_MAX_WAIT_TICKS,
            "SMART_BUY_SKIP_CONF":      Config.SMART_BUY_SKIP_CONF,
            # Smart TP
            "SMART_TP_ENABLED":         Config.SMART_TP_ENABLED,
            "SMART_TP_MIN_CONF":        Config.SMART_TP_MIN_CONF,
            "SMART_TP_TIGHT_TRAIL_PCT": Config.SMART_TP_TIGHT_TRAIL_PCT,
            # Авто-TP от ИИ
            "MIN_PROFIT_TON":          Config.MIN_PROFIT_TON,
            "AI_TP_ADAPT_MIN_TRADES":  Config.AI_TP_ADAPT_MIN_TRADES,
            "AI_TP_CAP_PCT":           Config.AI_TP_CAP_PCT,
            # DCA стратегия
            "DCA_MODE":             Config.DCA_MODE,
            "DCA_STAKE_TON":        Config.DCA_STAKE_TON,
            "DCA_TARGET_PROFIT_PCT": Config.DCA_TARGET_PROFIT_PCT,
            "DCA_DROP_TRIGGER_PCT": Config.DCA_DROP_TRIGGER_PCT,
            "DCA_PULLBACK_WAIT_PCT": Config.DCA_PULLBACK_WAIT_PCT,
            "DCA_MAX_ENTRIES":      Config.DCA_MAX_ENTRIES,
            # DCA улучшения (4 механизма)
            "DCA_CASCADE_ENABLED":    Config.DCA_CASCADE_ENABLED,
            "DCA_CASCADE_LEVEL1_PCT": Config.DCA_CASCADE_LEVEL1_PCT,
            "DCA_CASCADE_LEVEL2_PCT": Config.DCA_CASCADE_LEVEL2_PCT,
            "DCA_SMART_REENTRY_ENABLED":     Config.DCA_SMART_REENTRY_ENABLED,
            "DCA_SMART_REENTRY_PULLBACK_PCT": Config.DCA_SMART_REENTRY_PULLBACK_PCT,
            "DCA_SMART_REENTRY_MIN_AI_CONF":  Config.DCA_SMART_REENTRY_MIN_AI_CONF,
            "DCA_COMPOUND_ENABLED":   Config.DCA_COMPOUND_ENABLED,
            "DCA_COMPOUND_RATIO":     Config.DCA_COMPOUND_RATIO,
            "DCA_COMPOUND_MAX_TON":   Config.DCA_COMPOUND_MAX_TON,
            "DCA_ADAPTIVE_TRIGGER_ENABLED":  Config.DCA_ADAPTIVE_TRIGGER_ENABLED,
            "DCA_ADAPTIVE_FAST_MOVE_PCT":    Config.DCA_ADAPTIVE_FAST_MOVE_PCT,
            "DCA_ADAPTIVE_FAST_DROP_PCT":    Config.DCA_ADAPTIVE_FAST_DROP_PCT,
            # Детектор крупных продаж
            "LARGE_SELL_DCA_ENABLED":  Config.LARGE_SELL_DCA_ENABLED,
            "LARGE_SELL_DCA_TON":      Config.LARGE_SELL_DCA_TON,
            "LARGE_SELL_MIN_TON":      Config.LARGE_SELL_MIN_TON,
            "LARGE_SELL_COOLDOWN_SEC": Config.LARGE_SELL_COOLDOWN_SEC,
            # Защита прибыли
            "PROFIT_PROTECT_ENABLED":  Config.PROFIT_PROTECT_ENABLED,
            "PROFIT_PROTECT_TON":      Config.PROFIT_PROTECT_TON,
            "PROFIT_PROTECT_DROP_PCT": Config.PROFIT_PROTECT_DROP_PCT,
            "PROFIT_PROTECT_AI_SELL":  Config.PROFIT_PROTECT_AI_SELL,
            # Fast reentry / Scalp / DCA cooldown
            "FAST_REENTRY_PULLBACK_PCT":  Config.FAST_REENTRY_PULLBACK_PCT,
            "SCALP_TARGET_NET_PCT":        Config.SCALP_TARGET_NET_PCT,
            "DCA_REENTRY_COOLDOWN_SEC":    Config.DCA_REENTRY_COOLDOWN_SEC,
            # ATR-множители динамических целей
            "ATR_TP_MULT": Config.ATR_TP_MULT,
            "ATR_SL_MULT": Config.ATR_SL_MULT,
            # Новые защитные параметры
            "LOSS_COOLDOWN_SEC":        Config.LOSS_COOLDOWN_SEC,
            "DCA_AI_SELL_BLOCK_CONF":   Config.DCA_AI_SELL_BLOCK_CONF,
            "CONFLUENCE_ENABLED":       Config.CONFLUENCE_ENABLED,
            "CONFLUENCE_RSI_MAX":       Config.CONFLUENCE_RSI_MAX,
            "CONFLUENCE_VOL_MIN_RATIO": Config.CONFLUENCE_VOL_MIN_RATIO,
            "EV_THRESHOLD":             Config.EV_THRESHOLD,
            # Trail-параметры (ранее отсутствовали — теперь персистируются)
            "SCALP_MAX_ATR_PCT": Config.SCALP_MAX_ATR_PCT,
            "SHORT_TRAIL_PCT":   Config.SHORT_TRAIL_PCT,
            "TRAIL_STAGE2_AT":   Config.TRAIL_STAGE2_AT,
            "TRAIL_STAGE2_PCT":  Config.TRAIL_STAGE2_PCT,
            "TRAIL_STAGE3_AT":   Config.TRAIL_STAGE3_AT,
            "TRAIL_STAGE3_PCT":  Config.TRAIL_STAGE3_PCT,
            "TRAIL_STAGE4_AT":   Config.TRAIL_STAGE4_AT,
            "TRAIL_STAGE4_PCT":  Config.TRAIL_STAGE4_PCT,
            # ALL-IN на дне
            "ALLIN_ON_BOTTOM":    Config.ALLIN_ON_BOTTOM,
            "ALLIN_BOTTOM_CONF":  Config.ALLIN_BOTTOM_CONF,
            "ALLIN_RSI_MAX":      Config.ALLIN_RSI_MAX,
            "ALLIN_MIN_FREE_TON": Config.ALLIN_MIN_FREE_TON,
            # Временной фильтр
            "DEAD_HOURS_UTC":       ",".join(str(h) for h in Config.DEAD_HOURS_UTC),
            "DEAD_HOURS_DROP_MULT": Config.DEAD_HOURS_DROP_MULT,
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": True, "message": f"Настройки применены, но не сохранены на диск: {e}"})

    return jsonify({"ok": True, "message": "Настройки сохранены (применятся и после перезапуска)"})


# ════════════════════════════════════════════════════════════════════════════
#  Публичная платформа — TonConnect модель (без мнемоники)
# ════════════════════════════════════════════════════════════════════════════

@app.route("/join")
def join_page():
    t, w = trader.stats.get("total_trades", 0), trader.stats.get("winning_trades", 0)
    stats = {
        "active_traders": user_mgr.count_active(),
        "total_trades":   t,
        "winrate":        round(w / t * 100, 1) if t > 0 else 0,
    }
    return render_template("join.html",
                           stats=stats,
                           platform_wallet=Config.TON_WALLET)


@app.route("/dashboard/<token>")
def user_dashboard(token):
    if not _db_available:
        return "Мультипользовательские функции временно недоступны (нет подключения к БД)", 503
    status = user_mgr.get_status(token)
    if not status:
        with app.app_context():
            uw = UserWallet.query.filter_by(token=token).first()
        if not uw:
            return render_template("404.html"), 404
        try:
            user_mgr.register(token, uw.ton_address, uw.trade_amount, uw.name)
            # restore virtual balances from DB
            with app.app_context():
                uw2 = UserWallet.query.filter_by(token=token).first()
                user_mgr._restore(uw2)
            status = user_mgr.get_status(token)
        except Exception as e:
            return f"Ошибка загрузки аккаунта: {e}", 500

    with app.app_context():
        uw = UserWallet.query.filter_by(token=token).first()
        deposit_code = f"GG-{token[:8]}"
        deposited    = uw.total_deposited if uw else 0
        withdrawn    = uw.total_withdrawn if uw else 0

    return render_template("user_dash.html",
                           token=token,
                           init_status=status,
                           platform_wallet=Config.TON_WALLET,
                           deposit_code=deposit_code,
                           total_deposited=deposited,
                           total_withdrawn=withdrawn)


# ── API пользователей ──────────────────────────────────────────────────────

@app.route("/api/user/register", methods=["POST"])
def api_user_register():
    if not _db_available:
        return jsonify({"ok": False, "error": "БД недоступна — регистрация временно невозможна"}), 503
    data         = request.json or {}
    name         = str(data.get("name", "")).strip()[:80]
    ton_address  = str(data.get("ton_address", "")).strip()
    try:
        trade_amount = float(data.get("trade_amount", 1.0))
        if trade_amount < 0.5 or trade_amount > 1000:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Сумма сделки: от 0.5 до 1000 TON"}), 400

    if not ton_address:
        return jsonify({"ok": False, "error": "Адрес кошелька не указан"}), 400

    import uuid
    token = str(uuid.uuid4())
    uw = UserWallet(
        token=token,
        name=name,
        ton_address=ton_address,
        encrypted_mnemonic=None,
        trade_amount=trade_amount,
        active=True,
    )
    db.session.add(uw)
    db.session.commit()

    user_mgr.register(token, ton_address, trade_amount, name)

    deposit_code  = f"GG-{token[:8]}"
    dashboard_url = f"/dashboard/{token}"
    return jsonify({
        "ok":           True,
        "token":        token,
        "deposit_code": deposit_code,
        "dashboard_url": dashboard_url,
        "platform_wallet": Config.TON_WALLET,
    })


@app.route("/api/user/status/<token>")
def api_user_status(token):
    st = user_mgr.get_status(token)
    if not st:
        return jsonify({"ok": False, "error": "Не найдено"}), 404
    return jsonify({"ok": True, **st})


@app.route("/api/user/deposit", methods=["POST"])
def api_user_deposit_manual():
    """Ручное зачисление депозита (для тестирования / после ручной проверки)."""
    if not _db_available:
        return jsonify({"ok": False, "error": "БД недоступна"}), 503
    data   = request.json or {}
    token  = str(data.get("token", ""))
    amount = float(data.get("amount", 0))
    if amount <= 0:
        return jsonify({"ok": False, "error": "Сумма должна быть > 0"}), 400
    ok = user_mgr.credit_deposit(token, amount, app)
    if not ok:
        return jsonify({"ok": False, "error": "Пользователь не найден"}), 404

    with app.app_context():
        uw = UserWallet.query.filter_by(token=token).first()
        if uw:
            uw.total_deposited = (uw.total_deposited or 0) + amount
            db.session.commit()
    return jsonify({"ok": True, "credited": amount})


@app.route("/api/user/withdraw", methods=["POST"])
def api_user_withdraw():
    data   = request.json or {}
    token  = str(data.get("token", ""))
    amount = float(data.get("amount", 0))
    if amount < 0.1:
        return jsonify({"ok": False, "error": "Минимальный вывод 0.1 TON"}), 400
    result = user_mgr.withdraw(token, amount, app)
    return jsonify(result), 200 if result.get("ok") else 400


@app.route("/api/platform/stats")
def api_platform_stats():
    t = trader.stats.get("total_trades", 0)
    w = trader.stats.get("winning_trades", 0)
    return jsonify({
        "active_traders":  user_mgr.count_active(),
        "ai_winrate":      round(w / t * 100, 1) if t > 0 else 0,
        "total_trades":    t,
        "platform_fee":    9.5,
        "platform_wallet": Config.TON_WALLET,
        "owner_address":   "UQDDgb2BTM-KCjntOoUg6uHllvnu3KGqEquKw6IySVP3hDgM",
    })


# ════════════════════════════════════════════════════════════════════════════
#  Socket.IO
# ════════════════════════════════════════════════════════════════════════════

@socketio.on("connect")
def on_connect(auth=None):
    global _connected_clients
    # Поток статуса панели — только для владельца после входа.
    if _auth_configured() and not session.get("logged_in"):
        return False  # отклонить подключение неавторизованного клиента
    with _connected_lock:
        _connected_clients += 1
    try:
        emit("status_update", _status_for_response())
    except Exception as e:
        print(f"[on_connect] Ошибка: {e}")


@socketio.on("disconnect")
def on_disconnect():
    global _connected_clients
    with _connected_lock:
        _connected_clients = max(0, _connected_clients - 1)


def _free_port(port: int):
    """Освобождает TCP-порт перед запуском сервера.

    Находит ЧУЖОЙ процесс, который слушает этот порт (например, зависший прошлый
    экземпляр приложения), и аккуратно завершает его (SIGTERM, затем SIGKILL).
    Без этого рестарт падал с 'Address already in use'. Свой PID не трогаем,
    на чистом старте (никто не слушает) — это no-op.
    """
    import glob
    import signal

    my_pid = os.getpid()
    target_hex = f"{port:04X}"

    def _listening_inodes():
        inodes = set()
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(path) as f:
                    next(f, None)  # пропускаем заголовок
                    for line in f:
                        parts = line.split()
                        if len(parts) < 10:
                            continue
                        local, state = parts[1], parts[3]
                        if state != "0A":  # 0A = LISTEN
                            continue
                        if local.split(":")[-1].upper() == target_hex:
                            inodes.add(parts[9])
            except FileNotFoundError:
                pass
        return inodes

    inodes = _listening_inodes()
    if not inodes:
        return

    def _is_our_app(pid: int) -> bool:
        # Завершаем ТОЛЬКО зависший экземпляр этого же приложения, а не любой
        # чужой процесс на порту, чтобы случайно не убить посторонний сервис.
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            return False
        return "app.py" in cmd

    pids = set()
    for fd_link in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            link = os.readlink(fd_link)
        except OSError:
            continue
        if not link.startswith("socket:["):
            continue
        if link[len("socket:["):-1] in inodes:
            try:
                pid = int(fd_link.split("/")[2])
            except (IndexError, ValueError):
                continue
            if pid != my_pid and _is_our_app(pid):
                pids.add(pid)

    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[startup] порт {port} занят процессом {pid} — отправлен SIGTERM")
        except OSError:
            pass
    time.sleep(2)
    for pid in pids:
        try:
            os.kill(pid, 0)              # ещё жив?
            os.kill(pid, signal.SIGKILL)  # добиваем
        except OSError:
            pass
    time.sleep(1)


if __name__ == "__main__":
    import errno

    # Bothost передаёт порт через PORT; на Replit фолбэк — 5000
    _PORT = int(os.environ.get("PORT", 5000))

    start_background()
    _free_port(_PORT)
    for attempt in range(1, 11):
        try:
            socketio.run(app, host="0.0.0.0", port=_PORT,
                         debug=False, allow_unsafe_werkzeug=True)
            break
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            print(f"[startup] порт {_PORT} занят "
                  f"(попытка {attempt}/10): {e} — освобождаю и повторяю…")
            _free_port(_PORT)
            time.sleep(2)
    else:
        raise SystemExit(f"[startup] порт {_PORT} так и не освободился")
