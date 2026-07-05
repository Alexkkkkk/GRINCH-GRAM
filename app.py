import json
import math
import os
import numpy as np
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from flask.json.provider import DefaultJSONProvider
from flask_socketio import SocketIO, emit
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
import logging
from config import Config
from database import db
from trader import Trader
from ton_tracker import TONTracker
from coin_info import coin_info

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
    Слабый зашитый ключ по умолчанию не используется (иначе cookie подделать)."""
    import secrets as _secrets
    key = os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY")
    if key and key != "grinch-gram-secret-2024":
        return key
    path = ".session_secret"
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


_SECRET_KEY = _resolve_secret_key()
app.config["SECRET_KEY"] = _SECRET_KEY
app.secret_key = _SECRET_KEY

# ── База данных — берётся из переменной окружения DATABASE_URL (Replit PostgreSQL) ───
_DATABASE_URL = os.environ.get("DATABASE_URL")
if not _DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")
app.config["SQLALCHEMY_DATABASE_URI"] = _DATABASE_URL
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_recycle": 300, "pool_pre_ping": True}
db.init_app(app)

with app.app_context():
    from models import UserWallet   # noqa: F401
    db.create_all()
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

# ── Торговые движки ───────────────────────────────────────────────────────────
trader = Trader()
ton    = TONTracker(Config.TON_WALLET)

from user_trader import UserTradingManager, encrypt_mnemonic, decrypt_mnemonic
user_mgr = UserTradingManager()
trader.signal_callbacks.append(user_mgr.on_signal)

from grinch_liquidator import grinch_liquidator
import liquidity_guard

from deposit_monitor import DepositMonitor
deposit_monitor = DepositMonitor(Config.TON_WALLET)

from wallet_tracker import WalletTracker
wallet_tracker = WalletTracker()
# Бот учится у реальных кошельков в пуле — отдаём трекер торговому движку
trader.wallet_tracker = wallet_tracker


def _safe_status():
    def _walk(obj):
        if isinstance(obj, dict):             return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):    return [_walk(v) for v in obj]
        if isinstance(obj, (np.integer,)):    return int(obj)
        if isinstance(obj, (np.floating,)):   return float(obj)
        if isinstance(obj, (np.bool_,)):      return bool(obj)
        if isinstance(obj, np.ndarray):       return obj.tolist()
        return obj
    return _walk(trader.get_status())


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


def _load_users_bg():
    time.sleep(3)
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
        threading.Thread(target=push_updates,    daemon=True).start()
        threading.Thread(target=push_price,      daemon=True).start()
        threading.Thread(target=_load_users_bg,  daemon=True).start()
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
        except Exception as _al_ex:
            print(f"[Alerts] монитор не запущен: {_al_ex}")

start_background()

# ── AI Советник: запуск фонового потока автономии ──────────────────────────
try:
    from ai_advisor import start_background as _adv_start
    _adv_start()
except Exception as _adv_ex:
    print(f"[Advisor] не запущен: {_adv_ex}")


# ════════════════════════════════════════════════════════════════════════════
#  Авторизация — логин / пароль для входа в панель
# ════════════════════════════════════════════════════════════════════════════
import hmac
from datetime import timedelta

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

app.permanent_session_lifetime = timedelta(days=30)

# Публичные пути — доступны без входа (страницы участников платформы).
# Точные пути + узкие префиксы, чтобы случайно не открыть будущие эндпоинты.
_PUBLIC_EXACT = {
    "/login", "/logout", "/favicon.ico",
    "/tonconnect-manifest.json", "/join", "/api/platform/stats",
}
_PUBLIC_PREFIXES = ("/static/", "/dashboard/", "/api/user/")


def _auth_configured():
    return bool(ADMIN_USERNAME and ADMIN_PASSWORD)


def _is_public_path(path):
    return path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES)


@app.before_request
def _require_login():
    # Пока логин/пароль не заданы — доступ не блокируем (чтобы не закрыть панель).
    if not _auth_configured():
        return None
    path = request.path or "/"
    if _is_public_path(path):
        return None
    if session.get("logged_in"):
        return None
    if path.startswith("/api") or path.startswith("/socket.io"):
        return jsonify({"ok": False, "error": "Требуется вход"}), 401
    return redirect(url_for("login", next=path))


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
    if not trader.running:
        return jsonify({"status": "ok", "trader": "stopped"}), 200

    now = time.time()
    age = now - (trader.last_tick_ts or 0)
    if trader.last_tick_ts == 0:
        # Ещё идёт предобучение AI перед первым тиком — это ожидаемо, не ошибка
        return jsonify({"status": "ok", "trader": "starting"}), 200
    if age > 90:
        return jsonify({
            "status": "unhealthy",
            "reason": "trading loop stalled",
            "seconds_since_last_tick": round(age, 1),
        }), 503
    if trader.last_tick_ok is False:
        return jsonify({
            "status": "degraded",
            "reason": "last tick raised an error (see logs)",
            "seconds_since_last_tick": round(age, 1),
        }), 200

    return jsonify({
        "status": "ok",
        "trader": "running",
        "seconds_since_last_tick": round(age, 1),
    }), 200


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
                           init_ai=init_ai, init_balance=init_balance)


@app.route("/api/status")
def api_status():
    # Отдаём готовый снимок из буфера — мгновенно, без ожидания сети/блокчейна.
    # Пока буфер не прогрет (самый первый запрос) — считаем напрямую один раз.
    return jsonify(_status_for_response())

_CANDLES_CACHE = {"ts": 0.0, "payload": None}
_CANDLES_CACHE_TTL = 8  # сек — свечи обновляются раз в 15м, считать индикаторы на каждый опрос (10с) незачем

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
    result = trader.close_trade(tid)
    return jsonify(result), (200 if result.get("ok") else 400)

@app.route("/api/ai/decisions")
def api_ai_decisions():
    log = getattr(trader, "decision_log", [])
    return jsonify(list(reversed(log))[:15])

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
    from ai_advisor import set_config, AUTO_INTERVAL_MIN, AUTO_TRADES_TRIGGER
    if request.method == "POST":
        data = request.json or {}
        result = set_config(
            interval_min   = data.get("interval_min"),
            trades_trigger = data.get("trades_trigger"),
        )
        return jsonify({"ok": True, **result})
    return jsonify({"interval_min": AUTO_INTERVAL_MIN, "trades_trigger": AUTO_TRADES_TRIGGER})

@app.route("/api/advisor/log")
def api_advisor_log():
    from ai_advisor import get_adaptation_log
    return jsonify(get_adaptation_log())

@app.route("/api/advisor/apikey", methods=["POST"])
def api_advisor_apikey():
    from ai_advisor import reload_key
    import settings_store
    data = request.json or {}
    key  = str(data.get("key", "")).strip()
    if not key:
        return jsonify({"ok": False, "error": "Ключ не может быть пустым"})
    # сохраняем в персистентное хранилище (settings.json / PostgreSQL)
    settings_store.update_section("advisor", {"groq_api_key": key})
    # применяем немедленно без перезапуска
    reload_key(key)
    return jsonify({"ok": True, "enabled": True})

@app.route("/api/advisor/apikey", methods=["GET"])
def api_advisor_apikey_get():
    import settings_store
    sec = settings_store.get_section("advisor")
    stored = sec.get("groq_api_key", "")
    # возвращаем только маску — не раскрываем ключ в UI
    masked = ("gsk_" + "•" * 20 + stored[-4:]) if len(stored) > 8 else ("•" * len(stored) if stored else "")
    return jsonify({"ok": True, "has_key": bool(stored), "masked": masked})

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


@app.route("/api/analytics/trades")
def api_analytics_trades():
    """
    Полная аналитика закрытых сделок из PostgreSQL.
    Возвращает агрегаты по режиму рынка, RSI, умным деньгам, уверенности AI —
    для самообучения и понимания, при каких условиях бот торгует в плюс.
    """
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
    return jsonify({"ok": True, "count": total, "trades": trades[-50:], "summary": summary})


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
    if (v := num("dca_drop_trigger_pct",  5,   90))    is not None: Config.DCA_DROP_TRIGGER_PCT  = v
    if (v := num("dca_pullback_wait_pct", 5,   90))    is not None: Config.DCA_PULLBACK_WAIT_PCT = v
    if (v := num("dca_max_entries",       1,   50))    is not None: Config.DCA_MAX_ENTRIES       = int(v)

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
