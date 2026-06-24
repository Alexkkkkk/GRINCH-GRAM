import os
import time
import threading
import urllib.request
import urllib.parse
import json
import logging

log = logging.getLogger(__name__)


class TONTracker:
    """Отслеживает входящие TON-транзакции через публичный TonCenter API.

    Без API-ключа TonCenter допускает ~1 req/s. Два запроса на каждый цикл —
    баланс + транзакции. Интервал 120s даёт большой запас.
    """

    API_BASE     = "https://toncenter.com/api/v2"
    POLL_DEFAULT = 120   # секунд между циклами (без API-ключа)
    POLL_MIN     = 60    # минимум при наличии ключа
    BACKOFF_MAX  = 600   # максимальный backoff при 429 (10 мин)

    def __init__(self, address: str):
        self.address      = address
        self.api_key      = os.getenv("TONCENTER_API_KEY", "")
        self.poll_interval= self.POLL_MIN if self.api_key else self.POLL_DEFAULT

        self._lock        = threading.Lock()
        self._deposits    : list = []
        self._total_received = 0.0
        self._balance     = 0.0
        self._last_error  = None
        self._last_update = 0
        self._running     = False
        self._thread      = None
        self._backoff     = 0       # текущий backoff в секундах (при 429)

    # ── Публичные методы ──────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def get_data(self) -> dict:
        with self._lock:
            return {
                "address":       self.address,
                "balance":       round(self._balance, 4),
                "total_received": round(self._total_received, 4),
                "deposits":      list(self._deposits),
                "deposit_count": len(self._deposits),
                "last_update":   self._last_update,
                "last_error":    self._last_error,
                "configured":    bool(self.address),
            }

    def refresh(self):
        """Принудительное обновление (с уважением к backoff)."""
        if self._backoff > 0:
            log.debug(f"[TONTracker] backoff активен ({self._backoff}s) — пропускаем refresh")
            return
        self._do_refresh()

    # ── Внутреннее ────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _get(self, path: str, params: dict) -> dict | None:
        """HTTP GET с обработкой 429 и общих ошибок."""
        qs  = urllib.parse.urlencode(params)
        url = f"{self.API_BASE}/{path}?{qs}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                self._backoff = 0   # успех — сбрасываем
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Exponential backoff: 60 → 120 → 240 → 480 → 600
                self._backoff = min(max(self._backoff * 2, 60), self.BACKOFF_MAX)
                log.warning(f"[TONTracker] 429 Rate limit — ждём {self._backoff}s")
                with self._lock:
                    self._last_error = f"TonCenter rate limit (429) — пауза {self._backoff}s"
            else:
                log.warning(f"[TONTracker] HTTP {e.code}: {e}")
                with self._lock:
                    self._last_error = f"HTTP {e.code}"
            return None
        except Exception as e:
            log.debug(f"[TONTracker] Ошибка: {e}")
            with self._lock:
                self._last_error = str(e)
            return None

    def _nano_to_ton(self, nano) -> float:
        try:
            return int(nano) / 1_000_000_000
        except (ValueError, TypeError):
            return 0.0

    def _do_refresh(self):
        """Один цикл: баланс + транзакции (2 запроса, пауза между ними)."""
        # Запрос 1: баланс
        bdata = self._get("getAddressBalance", {"address": self.address})
        if bdata is None:
            return
        balance = self._nano_to_ton(bdata.get("result", 0))

        # Пауза между запросами — вежливость к API
        time.sleep(1.2)

        # Запрос 2: транзакции
        tdata = self._get("getTransactions", {"address": self.address, "limit": 20})
        if tdata is None:
            return

        txs      = tdata.get("result", [])
        deposits = []
        total    = 0.0

        for tx in txs:
            in_msg = tx.get("in_msg", {}) or {}
            value  = self._nano_to_ton(in_msg.get("value", 0))
            source = in_msg.get("source", "")
            if value >= 0.001 and source:
                comment = in_msg.get("message") or in_msg.get("comment") or ""
                ts      = int(tx.get("utime", 0))
                # Человеко-читаемое время
                try:
                    import datetime as _dt
                    dt_str = _dt.datetime.utcfromtimestamp(ts).strftime("%d.%m, %H:%M")
                except Exception:
                    dt_str = ""
                deposits.append({
                    "amount":     round(value, 4),
                    "from":       source,
                    "from_short": source[:6] + "…" + source[-4:] if len(source) > 12 else source,
                    "comment":    comment[:40],
                    "time":       ts,
                    "time_str":   dt_str,
                    "hash":       (tx.get("transaction_id", {}) or {}).get("hash", ""),
                })
                total += value

        with self._lock:
            self._balance        = balance
            self._deposits       = deposits
            self._total_received = total
            self._last_update    = int(time.time())
            self._last_error     = None

    def _loop(self):
        # Первый запрос — с небольшой задержкой чтобы не конкурировать со стартом
        time.sleep(5)
        while self._running:
            if self.address:
                if self._backoff > 0:
                    time.sleep(self._backoff)
                    self._backoff = 0
                else:
                    self._do_refresh()
            time.sleep(self.poll_interval)
