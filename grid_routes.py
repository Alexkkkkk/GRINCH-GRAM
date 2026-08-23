"""Flask routes for Binance Spot Grid Trading."""

from flask import Blueprint, jsonify, render_template, request

grid_bp = Blueprint("grid", __name__, url_prefix="/grid")
_engine = None
_db = None


def get_engine():
    global _engine
    if _engine is None:
        from binance_grid_engine import GridTradingEngine

        _engine = GridTradingEngine()
    return _engine


def get_db():
    global _db
    if _db is None:
        from grid_db import GridDatabase

        _db = GridDatabase()
    return _db


@grid_bp.route("/")
def grid_dashboard():
    return render_template("grid_dashboard.html")


@grid_bp.route("/api/status")
def api_status():
    return jsonify(get_engine().get_status())


@grid_bp.route("/api/start", methods=["POST"])
def api_start():
    try:
        if not get_engine().is_running():
            get_engine().start()
        return jsonify({"ok": True, "active": get_engine().is_running()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@grid_bp.route("/api/stop", methods=["POST"])
def api_stop():
    try:
        get_engine().stop()
        return jsonify({"ok": True, "active": get_engine().is_running()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@grid_bp.route("/api/build", methods=["POST"])
def api_build():
    try:
        data = request.get_json() or {}
        result = get_engine().build_grid(
            upper=data.get("upper"),
            lower=data.get("lower"),
            grid_count=data.get("grid_count"),
            investment=data.get("investment"),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@grid_bp.route("/api/history")
def api_history():
    hours = request.args.get("hours", 24, type=int)
    pnl = get_db().get_pnl_history(get_engine().symbol, hours)
    trades = get_db().get_trades(get_engine().symbol, 100)
    return jsonify({"pnl": pnl, "trades": trades})


@grid_bp.route("/api/stats")
def api_stats():
    return jsonify(get_db().get_stats(get_engine().symbol))
