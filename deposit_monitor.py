"""
Мониторинг депозитов на платформенный кошелёк.
Проверяет TonCenter API каждые 120 секунд (без ключа) или 30 секунд (с ключом).
Когда в поле comment транзакции найден код пользователя (GG-XXXXXXXX),
зачисляет сумму на его виртуальный баланс.

Оба опросчика (TONTracker и DepositMonitor) намеренно расфазированы:
TONTracker стартует через 5s, DepositMonitor через 65s — чтобы не совпадать
и не нарушать rate-limit TonCenter (~1 req/s бесплатно).
"""
import os
import threading
import logging
import time
import urllib.request
import urllib.parse
import json

log = logging.getLogger(__name__)


class DepositMonitor:
    TONCENTER    = "https://toncenter.com/api/v2"
    BACKOFF_MAX  = 600
    START_DELAY  = 65    # расфазировка с TONTracker (тот стартует через 5s)

    @property
    def POLL_SEC(self):
        """30s с ключом, 120s без — чтобы не жечь бесплатный rate-limit."""
        return 30 if os.getenv("TONCENTER_API_KEY") else 120

    def __init__(self, platform_address: str):
        self.address  = platform_address
        self._running = False
        self._backoff = 0

    def start(self, app, user_mgr):
        self._app      = app
        self._user_mgr = user_mgr
        self._running  = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        log.info(f"[DepositMonitor] Запущен. Наблюдение за {self.address[:20]}...")

    def _loop(self):
        time.sleep(self.START_DELAY)   # ждём расфазировки
        while self._running:
            try:
                self._check()
            except Exception as e:
                log.debug(f"[DepositMonitor] Ошибка: {e}")
            if self._backoff > 0:
                time.sleep(self._backoff)
                self._backoff = 0
            else:
                time.sleep(self.POLL_SEC)

    def _get(self, path: str, params: dict):
        qs  = urllib.parse.urlencode(params)
        url = f"{self.TONCENTER}/{path}?{qs}"
        req = urllib.request.Request(url)
        _key = os.getenv("TONCENTER_API_KEY", "")
        if _key:
            req.add_header("X-API-Key", _key)
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                self._backoff = 0
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self._backoff = min(max(self._backoff * 2, 60), self.BACKOFF_MAX)
                log.warning(f"[DepositMonitor] 429 Rate limit — пауза {self._backoff}s")
            else:
                log.debug(f"[DepositMonitor] HTTP {e.code}")
            return None
        except Exception as e:
            log.debug(f"[DepositMonitor] Ошибка запроса: {e}")
            return None

    def _check(self):
        data = self._get("getTransactions", {"address": self.address, "limit": 50})
        if data is None:
            return
        txs = data.get("result", [])
        if not txs:
            return

        # TonCenter отдаёт транзакции новейшими первыми. Обрабатываем по
        # возрастанию lt, чтобы несколько депозитов одного пользователя в одном
        # окне опроса зачислились ВСЕ по порядку: last_checked_lt растёт
        # монотонно и не «перепрыгивает» старые валидные депозиты.
        txs = sorted(txs, key=lambda t: int(t.get("transaction_id", {}).get("lt", 0)))

        with self._app.app_context():
            for tx in txs:
                try:
                    self._process_tx(tx)
                except Exception as e:
                    log.debug(f"[DepositMonitor] tx ошибка: {e}")

    def _process_tx(self, tx):
        lt      = int(tx.get("transaction_id", {}).get("lt", 0))
        in_msg  = tx.get("in_msg", {})
        source  = in_msg.get("source", "")
        value   = int(in_msg.get("value", 0))
        comment = (in_msg.get("message") or in_msg.get("comment") or "").strip()

        if not source or value <= 0 or not comment:
            return
        if not comment.upper().startswith("GG-"):
            return

        code = comment[3:].strip().lower()

        from models import UserWallet
        from database import db
        uw = UserWallet.query.filter(
            UserWallet.token.like(f"{code}%"),
            UserWallet.active == True
        ).first()
        if not uw:
            return

        last_lt = uw.last_checked_lt or 0
        if lt <= last_lt:
            return

        amount_ton = value / 1_000_000_000
        if amount_ton < 0.01:
            return

        log.info(f"[DepositMonitor] Депозит {amount_ton:.4f} TON от {source[:16]}… → {uw.name or uw.token[:8]}")
        self._user_mgr.credit_deposit(uw.token, amount_ton, self._app)

        uw.last_checked_lt = lt
        db.session.commit()
