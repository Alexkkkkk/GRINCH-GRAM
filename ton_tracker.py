import os
import time
import threading
import requests


class TONTracker:
    """Отслеживает входящие TON-транзакции на кошелёк через публичный API toncenter.com."""

    API_BASE = "https://toncenter.com/api/v2"

    def __init__(self, address: str, poll_interval: int = 30):
        self.address = address
        self.poll_interval = poll_interval
        self.api_key = os.getenv("TONCENTER_API_KEY", "")
        self._lock = threading.Lock()
        self._deposits = []          # список входящих переводов
        self._total_received = 0.0   # сумма всех входящих в TON
        self._balance = 0.0          # текущий баланс кошелька в TON
        self._last_error = None
        self._last_update = 0
        self._running = False
        self._thread = None

    # ── Публичные методы ──────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def get_data(self):
        with self._lock:
            return {
                "address": self.address,
                "balance": round(self._balance, 4),
                "total_received": round(self._total_received, 4),
                "deposits": list(self._deposits),
                "deposit_count": len(self._deposits),
                "last_update": self._last_update,
                "last_error": self._last_error,
                "configured": bool(self.address),
            }

    # ── Внутреннее ────────────────────────────────────────────────
    def _headers(self):
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _nano_to_ton(self, nano):
        try:
            return int(nano) / 1e9
        except (ValueError, TypeError):
            return 0.0

    def refresh(self):
        """Один цикл опроса: баланс + последние входящие транзакции."""
        try:
            # Баланс кошелька
            br = requests.get(
                f"{self.API_BASE}/getAddressBalance",
                params={"address": self.address},
                headers=self._headers(),
                timeout=15,
            )
            br.raise_for_status()
            bjson = br.json()
            balance = self._nano_to_ton(bjson.get("result", 0))

            # Последние транзакции
            tr = requests.get(
                f"{self.API_BASE}/getTransactions",
                params={"address": self.address, "limit": 25},
                headers=self._headers(),
                timeout=15,
            )
            tr.raise_for_status()
            txs = tr.json().get("result", [])

            deposits = []
            total = 0.0
            for tx in txs:
                in_msg = tx.get("in_msg", {}) or {}
                value = self._nano_to_ton(in_msg.get("value", 0))
                source = in_msg.get("source", "")
                # Только значимые входящие переводы (отфильтровываем dust/спам < 0.001 TON)
                if value >= 0.001 and source:
                    comment = ""
                    msg_data = in_msg.get("message", "")
                    if isinstance(msg_data, str):
                        comment = msg_data
                    deposits.append({
                        "amount": round(value, 4),
                        "from": source,
                        "from_short": source[:6] + "..." + source[-4:] if len(source) > 12 else source,
                        "comment": comment,
                        "time": int(tx.get("utime", 0)),
                        "hash": (tx.get("transaction_id", {}) or {}).get("hash", ""),
                    })
                    total += value

            with self._lock:
                self._balance = balance
                self._deposits = deposits
                self._total_received = total
                self._last_update = int(time.time())
                self._last_error = None
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
                self._last_update = int(time.time())

    def _loop(self):
        while self._running:
            if self.address:
                self.refresh()
            time.sleep(self.poll_interval)
