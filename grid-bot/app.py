#!/usr/bin/env python3
"""Flask API + WebSocket for Grid Trading Bot with GitHub error reporting."""
import logging
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO
from flask_cors import CORS

from config import Config
from grid_engine import GridTradingEngine
from database import GridDatabase
from github_reporter import get_reporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("app")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = Config.SECRET_KEY
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

engine = GridTradingEngine()
db = GridDatabase()
reporter = get_reporter()


@app.errorhandler(Exception)
def handle_exception(e):
    log.error("Unhandled exception: %s", e)
    if Config.REPORT_ERRORS:
        try:
            reporter.report_exception(e, context="Flask app unhandled exception")
        except Exception as rep_err:
            log.error("Failed to report to GitHub: %s", rep_err)
    return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    return render_template("grid_dashboard.html")


@app.route("/api/status")
def api_status():
    return jsonify(engine.get_status())


@app.route("/api/start", methods=["POST"])
def api_start():
    try:
        if not engine.is_running():
            engine.start()
        return jsonify({"ok": True, "active": engine.is_running()})
    except Exception as e:
        log.error("Start failed: %s", e)
        if Config.REPORT_ERRORS:
            reporter.report_exception(e, context="API /api/start")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    try:
        engine.stop()
        return jsonify({"ok": True, "active": engine.is_running()})
    except Exception as e:
        log.error("Stop failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/build", methods=["POST"])
def api_build():
    try:
        data = request.get_json() or {}
        result = engine.build_grid(
            upper=data.get("upper"),
            lower=data.get("lower"),
            grid_count=data.get("grid_count"),
            investment=data.get("investment")
        )
        return jsonify(result)
    except Exception as e:
        log.error("Build failed: %s", e)
        if Config.REPORT_ERRORS:
            reporter.report_exception(e, context="API /api/build")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/history")
def api_history():
    hours = request.args.get("hours", 24, type=int)
    pnl = db.get_pnl_history(engine.symbol, hours)
    trades = db.get_trades(engine.symbol, 100)
    return jsonify({"pnl": pnl, "trades": trades})


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats(engine.symbol))


@socketio.on("connect")
def ws_connect():
    socketio.emit("status", engine.get_status())


def broadcast_loop():
    import time
    while True:
        time.sleep(5)
        try:
            socketio.emit("status", engine.get_status())
        except Exception as e:
            log.debug("Broadcast error: %s", e)


if __name__ == "__main__":
    import threading
    t = threading.Thread(target=broadcast_loop, daemon=True)
    t.start()
    socketio.run(app, host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
