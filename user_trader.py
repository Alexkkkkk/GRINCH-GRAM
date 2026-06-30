"""
Мультипользовательский виртуальный трейдинг.
Пользователи подключают кошельки через TonConnect (без мнемоники).
Депонируют TON на платформенный кошелёк.
Платформа торгует своим кошельком; пользователи получают
пропорциональную долю прибыли (минус 9.5% комиссии платформы).
"""
import threading
import logging
import hashlib
import base64
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

PLATFORM_FEE_PCT = 9.5
OWNER_ADDRESS = "UQDDgb2BTM-KCjntOoUg6uHllvnu3KGqEquKw6IySVP3hDgM"


# ── Устаревшее шифрование (для совместимости с legacy-аккаунтами) ─────────────

def encrypt_mnemonic(mnemonic: str) -> str:
    try:
        from cryptography.fernet import Fernet
        from config import Config
        raw = hashlib.sha256(Config.SECRET_KEY.encode()).digest()
        f = Fernet(base64.urlsafe_b64encode(raw))
        return f.encrypt(mnemonic.encode()).decode()
    except Exception:
        return base64.urlsafe_b64encode(mnemonic.encode()).decode()


def decrypt_mnemonic(encrypted: str) -> str:
    try:
        from cryptography.fernet import Fernet
        from config import Config
        raw = hashlib.sha256(Config.SECRET_KEY.encode()).digest()
        f = Fernet(base64.urlsafe_b64encode(raw))
        return f.decrypt(encrypted.encode()).decode()
    except Exception:
        return base64.urlsafe_b64decode(encrypted.encode()).decode()


# ── Менеджер ─────────────────────────────────────────────────────────────────

class UserTradingManager:
    """
    Хранит состояние виртуальных позиций пользователей.
    Реальные сделки совершаются платформенным Trader; здесь
    ведётся виртуальное зеркало для каждого пользователя.
    """

    def __init__(self):
        self._users: dict = {}          # token → state dict
        self._lock  = threading.Lock()

    # ── Загрузка из БД ───────────────────────────────────────────────────────

    def load_from_db(self, app):
        from models import UserWallet
        with app.app_context():
            users = UserWallet.query.filter_by(active=True).all()
            for u in users:
                self._restore(u)
                log.info(f"[UserTrader] Загружен: {u.name or u.token[:8]} "
                         f"({u.virtual_ton_balance:.3f} TON)")

    def _restore(self, u):
        """Восстановить состояние из БД-записи."""
        with self._lock:
            self._users[u.token] = {
                "name":          u.name or "Трейдер",
                "ton_address":   u.ton_address,
                "trade_amount":  u.trade_amount,
                "balance_ton":   u.virtual_ton_balance,
                "grinch_held":   u.virtual_grinch_held,
                "entry_price":   u.entry_price_ton,
                "trades":        [],
                "logs":          [],
                "stats": {
                    "total_trades":   u.total_trades,
                    "winning_trades": u.winning_trades,
                    "total_pnl_ton":  u.total_pnl_ton,
                    "total_fee_paid": u.total_fee_paid,
                },
                "last_signal": "HOLD",
            }

    # ── Регистрация нового пользователя ──────────────────────────────────────

    def register(self, token: str, ton_address: str, trade_amount: float, name: str = ""):
        with self._lock:
            self._users[token] = {
                "name":         name or "Трейдер",
                "ton_address":  ton_address,
                "trade_amount": trade_amount,
                "balance_ton":  0.0,
                "grinch_held":  0.0,
                "entry_price":  None,
                "trades":       [],
                "logs":         [],
                "stats": {
                    "total_trades":   0,
                    "winning_trades": 0,
                    "total_pnl_ton":  0.0,
                    "total_fee_paid": 0.0,
                },
                "last_signal": "HOLD",
            }

    def deactivate(self, token: str):
        with self._lock:
            self._users.pop(token, None)

    # ── Депозит ──────────────────────────────────────────────────────────────

    def credit_deposit(self, token: str, amount_ton: float, app=None):
        """Зачислить TON после подтверждения депозита."""
        with self._lock:
            u = self._users.get(token)
            if not u:
                return False
            u["balance_ton"] = round(u["balance_ton"] + amount_ton, 6)
            self._log(u, f"💰 Депозит {amount_ton:.4f} TON зачислен (баланс: {u['balance_ton']:.4f} TON)", "INFO")

        if app:
            try:
                from models import UserWallet
                from database import db
                with app.app_context():
                    uw = UserWallet.query.filter_by(token=token).first()
                    if uw:
                        uw.virtual_ton_balance = u["balance_ton"]
                        uw.total_deposited = (uw.total_deposited or 0) + amount_ton
                        uw.last_deposit_at = datetime.utcnow()
                        db.session.commit()
            except Exception as e:
                log.error(f"[UserTrader] credit_deposit DB ошибка: {e}")
        return True

    # ── Вывод ────────────────────────────────────────────────────────────────

    def withdraw(self, token: str, amount_ton: float, app=None) -> dict:
        """Вывести TON с виртуального баланса пользователя на его кошелёк."""
        with self._lock:
            u = self._users.get(token)
            if not u:
                return {"ok": False, "error": "Пользователь не найден"}
            if amount_ton > u["balance_ton"]:
                return {"ok": False, "error": f"Недостаточно средств (доступно {u['balance_ton']:.4f} TON)"}
            if u.get("grinch_held", 0) > 0:
                return {"ok": False, "error": "Нельзя вывести во время открытой позиции — дождитесь SELL"}

        # Отправить TON с платформенного кошелька на кошелёк пользователя
        try:
            from dedust_client import dedust_client
            res = dedust_client.send_ton(u["ton_address"], amount_ton)
            if not res.get("ok"):
                return {"ok": False, "error": f"Ошибка отправки: {res.get('error')}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        with self._lock:
            u["balance_ton"] = round(u["balance_ton"] - amount_ton, 6)
            self._log(u, f"📤 Вывод {amount_ton:.4f} TON → {u['ton_address'][:16]}...", "INFO")

        if app:
            try:
                from models import UserWallet
                from database import db
                with app.app_context():
                    uw = UserWallet.query.filter_by(token=token).first()
                    if uw:
                        uw.virtual_ton_balance  = u["balance_ton"]
                        uw.total_withdrawn = (uw.total_withdrawn or 0) + amount_ton
                        db.session.commit()
            except Exception as e:
                log.error(f"[UserTrader] withdraw DB ошибка: {e}")

        return {"ok": True, "amount": amount_ton}

    # ── Сигнал от главного Trader ─────────────────────────────────────────────

    def on_signal(self, signal: str, price: float, ai: dict):
        """Вызывается главным Trader при BUY / SELL."""
        with self._lock:
            snapshot = dict(self._users)

        for token, user in snapshot.items():
            try:
                if signal == "BUY" and not user.get("grinch_held"):
                    threading.Thread(
                        target=self._virtual_buy,
                        args=(token, user, price, ai),
                        daemon=True,
                    ).start()
                elif signal == "SELL" and user.get("grinch_held", 0) > 0:
                    threading.Thread(
                        target=self._virtual_sell,
                        args=(token, user, price),
                        daemon=True,
                    ).start()
            except Exception as e:
                log.error(f"[UserTrader {token[:8]}] dispatch: {e}")

    # ── Виртуальная покупка ───────────────────────────────────────────────────

    def _virtual_buy(self, token, user, price, ai):
        trade_amount = min(user["trade_amount"], user.get("balance_ton", 0))
        if trade_amount < 0.1:
            self._log(user, f"⏸️ Пропуск BUY — баланс {user.get('balance_ton',0):.4f} TON (нужно мин. 0.1)", "WARN")
            return

        fee_ton = round(trade_amount * PLATFORM_FEE_PCT / 100, 6)
        net_ton = round(trade_amount - fee_ton, 6)

        # Используем реальный курс пула TON→GRINCH, а не перекрёстный USD-курс
        from price_feed import price_feed
        ton_per_grinch = price_feed.get_grinch_ton_price()
        if ton_per_grinch and ton_per_grinch > 0:
            est_grinch = net_ton / ton_per_grinch
        else:
            # Фоллбэк: пересчёт через USD-цены
            ton_usd = price_feed.get("TON") or 2.44
            est_grinch = (net_ton * ton_usd / price) if price and price > 0 else 0

        user["balance_ton"]  = round(user.get("balance_ton", 0) - trade_amount, 6)
        user["grinch_held"]  = est_grinch
        user["entry_price"]  = price
        user["last_signal"]  = "BUY"
        user["stats"]["total_trades"]   += 1
        user["stats"]["total_fee_paid"] += fee_ton

        self._log(user, f"🟢 BUY {net_ton:.4f} TON → ~{est_grinch:.0f} GRINCH @ {price} | Комиссия: {fee_ton:.4f} TON", "BUY")

        user["trades"].append({
            "type":        "buy",
            "price":       price,
            "ton_spent":   net_ton,
            "fee":         fee_ton,
            "grinch":      est_grinch,
            "ai_conf":     ai.get("confidence", 0),
            "time":        datetime.utcnow().isoformat(),
        })
        if len(user["trades"]) > 50:
            user["trades"] = user["trades"][-50:]

        self._sync_db(token, user)

    # ── Виртуальная продажа ───────────────────────────────────────────────────

    def _virtual_sell(self, token, user, price):
        grinch = user.get("grinch_held", 0)
        if not grinch:
            return

        # Конвертируем GRINCH обратно в TON по курсу пула, а не USD-цене
        from price_feed import price_feed
        ton_per_grinch = price_feed.get_grinch_ton_price()
        if ton_per_grinch and ton_per_grinch > 0:
            received_ton = grinch * ton_per_grinch
        else:
            # Фоллбэк: конвертация через USD-цены
            ton_usd = price_feed.get("TON") or 2.44
            received_ton = (grinch * price / ton_usd) if ton_usd > 0 else grinch * price * 0.41

        entry = user.get("entry_price") or price
        pnl   = round(received_ton - (grinch * entry) - user["stats"].get("last_buy_fee", 0), 6)

        # Find last buy to calc correct PnL
        for t in reversed(user.get("trades", [])):
            if t["type"] == "buy":
                invested  = t["ton_spent"] + t["fee"]
                pnl       = round(received_ton - invested, 6)
                break

        user["balance_ton"] = round(user.get("balance_ton", 0) + received_ton, 6)
        user["grinch_held"] = 0
        user["entry_price"] = None
        user["last_signal"] = "SELL"
        user["stats"]["total_pnl_ton"] = round(
            user["stats"].get("total_pnl_ton", 0) + pnl, 6
        )
        if pnl > 0:
            user["stats"]["winning_trades"] += 1

        sign = "+" if pnl >= 0 else ""
        self._log(user, f"🔴 SELL ~{grinch:.0f} GRINCH @ {price} | PNL: {sign}{pnl:.4f} TON", "SELL")

        user["trades"].append({
            "type":     "sell",
            "price":    price,
            "grinch":   grinch,
            "received": received_ton,
            "pnl_ton":  pnl,
            "time":     datetime.utcnow().isoformat(),
        })
        if len(user["trades"]) > 50:
            user["trades"] = user["trades"][-50:]

        self._sync_db(token, user)

    # ── API ───────────────────────────────────────────────────────────────────

    def get_status(self, token: str) -> Optional[dict]:
        with self._lock:
            u = self._users.get(token)
        if not u:
            return None
        s  = u["stats"]
        wt = s.get("winning_trades", 0)
        tt = s.get("total_trades", 0)
        return {
            "name":           u["name"],
            "ton_address":    u.get("ton_address", ""),
            "trade_amount":   u["trade_amount"],
            "balance_ton":    round(u.get("balance_ton", 0), 6),
            "grinch_held":    round(u.get("grinch_held", 0), 2),
            "entry_price":    u.get("entry_price"),
            "recent_trades":  u.get("trades", [])[-20:],
            "logs":           u.get("logs", [])[-40:],
            "stats":          {**s, "winrate": round(wt / tt * 100, 1) if tt else 0},
            "last_signal":    u.get("last_signal", "HOLD"),
        }

    def count_active(self) -> int:
        with self._lock:
            return len([u for u in self._users.values() if u.get("balance_ton", 0) > 0])

    # ── Вспомогательные ──────────────────────────────────────────────────────

    def _log(self, user, msg, level="INFO"):
        entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "level": level, "msg": msg}
        user.setdefault("logs", []).append(entry)
        if len(user["logs"]) > 100:
            user["logs"] = user["logs"][-100:]
        log.info(f"[UserTrader] {msg}")

    def _sync_db(self, token, user):
        try:
            from app import app as flask_app
            from models import UserWallet
            from database import db
            with flask_app.app_context():
                uw = UserWallet.query.filter_by(token=token).first()
                if uw:
                    s = user["stats"]
                    uw.virtual_ton_balance = user.get("balance_ton", 0)
                    uw.virtual_grinch_held = user.get("grinch_held", 0)
                    uw.entry_price_ton     = user.get("entry_price")
                    uw.total_trades        = s.get("total_trades", 0)
                    uw.winning_trades      = s.get("winning_trades", 0)
                    uw.total_fee_paid      = s.get("total_fee_paid", 0)
                    uw.total_pnl_ton       = s.get("total_pnl_ton", 0)
                    uw.last_signal_at      = datetime.utcnow()
                    db.session.commit()
        except Exception as e:
            log.debug(f"[UserTrader] _sync_db: {e}")
