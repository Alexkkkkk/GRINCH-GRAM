"""
wallet_manager.py — Полное отслеживание баланса кошелька TON + GRINCH.

• Периодически опрашивает реальный баланс через dedust_client.
• Вычисляет P&L на основе цены входа из открытых позиций trader.
• Хранит каждый снимок в PostgreSQL (bot_wallet_snapshots) — всё через БД.
• Предоставляет get_snapshot(), get_analytics(), get_history().

Не зависит от app.py и может запускаться в любой момент после init.
"""
import threading
import time
import logging
from datetime import datetime

log = logging.getLogger(__name__)

POLL_SEC = 30   # опрос баланса каждые 30 секунд


class WalletManager:
    """Менеджер полного состояния кошелька (TON + GRINCH) с историей в БД."""

    def __init__(self):
        self._lock      = threading.Lock()
        self._poll_lock = threading.Lock()   # предотвращает конкурентный запуск _poll
        self._snap      = {}           # последний снимок
        self._history   = []           # кольцо в памяти (200 точек)
        self._thread    = None
        self._running   = False
        self._stop_event = threading.Event()   # мгновенная остановка
        self._trader    = None         # ссылка на Trader для чтения open_trades

    # ─── запуск ────────────────────────────────────────────────────────────────

    def start(self, trader_ref=None):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._trader  = trader_ref
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="wallet-manager"
        )
        self._thread.start()
        log.info("[WalletManager] ✅ Запущен (опрос каждые %ds)", POLL_SEC)

    def stop(self):
        """Останавливает фоновый опрос мгновенно."""
        self._running = False
        self._stop_event.set()

    # ─── главный цикл ──────────────────────────────────────────────────────────

    def _loop(self):
        self._stop_event.wait(timeout=8)   # прерываемый прогрев после старта
        while self._running and not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as exc:
                log.warning("[WalletManager] ошибка опроса: %s", exc)
            self._stop_event.wait(timeout=POLL_SEC)   # прерываемый сон

    # ─── один опрос ────────────────────────────────────────────────────────────

    def _poll(self):
        # Предотвращаем конкурентный запуск (фоновый тред + ручной /api/wallet/refresh)
        if not self._poll_lock.acquire(blocking=False):
            return   # уже идёт опрос — пропускаем
        try:
            self._poll_body()
        finally:
            self._poll_lock.release()

    def _poll_body(self):
        from price_feed import price_feed
        import db_store

        # 1. Реальный баланс с блокчейна
        bal = {}
        try:
            from dedust_client import dedust_client
            bal = dedust_client.get_balance() or {}
        except Exception as exc:
            log.debug("[WalletManager] get_balance: %s", exc)

        ton_bal    = float(bal.get("TON",    0) or 0)
        grinch_bal = float(bal.get("GRINCH", 0) or 0)

        # 2. Цены
        ton_usd    = float(price_feed.get("TON")              or 0)
        grinch_usd = float(price_feed.get("GRINCH")           or 0)
        grinch_ton = float(price_feed.get_grinch_ton_price()  or 0)

        # 3. Стоимость GRINCH
        grinch_value_ton = round(grinch_bal * grinch_ton, 8) if grinch_ton > 0 else 0.0
        grinch_value_usd = round(grinch_bal * grinch_usd, 6) if grinch_usd > 0 else 0.0

        # 4. Общий портфель
        total_equity_ton = round(ton_bal + grinch_value_ton, 8)
        total_equity_usd = round(ton_bal * ton_usd + grinch_value_usd, 4) if ton_usd > 0 else 0.0

        # 5. Цена входа и P&L из открытых лонг-позиций
        entry_price_ton = None
        entry_price_usd = None
        pnl_ton         = None
        pnl_pct         = None
        pnl_usd         = None
        tracked_amount  = None   # сколько GRINCH реально относится к открытым trader-позициям
        tracked_entries = None
        tracked_stake   = None   # полная стоимость входа (total_stake) — единая база для cost
                                  # в _poll_body() и get_full_status(), независимо от tracked_amount

        trader = self._trader
        if trader is not None:
            try:
                # Копируем позиции под локом trader'а (если есть), чтобы не
                # прочитать trade в момент, когда trader обновляет amount и
                # stake_ton двумя отдельными присвоениями (self-heal баланса,
                # каскадная продажа) — иначе можно поймать "рваное" сочетание
                # новое amount + старое stake_ton (или наоборот), из-за чего
                # entry_price_ton/P&L на дашборде скачет при неизменной цене.
                _ot_lock = getattr(trader, "_ot_lock", None)
                if _ot_lock is not None:
                    with _ot_lock:
                        _raw_trades = [dict(t) for t in getattr(trader, "open_trades", [])]
                else:
                    _raw_trades = list(getattr(trader, "open_trades", []))
                open_trades = [t for t in _raw_trades if t.get("side") == "buy"]
                if open_trades and grinch_bal > 0 and grinch_ton > 0:
                    total_stake  = sum(t.get("stake_ton", 0) or 0 for t in open_trades)
                    total_amount = sum(t.get("amount",    0) or 0 for t in open_trades)

                    if total_amount > 0 and total_stake > 0:
                        # Средневзвешенная цена входа в TON
                        entry_price_ton = total_stake / total_amount

                        # Средневзвешенная цена входа в USD
                        entry_usd_weighted = sum(
                            (t.get("entry_price", 0) or 0) * (t.get("amount", 0) or 0)
                            for t in open_trades
                        )
                        entry_price_usd = entry_usd_weighted / total_amount

                        # P&L с учётом комиссий и газа.
                        # ВАЖНО: считаем P&L только по реально отслеживаемому trader'ом объёму
                        # (total_amount), а НЕ по всему балансу кошелька (grinch_bal) — на кошельке
                        # может лежать доп. GRINCH, не связанный с текущей открытой DCA-позицией
                        # (старые/ручные поступления), иначе P&L считается против чужого количества
                        # токенов и получается бессмысленно завышенным.
                        try:
                            from config import Config
                            fee      = Config.FEE_PCT / 100.0
                            sell_gas = Config.SELL_GAS_TON
                            buy_gas  = getattr(Config, "BUY_GAS_TON", 0.25)
                            n_entries = len(open_trades)

                            tracked_amount  = min(total_amount, grinch_bal)
                            tracked_entries = n_entries
                            tracked_stake   = total_stake
                            tracked_value_ton = tracked_amount * grinch_ton

                            proceeds = tracked_value_ton * (1.0 - fee) - sell_gas
                            cost     = tracked_stake + buy_gas * n_entries
                            pnl_ton  = round(proceeds - cost, 6)
                            pnl_pct  = round(pnl_ton / cost * 100, 2) if cost > 0 else 0.0
                            pnl_usd  = round(pnl_ton * ton_usd, 4) if ton_usd > 0 else None
                        except Exception as exc2:
                            log.debug("[WalletManager] P&L config: %s", exc2)
            except Exception as exc:
                log.debug("[WalletManager] P&L calc: %s", exc)

        snap = {
            "ts":               datetime.utcnow().isoformat(),
            "ton_balance":      round(ton_bal, 6),
            "grinch_balance":   round(grinch_bal, 2),
            "grinch_price_ton": round(grinch_ton, 10) if grinch_ton > 0 else None,
            "grinch_price_usd": grinch_usd            if grinch_usd > 0 else None,
            "ton_price_usd":    ton_usd               if ton_usd    > 0 else None,
            "grinch_value_ton": grinch_value_ton,
            "grinch_value_usd": grinch_value_usd,
            "total_equity_ton": total_equity_ton,
            "total_equity_usd": total_equity_usd,
            "entry_price_ton":  round(entry_price_ton, 10) if entry_price_ton else None,
            "entry_price_usd":  entry_price_usd,
            "pnl_ton":          pnl_ton,
            "pnl_pct":          pnl_pct,
            "pnl_usd":          pnl_usd,
            "tracked_amount":   round(tracked_amount, 6) if tracked_amount else None,
            "tracked_entries":  tracked_entries,
            "tracked_stake":    round(tracked_stake, 6) if tracked_stake else None,
        }

        # Рваные чтения open_trades предотвращены через _ot_lock (trader.py).
        # Фильтр аномалий здесь намеренно убран: он также срабатывал при
        # легитимных изменениях tracked_stake после self-heal (масштабирование
        # amount+stake_ton при расхождении баланса >1%), из-за чего скорректи-
        # рованный снимок отбрасывался и дашборд продолжал показывать старые
        # (неверные) данные.
        with self._lock:
            self._snap = snap
            self._history.append(snap)
            if len(self._history) > 200:
                self._history = self._history[-200:]

        # Сохраняем в БД (best-effort)
        try:
            db_store.wallet_snapshot_insert(snap)
        except Exception as exc:
            log.debug("[WalletManager] DB insert: %s", exc)

    # ─── публичный API ─────────────────────────────────────────────────────────

    def get_snapshot(self) -> dict:
        """Последний снимок кошелька (из памяти, мгновенно)."""
        with self._lock:
            return dict(self._snap)

    def get_history(self, limit: int = 200) -> list:
        """История снимков из БД (или памяти при недоступности БД)."""
        import db_store
        try:
            rows = db_store.wallet_snapshots_get_recent(limit)
            if rows:
                return rows
        except Exception:
            pass
        with self._lock:
            return list(self._history[-limit:])

    def get_full_status(self) -> dict:
        """Полный статус кошелька: снимок + позиция + потенциал + история (50 точек)."""
        snap    = self.get_snapshot()
        history = self.get_history(50)

        grinch_bal  = snap.get("grinch_balance", 0) or 0
        entry_ton   = snap.get("entry_price_ton")
        cur_ton     = snap.get("grinch_price_ton")
        cur_usd     = snap.get("grinch_price_usd")
        ton_usd     = snap.get("ton_price_usd")
        pnl_ton     = snap.get("pnl_ton")
        pnl_pct     = snap.get("pnl_pct")
        in_position = grinch_bal > 0
        # Только реально отслеживаемое trader'ом количество (см. _poll_body) —
        # избегаем расчёта потенциала от всего баланса кошелька, если часть GRINCH
        # не относится к текущей открытой DCA-позиции. Никакого fallback на grinch_bal:
        # если снимок ещё не содержит tracked_amount (нет открытой позиции / старый снимок),
        # потенциал просто не считаем, а не считаем его от чужого объёма токенов.
        tracked_amount  = snap.get("tracked_amount")
        tracked_entries = snap.get("tracked_entries") or 1
        tracked_stake   = snap.get("tracked_stake")

        # Ценовой диапазон за историю
        prices_ton = [h.get("grinch_price_ton") for h in history if h.get("grinch_price_ton")]
        price_min  = min(prices_ton) if prices_ton else None
        price_max  = max(prices_ton) if prices_ton else None

        # Мин/макс портфель за историю
        equities   = [h.get("total_equity_ton") for h in history if h.get("total_equity_ton")]
        eq_min     = min(equities) if equities else None
        eq_max     = max(equities) if equities else None

        # Потенциальная прибыль при разных ценах
        # Используем те же параметры стоимости, что и в _poll_body() для консистентности:
        # cost = total_stake + buy_gas * n_entries  (из снимка: grinch_bal*entry_ton ≈ total_stake)
        potential  = {}
        if entry_ton and tracked_amount and tracked_amount > 0 and tracked_stake and in_position:
            try:
                from config import Config
                fee      = Config.FEE_PCT / 100.0
                sell_gas = Config.SELL_GAS_TON
                buy_gas  = getattr(Config, "BUY_GAS_TON", 0.25)
                # Единая база стоимости с _poll_body(): cost = tracked_stake + buy_gas * n_entries.
                # tracked_stake — это полная сумма вложений trader'а (total_stake), а не
                # пропорция от tracked_amount*entry_ton — иначе cost-модель разойдётся между
                # live P&L (_poll_body) и проекцией потенциала здесь при tracked_amount < total_amount.
                cost  = tracked_stake + buy_gas * tracked_entries
                for pct in (5, 10, 15, 20, 30):
                    tgt_ton = entry_ton * (1 + pct / 100)
                    proceeds = tracked_amount * tgt_ton * (1 - fee) - sell_gas
                    p_pnl    = round(proceeds - cost, 6)
                    potential[f"+{pct}%"] = {
                        "target_price_ton": round(tgt_ton, 10),
                        "target_price_usd": round(tgt_ton / entry_ton * (cur_usd or 0), 8) if cur_usd else None,
                        "pnl_ton":          p_pnl,
                        "pnl_usd":          round(p_pnl * ton_usd, 4) if ton_usd else None,
                    }
            except Exception:
                pass

        # Процент от стартового капитала (если есть история)
        start_equity  = equities[0]  if equities else None
        current_eq    = snap.get("total_equity_ton")
        equity_change = None
        if start_equity and current_eq and start_equity > 0:
            equity_change = round((current_eq - start_equity) / start_equity * 100, 2)

        return {
            "snapshot":       snap,
            "in_position":    in_position,
            "grinch_count":   grinch_bal,
            "entry_price_ton": entry_ton,
            "entry_price_usd": snap.get("entry_price_usd"),
            "current_price_ton": cur_ton,
            "current_price_usd": cur_usd,
            "pnl_ton":        pnl_ton,
            "pnl_pct":        pnl_pct,
            "pnl_usd":        snap.get("pnl_usd"),
            "price_range": {
                "min_ton": price_min,
                "max_ton": price_max,
            },
            "equity_range": {
                "min_ton": eq_min,
                "max_ton": eq_max,
            },
            "equity_change_pct": equity_change,
            "potential":      potential,
            "history":        history,
        }


# Глобальный singleton
wallet_manager = WalletManager()
