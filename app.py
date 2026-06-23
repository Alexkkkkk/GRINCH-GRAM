from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import threading
import time
from config import Config
from trader import Trader

app = Flask(__name__)
app.config["SECRET_KEY"] = Config.SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

trader = Trader()

def push_updates():
    while True:
        try:
            status = trader.get_status()
            socketio.emit("status_update", status)
        except Exception as e:
            print(f"[Push] Ошибка: {e}")
        time.sleep(5)

@app.route("/")
def index():
    return render_template("index.html", symbol=Config.SYMBOL, demo=Config.DEMO_MODE)

@app.route("/api/status")
def api_status():
    return jsonify(trader.get_status())

@app.route("/api/start", methods=["POST"])
def api_start():
    trader.start()
    return jsonify({"ok": True, "message": "Агент запущен"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    trader.stop()
    return jsonify({"ok": True, "message": "Агент остановлен"})

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
    })

@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.json or {}
    if "trade_amount" in data:
        Config.TRADE_AMOUNT = float(data["trade_amount"])
    if "stop_loss_pct" in data:
        Config.STOP_LOSS_PCT = float(data["stop_loss_pct"])
    if "take_profit_pct" in data:
        Config.TAKE_PROFIT_PCT = float(data["take_profit_pct"])
    if "max_open_trades" in data:
        Config.MAX_OPEN_TRADES = int(data["max_open_trades"])
    if "symbol" in data:
        Config.SYMBOL = data["symbol"]
    return jsonify({"ok": True, "message": "Настройки сохранены"})

@socketio.on("connect")
def on_connect(auth=None):
    status = trader.get_status()
    emit("status_update", status)

if __name__ == "__main__":
    t = threading.Thread(target=push_updates, daemon=True)
    t.start()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
