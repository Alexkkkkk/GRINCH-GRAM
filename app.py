import json
import numpy as np
from flask import Flask, render_template, jsonify, request
from flask.json.provider import DefaultJSONProvider
from flask_socketio import SocketIO, emit
import threading
import time
from config import Config
from trader import Trader
from ton_tracker import TONTracker


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
        ton.start()


# Запуск при импорте (gunicorn выполняет app:app, не заходя в __main__)
start_background()


@app.route("/")
def index():
    return render_template("index.html", symbol=Config.SYMBOL, demo=Config.DEMO_MODE)

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

@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify({
        "symbol": Config.SYMBOL,
        "timeframe": Config.TIMEFRAME,
        "trade_amount": Config.TRADE_AMOUNT,
        "max_open_trades": Config.MAX_OPEN_TRADES,
        "stop_loss_pct": Config.STOP_LOSS_PCT,
        "take_profit_pct": Config.TAKE_PROFIT_PCT,
        "demo_mode": Config.DEMO_MODE,
        "exchange": Config.EXCHANGE,
        "ton_wallet": Config.TON_WALLET,
    })

@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.json or {}
    if "trade_amount"    in data: Config.TRADE_AMOUNT    = float(data["trade_amount"])
    if "stop_loss_pct"   in data: Config.STOP_LOSS_PCT   = float(data["stop_loss_pct"])
    if "take_profit_pct" in data: Config.TAKE_PROFIT_PCT = float(data["take_profit_pct"])
    if "max_open_trades" in data: Config.MAX_OPEN_TRADES = int(data["max_open_trades"])
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
