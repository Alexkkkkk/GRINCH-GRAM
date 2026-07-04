"""
wallet_tracker.py — Мониторинг ВСЕХ кошельков, торгующих GRINCH в пуле.

Что делает:
  • Фоном опрашивает ленту сделок пула GRINCH/TON (GeckoTerminal) каждые POLL_SEC.
  • Дедуплицирует по tx_hash и копит ВСЕ увиденные сделки на диск (wallets.json),
    поэтому статистика накапливается со временем и переживает перезапуск.
  • Агрегирует по каждому кошельку (адрес отправителя tx_from_address):
    кто, на сколько и когда купил/продал, потраченный/полученный TON,
    средние цены входа/выхода, реализованная прибыль в TON.
  • Считает «умные деньги» (smart money): какие кошельки исторически в плюсе
    и что они делают ПРЯМО СЕЙЧАС — копят (buy) или раздают (sell).
  • Отдаёт ИИ числовой сигнал умных денег [-1..+1], чтобы бот учился у тех,
    кто реально зарабатывает, а не входил против них.

ВАЖНО (честное ограничение): бесплатный API GeckoTerminal отдаёт только
~последние сделки пула, а не всю историю с самого запуска токена. Поэтому
полная картина строится ВПЕРЁД — со временем, по мере наблюдения. Чем дольше
бот работает, тем богаче статистика по кошелькам.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
from http_client import SESSION as _HTTP

from config import Config

logger = logging.getLogger(__name__)


def _db():
    try:
        import db_store
        return db_store if db_store.is_available() else None
    except Exception:
        return None

_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(_DATA_DIR, exist_ok=True)
STORE_PATH = os.getenv("WALLETS_FILE", os.path.join(_DATA_DIR, "wallets.json"))


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts_to_epoch(iso):
    """'2026-06-26T17:58:53Z' -> epoch seconds (float)."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


class WalletTracker:
    POLL_SEC = 15           # как часто опрашиваем ленту пула
    START_DELAY = 12        # расфазировка с остальными пуллерами
    SIGNAL_WINDOW_SEC = 3600        # окно «прямо сейчас» для сигнала умных денег (1 ч)
    MIN_FLOW_TON = 5.0      # минимальный оборот в окне, чтобы доверять сигналу
    MAX_EVENTS = 2000       # сколько последних сделок храним на диске
    MAX_SEEN = 6000         # размер набора уже учтённых tx_hash
    SMART_MIN_TRADES = 2    # минимум сделок, чтобы считать кошелёк «умным»

    def __init__(self):
        self._lock = threading.RLock()
        self._running = False
        self._backoff = self.POLL_SEC
        self.last_poll = 0.0
        self.last_error = None
        # адрес -> агрегат
        self.wallets = {}
        # дедупликация увиденных сделок
        self._seen = set()
        # последние сделки (для сигнала и отображения)
        self.events = []
        self._load()

    # ----------------------------------------------------------------- запуск
    def start(self):
        if self._running:
            return
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        time.sleep(self.START_DELAY)
        while self._running:
            try:
                self._poll_once()
                self.last_error = None
                self._backoff = self.POLL_SEC
                time.sleep(self.POLL_SEC)
            except Exception as e:           # noqa: BLE001
                self.last_error = str(e)
                self._backoff = min(self._backoff * 2, 300)
                time.sleep(self._backoff)

    # ------------------------------------------------------------ опрос ленты
    def _poll_once(self):
        pool = Config.GRINCH_POOL_ADDRESS
        grinch = (Config.GRINCH_TOKEN_ADDRESS or "").lower()
        if not pool:
            return
        r = _HTTP.get(
            f"https://api.geckoterminal.com/api/v2/networks/ton/pools/{pool}/trades",
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json().get("data") or []
        new = 0
        for t in rows:
            a = t.get("attributes", {}) or {}
            # Уникальный ключ сделки: одна транзакция может содержать несколько
            # обменов (мульти-хоп), поэтому tx_hash недостаточно — берём id ленты.
            tx = a.get("tx_hash")
            trade_id = t.get("id") or (
                f"{tx}_{a.get('block_number')}_{a.get('block_timestamp')}"
                f"_{a.get('from_token_amount')}_{a.get('to_token_amount')}"
            )
            if not trade_id:
                continue
            with self._lock:
                if trade_id in self._seen:
                    continue
            addr = a.get("tx_from_address") or "—"
            to_addr = (a.get("to_token_address") or "").lower()
            from_addr = (a.get("from_token_address") or "").lower()
            if to_addr == grinch:
                kind = "buy"
                grinch_amt = _f(a.get("to_token_amount"))
                ton_amt = _f(a.get("from_token_amount"))
                price = _f(a.get("price_to_in_usd"))
            elif from_addr == grinch:
                kind = "sell"
                grinch_amt = _f(a.get("from_token_amount"))
                ton_amt = _f(a.get("to_token_amount"))
                price = _f(a.get("price_from_in_usd"))
            else:
                continue
            usd_vol = _f(a.get("volume_in_usd"))
            ts = _ts_to_epoch(a.get("block_timestamp"))
            self._record(trade_id, addr, kind, ton_amt, grinch_amt, price, ts, usd_vol)
            new += 1
        with self._lock:
            self.last_poll = time.time()
        if new:
            self._save()

    def _record(self, tx, addr, kind, ton_amt, grinch_amt, price, ts, usd_vol=0.0):
        with self._lock:
            self._seen.add(tx)
            if len(self._seen) > self.MAX_SEEN:
                self._seen = set(list(self._seen)[self.MAX_SEEN // 2:])

            w = self.wallets.get(addr)
            if w is None:
                w = {
                    "buys": 0, "sells": 0,
                    "ton_in": 0.0, "ton_out": 0.0,
                    "usd_in": 0.0, "usd_out": 0.0,
                    "grinch_bought": 0.0, "grinch_sold": 0.0,
                    "first_ts": ts, "last_ts": ts, "last_kind": kind,
                }
                self.wallets[addr] = w
            if kind == "buy":
                w["buys"] += 1
                w["ton_in"] += ton_amt
                w["grinch_bought"] += grinch_amt
                w["usd_in"] = w.get("usd_in", 0.0) + usd_vol
            else:
                w["sells"] += 1
                w["ton_out"] += ton_amt
                w["grinch_sold"] += grinch_amt
                w["usd_out"] = w.get("usd_out", 0.0) + usd_vol
            w["last_ts"] = max(w["last_ts"], ts)
            w["first_ts"] = min(w["first_ts"], ts) if w["first_ts"] else ts
            w["last_kind"] = kind

            self.events.append({
                "addr": addr, "kind": kind, "ton": ton_amt,
                "grinch": grinch_amt, "price": price,
                "usd": round(usd_vol, 2),
                "ts": ts,
            })
            if len(self.events) > self.MAX_EVENTS:
                self.events = self.events[-self.MAX_EVENTS:]

    # ---------------------------------------------------------------- метрики
    @staticmethod
    def _realized_pnl(w):
        """
        Реализованная прибыль кошелька в TON по сведённому объёму.
        Считаем только то количество GRINCH, что и куплено, и продано в
        наблюдаемой истории (matched = min(куплено, продано)). Иначе при
        sell > buy (частый случай при истории «только вперёд») прибыль
        завышается и кошелёк ошибочно попадает в «умные».
        """
        gb, gs = w["grinch_bought"], w["grinch_sold"]
        if gb <= 0 or gs <= 0:
            return 0.0
        matched = min(gb, gs)
        avg_buy = w["ton_in"] / gb
        avg_sell = w["ton_out"] / gs
        return matched * (avg_sell - avg_buy)

    def _smart_set(self):
        out = set()
        for addr, w in self.wallets.items():
            if (w["buys"] + w["sells"]) >= self.SMART_MIN_TRADES and self._realized_pnl(w) > 0:
                out.add(addr)
        return out

    def get_signal(self):
        """
        Сигнал умных денег для ИИ.
        score в [-1..+1]: >0 — копят (бычий), <0 — раздают (медвежий).
        """
        with self._lock:
            now = time.time()
            win = self.SIGNAL_WINDOW_SEC
            recent = [e for e in self.events if now - e["ts"] <= win]
            smart = self._smart_set()

        smart_buy = sum(e["ton"] for e in recent if e["kind"] == "buy" and e["addr"] in smart)
        smart_sell = sum(e["ton"] for e in recent if e["kind"] == "sell" and e["addr"] in smart)
        all_buy = sum(e["ton"] for e in recent if e["kind"] == "buy")
        all_sell = sum(e["ton"] for e in recent if e["kind"] == "sell")

        if smart_buy + smart_sell >= self.MIN_FLOW_TON:
            score = (smart_buy - smart_sell) / (smart_buy + smart_sell)
            basis = "smart"
            buy_ton, sell_ton = smart_buy, smart_sell
        elif all_buy + all_sell >= self.MIN_FLOW_TON:
            score = (all_buy - all_sell) / (all_buy + all_sell)
            basis = "flow"
            buy_ton, sell_ton = all_buy, all_sell
        else:
            score, basis = 0.0, "idle"
            buy_ton, sell_ton = all_buy, all_sell

        score = max(-1.0, min(1.0, score))
        if score >= 0.4:
            label = "накопление"
        elif score <= -0.4:
            label = "распродажа"
        else:
            label = "нейтрально"

        # ── Ранний вход: свежая волна покупок умных денег ──────────────
        # Прибыльные кошельки ТОЛЬКО НАЧАЛИ покупать (в коротком окне их
        # чистые покупки положительны, а в предыдущем таком же окне они
        # молчали/продавали) → шанс войти раньше основной волны.
        ew = Config.SMART_EARLY_WINDOW_SEC
        cur = [e for e in recent if now - e["ts"] <= ew]
        prev = [e for e in recent if ew < now - e["ts"] <= 2 * ew]
        cur_buy = sum(e["ton"] for e in cur if e["kind"] == "buy" and e["addr"] in smart)
        cur_sell = sum(e["ton"] for e in cur if e["kind"] == "sell" and e["addr"] in smart)
        prev_net = sum(
            (e["ton"] if e["kind"] == "buy" else -e["ton"])
            for e in prev if e["addr"] in smart
        )
        cur_net = cur_buy - cur_sell
        early_buy = (
            cur_net >= Config.SMART_EARLY_MIN_TON
            and prev_net <= 0
        )
        return {
            "score": round(score, 3),
            "basis": basis,
            "label": label,
            "buy_ton": round(buy_ton, 2),
            "sell_ton": round(sell_ton, 2),
            "smart_wallets": len(smart),
            "early_buy": bool(early_buy),
            "early_buy_ton": round(cur_net, 2),
        }

    WINDOW_24H = 86400      # окно для списка кошельков (последние сутки)

    def get_large_sell_events(self, window_sec: float = 120.0, min_ton: float = 500.0):
        """
        Возвращает список крупных продаж в пуле за последние window_sec секунд,
        где объём продажи >= min_ton TON. Используется детектором крупных продаж.

        Возвращает список dict: {addr, ton, grinch, price, usd, ts}
        """
        now = time.time()
        with self._lock:
            recent = [
                e for e in self.events
                if e["kind"] == "sell"
                and (now - e["ts"]) <= window_sec
                and e["ton"] >= min_ton
            ]
        return list(recent)

    def get_stats(self, top=20):
        """Полная статистика для дашборда (список — за последние 24 часа)."""
        with self._lock:
            wallets = {k: dict(v) for k, v in self.wallets.items()}
            now = time.time()
            recent = [dict(e) for e in self.events if now - e["ts"] <= self.SIGNAL_WINDOW_SEC]
            total_events = len(self.events)
            last_poll = self.last_poll
            err = self.last_error

        cutoff_24h = now - self.WINDOW_24H
        rows = []
        for addr, w in wallets.items():
            pnl = self._realized_pnl(w)
            held = w["grinch_bought"] - w["grinch_sold"]
            usd_in  = round(w.get("usd_in",  0.0), 2)
            usd_out = round(w.get("usd_out", 0.0), 2)
            rows.append({
                "addr": addr,
                "short": (addr[:6] + "…" + addr[-4:]) if len(addr) > 12 else addr,
                "buys": w["buys"], "sells": w["sells"],
                "ton_in":  round(w["ton_in"],  2),
                "ton_out": round(w["ton_out"], 2),
                "usd_in":  usd_in,
                "usd_out": usd_out,
                "usd_volume": round(usd_in + usd_out, 2),
                "grinch_held": round(held, 2),
                "grinch_bought": round(w["grinch_bought"], 2),
                "grinch_sold":   round(w["grinch_sold"],   2),
                "pnl_ton": round(pnl, 3),
                "first_ts": w["first_ts"], "last_ts": w["last_ts"],
                "last_kind": w["last_kind"],
                "smart": (w["buys"] + w["sells"]) >= self.SMART_MIN_TRADES and pnl > 0,
            })

        # Список — только кошельки, активные за последние 24 часа, по убыванию
        active = [r for r in rows if r["last_ts"] >= cutoff_24h]
        top_profit = sorted(active, key=lambda x: x["pnl_ton"], reverse=True)[:top]
        top_volume = sorted(active, key=lambda x: x["usd_volume"], reverse=True)[:top]

        buy_ton  = sum(e["ton"] for e in recent if e["kind"] == "buy")
        sell_ton = sum(e["ton"] for e in recent if e["kind"] == "sell")
        buy_usd  = sum(e.get("usd", 0.0) for e in recent if e["kind"] == "buy")
        sell_usd = sum(e.get("usd", 0.0) for e in recent if e["kind"] == "sell")
        smart_n  = sum(1 for r in rows if r["smart"])

        # Последние 30 событий — для блока «Последние сделки» в карточке кошельков
        smart_addrs_set = {r["addr"] for r in rows if r["smart"]}
        recent_events = []
        for e in reversed(self.events[-30:]):
            recent_events.append({
                "addr":   e["addr"],
                "short":  (e["addr"][:6] + "…" + e["addr"][-4:]) if len(e["addr"]) > 12 else e["addr"],
                "kind":   e["kind"],
                "grinch": e["grinch"],
                "ton":    e["ton"],
                "usd":    e.get("usd", 0.0),
                "ts":     e["ts"],
                "smart":  e["addr"] in smart_addrs_set,
            })

        smart_addrs = [r["addr"] for r in rows if r["smart"]]
        return {
            "signal": self.get_signal(),
            "total_wallets": len(rows),
            "active_24h": len(active),
            "smart_wallets": smart_n,
            "smart_addrs": smart_addrs,
            "total_trades_seen": total_events,
            "recent_buy_ton":  round(buy_ton,  2),
            "recent_sell_ton": round(sell_ton, 2),
            "recent_buy_usd":  round(buy_usd,  2),
            "recent_sell_usd": round(sell_usd, 2),
            "top_profit": top_profit,
            "top_volume": top_volume,
            "recent_events": recent_events,
            "last_poll": last_poll,
            "error": err,
            "pool": Config.GRINCH_POOL_ADDRESS,
        }

    # ------------------------------------------------------------ persistence
    def _load(self):
        db = _db()
        loaded_from_db = False

        # Попытка загрузить из PostgreSQL
        if db:
            try:
                wallets, events, seen, last_poll = db.wallets_load()
                if wallets or events:
                    self.wallets   = wallets
                    self.events    = events
                    self._seen     = seen
                    self.last_poll = last_poll
                    loaded_from_db = True
                    logger.info(f"[WalletTracker] Загружено из DB: {len(wallets)} кошельков")
            except Exception as e:
                logger.warning(f"[WalletTracker] DB load error: {e}")

        # Fallback / миграция: JSON
        if not loaded_from_db:
            if not os.path.exists(STORE_PATH):
                return
            try:
                with open(STORE_PATH, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.wallets   = data.get("wallets", {}) or {}
                self.events    = data.get("events", []) or []
                self._seen     = set(data.get("seen", []) or [])
                self.last_poll = data.get("last_poll", 0.0) or 0.0
                logger.info(f"[WalletTracker] Загружено из JSON: {len(self.wallets)} кошельков")
                # Миграция в DB
                if db and self.wallets:
                    try:
                        db.wallets_save(
                            self.wallets,
                            self.events[-self.MAX_EVENTS:],
                            list(self._seen)[-self.MAX_SEEN:],
                            self.last_poll,
                        )
                        logger.info("[WalletTracker] ✅ Кошельки мигрированы JSON → PostgreSQL")
                    except Exception as e:
                        logger.warning(f"[WalletTracker] migrate_to_db error: {e}")
            except Exception:
                self.wallets, self.events, self._seen = {}, [], set()

    def _save(self):
        with self._lock:
            wallets    = dict(self.wallets)
            events     = self.events[-self.MAX_EVENTS:]
            seen       = list(self._seen)[-self.MAX_SEEN:]
            last_poll  = self.last_poll

        # JSON backup
        payload = {
            "wallets": wallets,
            "events": events,
            "seen": seen,
            "last_poll": last_poll,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = STORE_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, STORE_PATH)
        except Exception:
            pass

        # DB
        db = _db()
        if db:
            try:
                db.wallets_save(wallets, events, seen, last_poll)
            except Exception as e:
                logger.warning(f"[WalletTracker] DB save error: {e}")
