import json
import math
import os
import numpy as np
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from flask.json.provider import DefaultJSONProvider
from flask_socketio import SocketIO, emit
import threading
import time
import logging
from config import Config
from database import db
from trader import Trader
from ton_tracker import TONTracker
from coin_info import coin_info

log = logging.getLogger(__name__)


class NumpyJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        return json.dumps(obj, default=self._convert, **kwargs)

    def _convert(self, o):
        if isinstance(o, (np.integer,)):  return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        if isinstance(o, (np.bool_,)):    return bool(o)
        if isinstance(o, np.ndarray):     return o.tolist()
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


app = Flask(__name__)
app.json_provider_class = NumpyJSONProvider
app.json = NumpyJSONProvider(app)
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

# ── База данных ───────────────────────────────────────────────────────────────
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///grinchgram.db"
)
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
    def _default(o):
        if isinstance(o, (np.integer,)):  return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        if isinstance(o, (np.bool_,)):    return bool(o)
        if isinstance(o, np.ndarray):     return o.tolist()
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")
    kw.setdefault("default", _default)
    return _orig_dumps(obj, **kw)

import flask_socketio
flask_socketio.json = type("_J", (), {
    "dumps": staticmethod(_safe_dumps),
    "loads": staticmethod(json.loads),
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

from deposit_monitor import DepositMonitor
deposit_monitor = DepositMonitor(Config.TON_WALLET)


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

def push_updates():
    while True:
        try:
            socketio.emit("status_update", _safe_status())
        except Exception as e:
            print(f"[Push] Ошибка: {e}")
        time.sleep(5)


def push_price():
    from price_feed import price_feed
    last, last_symbol = None, None
    while True:
        try:
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
        ton.start()

start_background()


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


# ════════════════════════════════════════════════════════════════════════════
#  Главный дашборд
# ════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    try:
        status       = _safe_status()
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
    return jsonify(_safe_status())

@app.route("/api/candles")
def api_candles():
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
    return jsonify({
        "candles": _walk(analysis.get("candles", [])),
        "price":   _walk(analysis.get("price", 0)),
    })

@app.route("/api/start", methods=["POST"])
def api_start():
    trader.start()
    return jsonify({"ok": True, "message": "Агент запущен"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    trader.stop()
    return jsonify({"ok": True, "message": "Агент остановлен"})

@app.route("/api/trade/close", methods=["POST"])
def api_trade_close():
    data = request.get_json(silent=True) or {}
    tid = data.get("id")
    if tid is None or tid == "":
        return jsonify({"ok": False, "error": "не указан id позиции"}), 400
    result = trader.close_trade(tid)
    return jsonify(result), (200 if result.get("ok") else 400)

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

@app.route("/api/liquidator")
def api_liquidator_status():
    return jsonify(grinch_liquidator.get_status())

@app.route("/api/experience")
def api_experience():
    """Состояние долговременной памяти и само-управления ИИ."""
    from experience_manager import experience_manager
    return jsonify(experience_manager.get_report())

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

    if (v := num("trade_amount", 1, 1e9))      is not None: Config.TRADE_AMOUNT     = v
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
    # Поток статуса панели — только для владельца после входа.
    if _auth_configured() and not session.get("logged_in"):
        return False  # отклонить подключение неавторизованного клиента
    try:
        emit("status_update", _safe_status())
    except Exception as e:
        print(f"[on_connect] Ошибка: {e}")


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

    start_background()
    _free_port(5000)
    for attempt in range(1, 11):
        try:
            socketio.run(app, host="0.0.0.0", port=5000,
                         debug=False, allow_unsafe_werkzeug=True)
            break
        except OSError as e:
            # Повторяем ТОЛЬКО при «адрес занят»; прочие ошибки — пробрасываем.
            if e.errno != errno.EADDRINUSE:
                raise
            print(f"[startup] порт 5000 занят "
                  f"(попытка {attempt}/10): {e} — освобождаю и повторяю…")
            _free_port(5000)
            time.sleep(2)
    else:
        raise SystemExit("[startup] порт 5000 так и не освободился")
