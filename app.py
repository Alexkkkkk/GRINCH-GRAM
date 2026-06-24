import json
import math
import numpy as np
from flask import Flask, render_template, jsonify, request
from flask.json.provider import DefaultJSONProvider
from flask_socketio import SocketIO, emit
import threading
import time
from config import Config
from trader import Trader
from ton_tracker import TONTracker
from coin_info import coin_info


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
app.config["SECRET_KEY"] = Config.SECRET_KEY

# Patch socketio to also use numpy-safe serialisation
import socketio as sio_pkg

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
flask_socketio.json = type("_J", (), {"dumps": staticmethod(_safe_dumps), "loads": staticmethod(json.loads)})()

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    allow_upgrades=True,
                    ping_timeout=60, ping_interval=25,
                    json=type("_J", (), {"dumps": staticmethod(_safe_dumps), "loads": staticmethod(json.loads)})())

trader = Trader()
ton = TONTracker(Config.TON_WALLET)


def _safe_status():
    """Return status dict with all numpy types converted to Python natives."""
    def _walk(obj):
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_walk(v) for v in obj]
        if isinstance(obj, (np.integer,)):  return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, (np.bool_,)):    return bool(obj)
        if isinstance(obj, np.ndarray):     return obj.tolist()
        return obj
    return _walk(trader.get_status())


def push_updates():
    while True:
        try:
            status = _safe_status()
            socketio.emit("status_update", status)
        except Exception as e:
            print(f"[Push] Ошибка: {e}")
        time.sleep(5)


def push_price():
    """Живая цена в реальном времени — частое обновление (каждые 2 сек)."""
    last = None
    last_symbol = None
    while True:
        try:
            symbol = Config.SYMBOL
            # При смене пары сбрасываем базу для расчёта изменения
            if symbol != last_symbol:
                last = None
                last_symbol = symbol
            price = float(trader.exchange.get_live_price())
            change = 0.0
            if last:
                change = round((price - last) / last * 100, 3)
            socketio.emit("price_update", {
                "symbol": symbol,
                "price": price,
                "change": change,
            })
            last = price
        except Exception as e:
            print(f"[Price] Ошибка: {e}")
        time.sleep(2)


_bg_started = False
_bg_lock = threading.Lock()

def start_background():
    """Запуск фоновых потоков (один раз на процесс, совместимо с gunicorn)."""
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
        threading.Thread(target=push_updates, daemon=True).start()
        threading.Thread(target=push_price, daemon=True).start()
        ton.start()


# Запуск при импорте (gunicorn выполняет app:app, не заходя в __main__)
start_background()


@app.route("/")
def index():
    try:
        status = _safe_status()
        init_price = status.get("analysis", {}).get("price", 0)
    except Exception:
        init_price = 0
    return render_template("index.html", symbol=Config.SYMBOL, demo=Config.DEMO_MODE, init_price=init_price)

@app.route("/api/status")
def api_status():
    return jsonify(_safe_status())

@app.route("/api/start", methods=["POST"])
def api_start():
    trader.start()
    return jsonify({"ok": True, "message": "Агент запущен"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    trader.stop()
    return jsonify({"ok": True, "message": "Агент остановлен"})

@app.route("/api/ton")
def api_ton():
    return jsonify(ton.get_data())

@app.route("/api/ton/refresh", methods=["POST"])
def api_ton_refresh():
    ton.refresh()
    return jsonify(ton.get_data())

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

@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify({
        "symbol": Config.SYMBOL,
        "timeframe": Config.TIMEFRAME,
        "trade_amount": Config.TRADE_AMOUNT,
        "max_open_trades": Config.MAX_OPEN_TRADES,
        "stop_loss_pct": Config.STOP_LOSS_PCT,
        "take_profit_pct": Config.TAKE_PROFIT_PCT,
        "trailing_stop_pct": Config.TRAILING_STOP_PCT,
        "fee_pct": Config.FEE_PCT,
        "use_dynamic_targets": Config.USE_DYNAMIC_TARGETS,
        "trend_filter": Config.TREND_FILTER,
        "min_ai_confidence": Config.MIN_AI_CONFIDENCE,
        "demo_mode": Config.DEMO_MODE,
        "exchange": Config.EXCHANGE,
        "ton_wallet": Config.TON_WALLET,
    })

@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.json or {}
    def num(key, lo, hi):
        """Безопасно парсит число из data[key] в диапазоне [lo, hi]; иначе None."""
        if key not in data:
            return None
        try:
            v = float(data[key])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v):
            return None
        return max(lo, min(hi, v))

    errors = []
    for key in ("trade_amount", "stop_loss_pct", "take_profit_pct",
                "max_open_trades", "trailing_stop_pct", "fee_pct", "min_ai_confidence"):
        if key in data and num(key, -1e18, 1e18) is None:
            errors.append(key)
    if errors:
        return jsonify({"ok": False, "message": "Некорректные значения: " + ", ".join(errors)}), 400

    v = num("trade_amount", 1, 1e9)
    if v is not None: Config.TRADE_AMOUNT = v
    v = num("stop_loss_pct", 0.1, 90)
    if v is not None: Config.STOP_LOSS_PCT = v
    v = num("take_profit_pct", 0.1, 1000)
    if v is not None: Config.TAKE_PROFIT_PCT = v
    v = num("max_open_trades", 1, 50)
    if v is not None: Config.MAX_OPEN_TRADES = int(v)
    v = num("trailing_stop_pct", 0, 90)
    if v is not None: Config.TRAILING_STOP_PCT = v
    v = num("fee_pct", 0, 10)
    if v is not None: Config.FEE_PCT = v
    v = num("min_ai_confidence", 0, 100)
    if v is not None: Config.MIN_AI_CONFIDENCE = v
    if "use_dynamic_targets" in data: Config.USE_DYNAMIC_TARGETS = bool(data["use_dynamic_targets"])
    if "trend_filter" in data: Config.TREND_FILTER = bool(data["trend_filter"])
    if "symbol" in data and data["symbol"] != Config.SYMBOL:
        if trader.open_trades:
            return jsonify({
                "ok": False,
                "message": "Нельзя сменить пару при открытых сделках. Сначала закройте позиции.",
            }), 409
        Config.SYMBOL = data["symbol"]
    return jsonify({"ok": True, "message": "Настройки сохранены"})

@socketio.on("connect")
def on_connect(auth=None):
    try:
        status = _safe_status()
        emit("status_update", status)
    except Exception as e:
        print(f"[on_connect] Ошибка: {e}")

if __name__ == "__main__":
    start_background()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
