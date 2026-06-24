"""
Мониторинг депозитов на платформенный кошелёк.
Проверяет TonCenter API каждые 60 секунд.
Когда в поле comment транзакции найден код пользователя (GG-XXXXXXXX),
зачисляет сумму на его виртуальный баланс.
"""
import threading
import logging
import time
import urllib.request
import json

log = logging.getLogger(__name__)


class DepositMonitor:
    TONCENTER = "https://toncenter.com/api/v2"
    POLL_SEC  = 60

    def __init__(self, platform_address: str):
        self.address  = platform_address
        self._running = False
        self._last_lt: dict = {}   # token → last processed lt (BigInt)

    def start(self, app, user_mgr):
        self._app      = app
        self._user_mgr = user_mgr
        self._running  = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        log.info(f"[DepositMonitor] Наблюдение за {self.address[:20]}...")

    def _loop(self):
        while self._running:
            try:
                self._check()
            except Exception as e:
                log.debug(f"[DepositMonitor] Ошибка: {e}")
            time.sleep(self.POLL_SEC)

    def _check(self):
        from models import UserWallet
        url = (f"{self.TONCENTER}/getTransactions"
               f"?address={self.address}&limit=50&archival=false")
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
        except Exception as e:
            log.debug(f"[DepositMonitor] API ошибка: {e}")
            return

        txs = data.get("result", [])
        if not txs:
            return

        with self._app.app_context():
            for tx in txs:
                try:
                    self._process_tx(tx)
                except Exception as e:
                    log.debug(f"[DepositMonitor] tx ошибка: {e}")

    def _process_tx(self, tx):
        lt = int(tx.get("transaction_id", {}).get("lt", 0))
        in_msg = tx.get("in_msg", {})

        # Only incoming TON transfers
        source  = in_msg.get("source", "")
        value   = int(in_msg.get("value", 0))
        comment = (in_msg.get("message") or in_msg.get("comment") or "").strip()
        if not source or value <= 0 or not comment:
            return

        # Find user by code: comment starts with "GG-" + token prefix
        if not comment.upper().startswith("GG-"):
            return

        code = comment[3:].strip().lower()  # 8-char token prefix

        from models import UserWallet
        from database import db
        uw = UserWallet.query.filter(
            UserWallet.token.like(f"{code}%"),
            UserWallet.active == True
        ).first()
        if not uw:
            return

        # Check if already processed (lt must be newer than last_checked_lt)
        last_lt = uw.last_checked_lt or 0
        if lt <= last_lt:
            return

        amount_ton = value / 1_000_000_000
        if amount_ton < 0.01:
            return

        log.info(f"[DepositMonitor] Депозит {amount_ton:.4f} TON от {source[:16]}... → {uw.name or uw.token[:8]}")

        # Credit virtual balance
        self._user_mgr.credit_deposit(uw.token, amount_ton, self._app)

        # Update last_checked_lt
        uw.last_checked_lt = lt
        db.session.commit()
