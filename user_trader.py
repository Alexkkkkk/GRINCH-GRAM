"""
Менеджер пользовательских трейдеров.
Каждый зарегистрированный пользователь получает отдельный экземпляр DedustClient
и торгует на основе сигналов главного Trader.
С каждой покупки списывается 9.5% комиссии платформы → OWNER_ADDRESS.
"""
import threading
import logging
import hashlib
import base64
from datetime import datetime
from typing import Optional

from cryptography.fernet import Fernet

log = logging.getLogger(__name__)

PLATFORM_FEE_PCT = 9.5
OWNER_ADDRESS = "UQDDgb2BTM-KCjntOoUg6uHllvnu3KGqEquKw6IySVP3hDgM"


# ── Шифрование мнемоник ──────────────────────────────────────────────────────

def _make_fernet() -> Fernet:
    from config import Config
    raw = hashlib.sha256(Config.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_mnemonic(mnemonic: str) -> str:
    return _make_fernet().encrypt(mnemonic.encode()).decode()


def decrypt_mnemonic(encrypted: str) -> str:
    return _make_fernet().decrypt(encrypted.encode()).decode()


# ── Менеджер ─────────────────────────────────────────────────────────────────

class UserTradingManager:
    def __init__(self):
        self._users: dict = {}          # token → state dict
        self._lock  = threading.Lock()

    # ── Загрузка из БД ───────────────────────────────────────────────────────

    def load_from_db(self, app):
        from models import UserWallet
        with app.app_context():
            users = UserWallet.query.filter_by(active=True).all()
            for u in users:
                try:
                    mnemonic = decrypt_mnemonic(u.encrypted_mnemonic)
                    self._register(u.token, mnemonic, u.trade_amount, u.name)
                    log.info(f"[UserTrader] Загружен пользователь {u.name or u.token[:8]}")
                except Exception as e:
                    log.error(f"[UserTrader] Ошибка загрузки {u.token[:8]}: {e}")

    # ── Регистрация нового пользователя ──────────────────────────────────────

    def register(self, token: str, mnemonic: str, trade_amount: float, name: str = ""):
        self._register(token, mnemonic, trade_amount, name)

    def _register(self, token, mnemonic, trade_amount, name):
        from dedust_client import DedustClient
        client = DedustClient(mnemonic_override=mnemonic)
        state = {
            "client":         client,
            "trade_amount":   trade_amount,
            "name":           name or "Трейдер",
            "open_position":  None,
            "trades":         [],
            "logs":           [],
            "stats": {
                "total_trades":   0,
                "winning_trades": 0,
                "total_pnl_ton":  0.0,
                "total_fee_paid": 0.0,
            },
            "last_signal": "HOLD",
        }
        with self._lock:
            self._users[token] = state

    def deactivate(self, token: str):
        with self._lock:
            self._users.pop(token, None)

    # ── Обработка сигнала от главного Trader ─────────────────────────────────

    def on_signal(self, signal: str, price: float, ai: dict):
        """Вызывается главным Trader при BUY или SELL сигнале."""
        with self._lock:
            snapshot = dict(self._users)

        for token, user in snapshot.items():
            try:
                if signal == "BUY" and user["open_position"] is None:
                    threading.Thread(
                        target=self._user_buy,
                        args=(token, user, price, ai),
                        daemon=True,
                    ).start()
                elif signal == "SELL" and user["open_position"] is not None:
                    threading.Thread(
                        target=self._user_sell,
                        args=(token, user, price),
                        daemon=True,
                    ).start()
            except Exception as e:
                log.error(f"[UserTrader {token[:8]}] Ошибка dispatch: {e}")

    # ── Покупка ──────────────────────────────────────────────────────────────

    def _user_buy(self, token, user, price, ai):
        trade_amount = user["trade_amount"]
        fee_ton  = round(trade_amount * PLATFORM_FEE_PCT / 100, 6)
        net_ton  = round(trade_amount - fee_ton, 6)

        self._log(user, f"💸 Комиссия {fee_ton:.4f} TON → платформа (9.5%)", "INFO")

        # 1. Отправить комиссию владельцу
        fee_res = user["client"].send_ton(OWNER_ADDRESS, fee_ton)
        if fee_res.get("ok"):
            user["stats"]["total_fee_paid"] += fee_ton
            self._log(user, f"✅ Комиссия {fee_ton:.4f} TON отправлена", "INFO")
        else:
            self._log(user, f"⚠️ Комиссия не отправлена: {fee_res.get('error')}", "WARN")

        # 2. Купить GRINCH
        self._log(user, f"🟢 BUY {net_ton:.4f} TON → GRINCH @ {price}", "BUY")
        res = user["client"].buy(net_ton)

        if res.get("ok"):
            est_grinch = net_ton / price if price else 0
            user["open_position"] = {
                "ton_spent":       net_ton,
                "fee_paid":        fee_ton,
                "entry_price":     price,
                "est_grinch":      est_grinch,
                "entry_time":      datetime.utcnow().isoformat(),
                "ai_confidence":   ai.get("confidence", 0),
            }
            user["stats"]["total_trades"] += 1
            user["last_signal"] = "BUY"
            self._log(user, f"✅ Куплено ~{est_grinch:.0f} GRINCH", "INFO")
            self._sync_db(token, user)
        else:
            self._log(user, f"❌ Покупка провалилась: {res.get('error')}", "ERROR")

    # ── Продажа ──────────────────────────────────────────────────────────────

    def _user_sell(self, token, user, price):
        pos = user.get("open_position")
        if not pos:
            return

        grinch = pos["est_grinch"]
        self._log(user, f"🔴 SELL ~{grinch:.0f} GRINCH @ {price}", "SELL")
        res = user["client"].sell(grinch)

        if res.get("ok"):
            received = grinch * price
            pnl = round(received - pos["ton_spent"] - pos["fee_paid"], 6)
            user["stats"]["total_pnl_ton"] += pnl
            if pnl > 0:
                user["stats"]["winning_trades"] += 1

            user["trades"].append({
                "buy_price":  pos["entry_price"],
                "sell_price": price,
                "ton_spent":  pos["ton_spent"],
                "fee_paid":   pos["fee_paid"],
                "pnl_ton":    pnl,
                "time":       datetime.utcnow().isoformat(),
            })
            if len(user["trades"]) > 50:
                user["trades"] = user["trades"][-50:]

            user["open_position"] = None
            user["last_signal"]   = "SELL"
            sign = "+" if pnl >= 0 else ""
            self._log(user, f"✅ Продано | PNL: {sign}{pnl:.4f} TON", "INFO")
            self._sync_db(token, user)
        else:
            self._log(user, f"❌ Продажа провалилась: {res.get('error')}", "ERROR")

    # ── API: статус пользователя ──────────────────────────────────────────────

    def get_status(self, token: str) -> Optional[dict]:
        with self._lock:
            u = self._users.get(token)
        if not u:
            return None
        s = u["stats"]
        wrate = 0
        if s["total_trades"] > 0:
            wrate = round(s["winning_trades"] / s["total_trades"] * 100, 1)
        return {
            "name":          u["name"],
            "trade_amount":  u["trade_amount"],
            "open_position": u["open_position"],
            "recent_trades": u["trades"][-20:],
            "logs":          u["logs"][-40:],
            "stats":         {**s, "winrate": wrate},
            "last_signal":   u["last_signal"],
            "ready":         u["client"].ready,
            "error":         u["client"].error,
        }

    def get_balance(self, token: str) -> Optional[dict]:
        with self._lock:
            u = self._users.get(token)
        if not u:
            return None
        return u["client"].get_balance()

    def count_active(self) -> int:
        with self._lock:
            return len(self._users)

    # ── Вспомогательные ──────────────────────────────────────────────────────

    def _log(self, user, msg, level="INFO"):
        entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "level": level, "msg": msg}
        user["logs"].append(entry)
        if len(user["logs"]) > 100:
            user["logs"] = user["logs"][-100:]
        log.info(f"[UserTrader] {msg}")

    def _sync_db(self, token, user):
        """Сохраняет статистику в БД (не блокирует торговлю)."""
        try:
            from app import app as flask_app
            from models import UserWallet
            from database import db
            with flask_app.app_context():
                uw = UserWallet.query.filter_by(token=token).first()
                if uw:
                    s = user["stats"]
                    uw.total_trades   = s["total_trades"]
                    uw.winning_trades = s["winning_trades"]
                    uw.total_fee_paid = s["total_fee_paid"]
                    uw.total_pnl_ton  = s["total_pnl_ton"]
                    uw.last_signal_at = datetime.utcnow()
                    db.session.commit()
        except Exception as e:
            log.debug(f"[UserTrader] _sync_db ошибка: {e}")
