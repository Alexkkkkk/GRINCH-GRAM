"""
Авто-ликвидатор накопленного GRINCH.

Следит за реальным балансом GRINCH на платформенном кошельке (из Failed-транзакций).
Как только цена поднимается на SELL_RISE_PCT% от опорной — продаёт всё автоматически.
"""
import threading
import time
import logging
import urllib.parse
from datetime import datetime
from typing import Optional
import requests as _requests
from config import Config
from price_feed import price_feed

log = logging.getLogger(__name__)


def _addresses_match(a: str, b: str) -> bool:
    """
    Сравниваем два TON-адреса нечувствительно к формату (EQ/UQ/raw 0:...).
    TonAPI возвращает raw-формат (0:abc...), Config хранит EQ-формат.
    Нормализуем оба к raw-hex через Address — base64 (EQ/UQ) нельзя сравнивать
    с raw побайтово напрямую, иначе совпадения никогда не будет.
    """
    def _norm(addr: str) -> str:
        addr = (addr or "").strip()
        try:
            from pytoniq_core import Address
            return Address(addr).to_str(is_user_friendly=False).lower()
        except Exception:
            # запасной вариант: hex-часть после ':'
            if ":" in addr:
                return addr.split(":", 1)[1].lower()
            return addr.lower()
    try:
        return _norm(a) == _norm(b)
    except Exception:
        return a.strip().lower() == b.strip().lower()

MIN_GRINCH_TO_SELL = 0.5    # меньше этого — не стоит тратить TON на газ
BAL_CHECK_INTERVAL = 60     # секунд между on-chain запросами баланса
PRICE_TICK_SECS    = 30     # секунд между проверками цены (из кэша price_feed)
START_DELAY        = 20     # задержка запуска
GAS_NEEDED_TON     = 0.40   # минимум TON на кошельке для свопа GRINCH→TON (attach 0.25+0.18=0.43, часть вернётся)


class GrinchLiquidator:
    """
    Фоновый поток: следит за накопленным GRINCH и продаёт при заданном росте цены.

    Логика:
    1. Раз в BAL_CHECK_INTERVAL проверяем on-chain GRINCH баланс.
    2. Если баланс > MIN_GRINCH_TO_SELL → фиксируем опорную цену.
    3. Каждые PRICE_TICK_SECS сравниваем текущую цену с опорной.
    4. Как только цена выросла на sell_rise_pct% — продаём всё через DeDust.
    """

    def __init__(self):
        self._lock           = threading.Lock()
        self._running        = False
        self._thread         = None
        self._grinch_bal     = 0.0
        self._ton_bal        = None    # баланс TON кошелька (для проверки газа)
        self._ref_price      = None    # цена в момент обнаружения GRINCH
        self._ref_time       = None
        self._last_bal_check = 0.0
        self._last_sell_at   = None
        self._sell_count     = 0
        self._logs           = []
        # Порог роста для продажи — можно менять через API.
        # Загружаем сохранённое значение из settings.json (если есть), иначе
        # дефолт = нетто-цель + комиссия цикла (≈22% gross → ≥20% нетто).
        # Значение переживает перезапуски.
        self.sell_rise_pct   = Config.required_gross_pct()
        try:
            from settings_store import get_section
            saved = get_section("liquidator").get("sell_rise_pct")
            if saved is not None:
                self.sell_rise_pct = max(0.5, min(float(saved), 200.0))
        except Exception:  # noqa: BLE001 — настройки не должны ломать запуск
            pass

    # ── Логирование ─────────────────────────────────────────────────────────

    def _log(self, msg: str, level: str = "INFO"):
        entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "level": level, "msg": msg}
        self._logs.append(entry)
        if len(self._logs) > 150:
            self._logs = self._logs[-150:]
        print(f"[Liquidator] {entry['time']} [{level}] {msg}")

    # ── Жизненный цикл ──────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._log("🟢 Авто-ликвидатор GRINCH запущен")

    def stop(self):
        self._running = False
        self._log("🔴 Авто-ликвидатор остановлен", "WARN")

    # ── Публичный статус (для API / UI) ─────────────────────────────────────

    def get_status(self) -> dict:
        with self._lock:
            current = price_feed.get("GRINCH") or 0.0
            target_price, pct_to_go, pct_now = None, None, None
            if self._ref_price and self._ref_price > 0 and current > 0:
                target_price = round(self._ref_price * (1 + self.sell_rise_pct / 100), 8)
                pct_to_go    = round((target_price - current) / current * 100, 2)
                pct_now      = round((current - self._ref_price) / self._ref_price * 100, 2)
            # Хватает ли TON на газ для свопа GRINCH→TON
            gas_ok = None if self._ton_bal is None else (self._ton_bal >= GAS_NEEDED_TON)
            return {
                "running":        self._running,
                "grinch_balance": round(self._grinch_bal, 4),
                "ton_balance":    None if self._ton_bal is None else round(self._ton_bal, 3),
                "gas_needed":     GAS_NEEDED_TON,
                "gas_ok":         gas_ok,
                "ref_price":      self._ref_price,
                "ref_time":       self._ref_time,
                "current_price":  current,
                "target_price":   target_price,
                "pct_to_go":      pct_to_go,
                "pct_now":        pct_now,
                "sell_rise_pct":  self.sell_rise_pct,
                "sell_count":     self._sell_count,
                "last_sell_at":   self._last_sell_at,
                "logs":           list(self._logs[-30:]),
            }

    def set_threshold(self, pct: float):
        """Изменить порог продажи (в процентах) и сохранить между перезапусками."""
        pct = max(0.5, min(pct, 200.0))
        self.sell_rise_pct = pct
        try:
            from settings_store import update_section
            update_section("liquidator", {"sell_rise_pct": pct})
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ Не удалось сохранить порог: {e}", "WARN")
        self._log(f"⚙️ Порог продажи изменён на +{pct}% (сохранено)")

    # ── Основной цикл ───────────────────────────────────────────────────────

    def _loop(self):
        # Стартуем позже всех чтобы не перегружать TonCenter
        time.sleep(START_DELAY)
        self._log(f"🔍 Начинаю мониторинг GRINCH (порог продажи: +{self.sell_rise_pct}%)")

        while self._running:
            try:
                now = time.time()
                # Обновляем on-chain баланс раз в BAL_CHECK_INTERVAL
                if now - self._last_bal_check >= BAL_CHECK_INTERVAL:
                    self._refresh_balance()
                    self._last_bal_check = now

                # Проверяем цену каждые PRICE_TICK_SECS
                self._check_and_maybe_sell()

            except Exception as e:
                self._log(f"Ошибка цикла: {e}", "ERROR")

            time.sleep(PRICE_TICK_SECS)

    # ── Получение баланса ────────────────────────────────────────────────────

    def _fetch_grinch_balance_http(self) -> float:
        """
        Получаем GRINCH баланс. Приоритет: TonCenter v3 (стабильный, без rate-limit)
        → TonAPI v2 (запасной).
        """
        import urllib.request, json as _json
        wallet = Config.TON_WALLET
        token  = Config.GRINCH_TOKEN_ADDRESS

        # Приоритет 1: TonCenter v3 (без rate-limit, прямой запрос)
        try:
            url = (
                f"https://toncenter.com/api/v3/jetton/wallets"
                f"?owner_address={urllib.parse.quote(wallet)}"
                f"&jetton_address={urllib.parse.quote(token)}&limit=1"
            )
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = _json.loads(r.read())
            wallets = d.get("jetton_wallets", [])
            if wallets:
                return float(wallets[0].get("balance", 0)) / (10 ** 9)
        except Exception as e:
            self._log(f"TonCenter v3 GRINCH balance ошибка: {e}", "WARN")

        # Запасной: TonAPI v2
        try:
            url2 = f"https://tonapi.io/v2/accounts/{wallet}/jettons"
            req2 = urllib.request.Request(url2, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req2, timeout=10) as r2:
                data = _json.loads(r2.read())
            for item in data.get("balances", []):
                master = (item.get("jetton", {}) or {}).get("address", "")
                if _addresses_match(master, token):
                    return float(item.get("balance", "0")) / (10 ** 9)
        except Exception as e2:
            self._log(f"TonAPI jetton balance ошибка: {e2}", "WARN")

        return 0.0

    def _fetch_ton_balance_http(self) -> Optional[float]:
        """Баланс TON кошелька. Приоритет: TonCenter v2 → TonAPI v2."""
        import urllib.request, json as _json
        wallet = Config.TON_WALLET

        # Приоритет 1: TonCenter v2 (стабильный, без rate-limit)
        try:
            url = f"https://toncenter.com/api/v2/getAddressBalance?address={urllib.parse.quote(wallet)}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read())
            result = data.get("result")
            if result is not None:
                return float(result) / (10 ** 9)
        except Exception as e:
            self._log(f"TonCenter v2 TON balance ошибка: {e}", "WARN")

        # Запасной: TonAPI v2
        try:
            url2 = f"https://tonapi.io/v2/accounts/{wallet}"
            req2 = urllib.request.Request(url2, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req2, timeout=8) as r2:
                data2 = _json.loads(r2.read())
            bal = data2.get("balance")
            if bal is not None:
                return float(bal) / (10 ** 9)
        except Exception as e2:
            self._log(f"TonAPI TON balance ошибка: {e2}", "WARN")

        return None

    def _refresh_balance(self):
        try:
            from dedust_client import get_shared_balance
            bal   = get_shared_balance()
            grinch = bal.get("GRINCH", 0.0)
            ton    = bal.get("TON")   # None если недоступно

            with self._lock:
                old = self._grinch_bal
                self._grinch_bal = grinch
                if ton is not None:
                    self._ton_bal = ton

                # Предупреждаем, если есть GRINCH на продажу, но мало TON на газ
                if grinch >= MIN_GRINCH_TO_SELL and ton is not None and ton < GAS_NEEDED_TON:
                    self._log(
                        f"⛽ Мало TON для газа: {ton:.3f} TON на кошельке, "
                        f"нужно ≥ {GAS_NEEDED_TON} TON. Своп GRINCH отскочит (Bounce) — "
                        f"пополните кошелёк TON.",
                        "WARN"
                    )

                if grinch >= MIN_GRINCH_TO_SELL:
                    if self._ref_price is None:
                        ref = price_feed.get("GRINCH") or 0.0
                        # Опорная = РЕАЛЬНАЯ цена покупки (если сохранена), а не
                        # текущая цена. Так после перезапуска цель = покупка×(+%),
                        # и мы НИКОГДА не продаём дешевле, чем купили.
                        try:
                            from experience_manager import experience_manager
                            cb = experience_manager.get_cost_basis()
                            if cb and cb > 0:
                                ref = cb
                                self._log(
                                    f"📌 Опорная взята из памяти (цена покупки): ${cb:.8f}",
                                    "INFO"
                                )
                        except Exception as e:  # noqa: BLE001
                            self._log(f"Не удалось прочитать цену покупки: {e}", "WARN")
                        if ref > 0:
                            self._ref_price = ref
                            self._ref_time  = datetime.utcnow().isoformat()
                            target = ref * (1 + self.sell_rise_pct / 100)
                            self._log(
                                f"💰 Найдено {grinch:.4f} GRINCH | "
                                f"Опорная: ${ref:.8f} | "
                                f"Цель: ${target:.8f} (+{self.sell_rise_pct}%)"
                            )
                    elif abs(grinch - old) > 0.01:
                        self._log(
                            f"🔄 Баланс: {old:.4f} → {grinch:.4f} GRINCH | "
                            f"Опорная: ${self._ref_price:.8f}"
                        )
                elif grinch < 0.01 and old >= MIN_GRINCH_TO_SELL:
                    self._log(f"✅ GRINCH продан ({old:.4f} → {grinch:.4f})")
                    self._ref_price = None
                    self._ref_time  = None
                else:
                    self._log(f"📊 GRINCH on-chain: {grinch:.4f}")

        except Exception as e:
            self._log(f"Ошибка обновления баланса: {e}", "WARN")

    # ── Проверка цены и продажа ──────────────────────────────────────────────

    def _check_and_maybe_sell(self):
        with self._lock:
            grinch = self._grinch_bal
            ref    = self._ref_price

        if grinch < MIN_GRINCH_TO_SELL or ref is None or ref <= 0:
            return

        current = price_feed.get("GRINCH") or 0.0
        if current <= 0:
            return

        rise_pct = (current - ref) / ref * 100
        target   = ref * (1 + self.sell_rise_pct / 100)

        if current >= target:
            self._log(
                f"🚀 Цена выросла на {rise_pct:+.2f}%! "
                f"${ref:.8f} → ${current:.8f} (цель: ${target:.8f}) | "
                f"Продаём {grinch:.4f} GRINCH...",
                "INFO"
            )
            self._execute_sell(grinch, current)
        else:
            pct_to_go = ((target - current) / current) * 100
            self._log(
                f"⏳ {grinch:.4f} GRINCH | Сейчас ${current:.8f} "
                f"({rise_pct:+.2f}%) | Цель ${target:.8f} | "
                f"До продажи: ещё +{pct_to_go:.2f}%"
            )

    # ── Исполнение продажи ───────────────────────────────────────────────────

    def _execute_sell(self, grinch_amount: float, current_price: float) -> dict:
        """Исполняет продажу и ВОЗВРАЩАЕТ реальный результат свопа.

        Раньше метод глотал ошибку (только лог), из-за чего вызывающий код
        мог сообщить об «успехе», хотя своп на самом деле отскочил. Теперь
        результат всегда возвращается, а баланс/счётчики обновляются ТОЛЬКО
        при подтверждённом on-chain исполнении.
        """
        try:
            from dedust_client import dedust_client
            result = dedust_client.sell(grinch_amount)

            if result and result.get("ok"):
                est_ton = grinch_amount * current_price
                self._log(
                    f"✅ Продано {grinch_amount:.4f} GRINCH @ ${current_price:.8f} | "
                    f"Ожидаемо ≈{est_ton:.4f} TON",
                    "INFO"
                )
                with self._lock:
                    self._sell_count   += 1
                    self._last_sell_at  = datetime.utcnow().isoformat()
                    self._grinch_bal    = 0.0
                    self._ref_price     = None
                    self._ref_time      = None
                # Обновим баланс через 60 сек
                self._last_bal_check = time.time() - BAL_CHECK_INTERVAL + 60
                return {"ok": True, "grinch_sold": grinch_amount, "price": current_price}
            else:
                err = (result.get("error") if result else None) or "нет ответа"
                self._log(f"⚠️ Продажа не удалась: {err}", "WARN")
                return {"ok": False, "error": err}

        except Exception as e:
            self._log(f"Ошибка продажи: {e}", "ERROR")
            return {"ok": False, "error": str(e)}

    def force_sell_now(self) -> dict:
        """Немедленная продажа (вызывается вручную через кнопку в UI)."""
        with self._lock:
            grinch = self._grinch_bal

        if grinch < MIN_GRINCH_TO_SELL:
            # Попробуем получить актуальный баланс
            self._refresh_balance()
            with self._lock:
                grinch = self._grinch_bal

        if grinch < MIN_GRINCH_TO_SELL:
            return {"ok": False, "error": f"GRINCH баланс {grinch:.4f} < мин. {MIN_GRINCH_TO_SELL}"}

        current = price_feed.get("GRINCH") or 0.0
        self._log(f"🔴 РУЧНАЯ продажа {grinch:.4f} GRINCH @ ${current:.8f}")
        # Возвращаем РЕАЛЬНЫЙ результат свопа (ok только при подтверждённом
        # списании GRINCH on-chain), а не безусловный успех.
        return self._execute_sell(grinch, current)


# ── Синглтон — запускается при импорте модуля ────────────────────────────────
grinch_liquidator = GrinchLiquidator()
grinch_liquidator.start()
