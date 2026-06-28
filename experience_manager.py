"""
experience_manager.py — Долговременная память и САМО-УПРАВЛЕНИЕ ИИ.

Что делает (то, о чём просил пользователь):
  1. СОХРАНЯЕТ свой опыт на диск (experience.json):
       • журнал всех закрытых сделок (вход / выход / PnL / причина)
       • кривую капитала кошелька (TON + GRINCH во времени)
       • подтверждённый опыт ИИ (признаки + метки) — обучение переживает рестарт
       • статистику трейдера
       • адаптивные параметры управления
  2. ЧИТАЕТ файл при старте и восстанавливает: статистику, кривую капитала,
     и ВОЗВРАЩАЕТ опыт обратно в ИИ (тёплый старт обучения).
  3. СЛЕДИТ ЗА БАЛАНСОМ кошелька во времени (считает просадку от пика).
  4. ОТДАЁТ опыт на изучение ИИ и по фактам ПРАВИТ УПРАВЛЕНИЕ:
       • строже фильтр уверенности после серии убытков
       • БОЛЬШЕ размер ставки на доказанной серии прибыли (с жёстким потолком)
       • меньше размер ставки при просадке капитала
       • ПАУЗА новых покупок при сильной просадке (с гистерезисом)

ВАЖНО: «правит код» здесь = безопасная адаптация торговых ПАРАМЕТРОВ по
реальной статистике. ИИ НЕ переписывает исходники программы (это опасно) —
он настраивает поведение бота на лету и сохраняет настройки между запусками.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime

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
FILE = os.getenv("EXPERIENCE_FILE", os.path.join(_DATA_DIR, "experience.json"))

# ── Параметры адаптации (само-управление) ────────────────────────────────────
MAX_TRADES_KEPT   = 1000     # сколько последних сделок хранить в журнале
MAX_EQUITY_KEPT   = 3000     # сколько точек кривой капитала хранить
EQUITY_MIN_GAP    = 60       # не чаще раза в N секунд писать точку капитала
RECENT_WINDOW     = 10       # окно «недавних» сделок для оценки
CONF_CAP          = 90.0     # потолок порога уверенности
DD_SHRINK_1       = 10.0     # просадка % → уменьшаем ставку
DD_SHRINK_2       = 20.0     # просадка % → сильно уменьшаем ставку
DD_PAUSE          = 30.0     # просадка % → пауза новых покупок
DD_RESUME         = 15.0     # просадка % → снимаем паузу (гистерезис)
# — безопасный РОСТ ставки на доказанной прибыли (только в спокойном режиме) —
WIN_GROW_1        = 3        # серия прибыльных сделок → ставка +25%
WIN_GROW_2        = 6        # серия прибыльных сделок → ставка до потолка
GROW_CAP          = 1.5      # жёсткий потолок множителя ставки (×1.5 от базовой)


class ExperienceManager:
    def __init__(self, path: str = FILE):
        self.path = path
        self._lock = threading.RLock()
        self._last_equity_ts = 0.0
        self.data = {
            "version": 1,
            "created": datetime.utcnow().isoformat(),
            "trades":      [],   # журнал закрытых сделок
            "open_trades": [],   # ОТКРЫТЫЕ позиции: цена покупки + цель продажи
            "equity":      [],   # снимки капитала
            "stats":       {},   # последняя статистика трейдера
            "ai":          {},   # экспорт опыта ИИ
            "control":     self._default_control(),
        }
        self._load()

    # ── По умолчанию ─────────────────────────────────────────────────────────
    def _default_control(self) -> dict:
        return {
            "base_min_conf":     float(Config.MIN_AI_CONFIDENCE),
            "base_trade_amount": float(Config.TRADE_AMOUNT),
            "min_conf":          float(Config.MIN_AI_CONFIDENCE),
            "trade_amount":      float(Config.TRADE_AMOUNT),
            "paused":            False,
            "peak_equity":       0.0,
            "drawdown_pct":      0.0,
            "loss_streak":       0,
            "last_note":         "init",
            "updated":           None,
        }

    # ── Чтение / запись ──────────────────────────────────────────────────────
    def _load(self):
        db = _db()
        loaded_from_db = False

        # ── Попытка загрузить из PostgreSQL ──────────────────────────────────
        if db:
            try:
                trades     = db.trades_get_all()
                equity     = db.equity_get_all()
                open_trades = db.open_trades_get()
                ai_state   = db.ai_state_get_all()
                control_raw = ai_state.get("control")
                stats_raw   = ai_state.get("stats")
                ai_raw      = ai_state.get("ai_export")

                if trades or equity or control_raw:
                    if trades:      self.data["trades"]      = trades
                    if equity:      self.data["equity"]      = equity
                    if open_trades: self.data["open_trades"] = open_trades
                    if control_raw: self.data["control"]     = control_raw if isinstance(control_raw, dict) else json.loads(control_raw)
                    if stats_raw:   self.data["stats"]       = stats_raw if isinstance(stats_raw, dict) else json.loads(stats_raw)
                    if ai_raw:      self.data["ai"]          = ai_raw if isinstance(ai_raw, dict) else json.loads(ai_raw)
                    ctrl = self._default_control()
                    ctrl.update(self.data.get("control") or {})
                    self.data["control"] = ctrl
                    print(f"[Experience] загружено из DB: {len(self.data['trades'])} сделок, "
                          f"{len(self.data['equity'])} точек капитала")
                    loaded_from_db = True
            except Exception as e:
                logger.warning(f"[Experience] DB load error: {e}")

        # ── Fallback / миграция: читаем JSON ─────────────────────────────────
        if not loaded_from_db:
            try:
                if not os.path.exists(self.path):
                    return
                with open(self.path, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                for k in ("trades", "open_trades", "equity", "stats", "ai", "control", "created"):
                    if k in disk and disk[k] is not None:
                        self.data[k] = disk[k]
                ctrl = self._default_control()
                ctrl.update(self.data.get("control") or {})
                self.data["control"] = ctrl
                print(f"[Experience] загружено из JSON: {len(self.data['trades'])} сделок, "
                      f"{len(self.data['equity'])} точек капитала")
                # Миграция JSON → DB (однократно)
                if db:
                    self._migrate_to_db(db)
            except Exception as e:
                print(f"[Experience] ошибка чтения {self.path}: {e}")

    def _migrate_to_db(self, db):
        """Однократный перенос данных из JSON в PostgreSQL."""
        try:
            trades  = self.data.get("trades") or []
            equity  = self.data.get("equity") or []
            open_ts = self.data.get("open_trades") or []
            ctrl    = self.data.get("control") or {}
            stats   = self.data.get("stats") or {}
            ai      = self.data.get("ai") or {}
            if trades:  db.trades_bulk_insert(trades)
            if equity:  db.equity_bulk_insert(equity)
            if open_ts: db.open_trades_save(open_ts)
            if ctrl:    db.ai_state_set("control", ctrl)
            if stats:   db.ai_state_set("stats", stats)
            if ai:      db.ai_state_set("ai_export", ai)
            logger.info(f"[Experience] ✅ Мигрировано в DB: {len(trades)} сделок, {len(equity)} точек")
        except Exception as e:
            logger.warning(f"[Experience] migrate_to_db error: {e}")

    def _save_locked(self):
        # JSON (локальный backup)
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[Experience] ошибка записи {self.path}: {e}")
        # DB: control + stats (AI export сохраняется отдельно в record_trade)
        db = _db()
        if db:
            try:
                ctrl  = self.data.get("control") or {}
                stats = self.data.get("stats") or {}
                if ctrl:  db.ai_state_set("control", ctrl)
                if stats: db.ai_state_set("stats", stats)
            except Exception as e:
                logger.warning(f"[Experience] DB _save_locked error: {e}")

    def save(self):
        with self._lock:
            self.data["control"]["updated"] = datetime.utcnow().isoformat()
            self._save_locked()

    # ── Восстановление при старте ────────────────────────────────────────────
    def restore_trader(self, trader):
        """Возвращает статистику в трейдер и применяет сохранённые параметры
        управления к Config (тёплый старт)."""
        with self._lock:
            stats = self.data.get("stats") or {}
            if stats:
                for k in ("total_trades", "winning_trades", "total_pnl"):
                    if k in stats:
                        trader.stats[k] = stats[k]
            # ВОССТАНАВЛИВАЕМ открытые позиции: цена покупки + цель продажи —
            # чтобы после перезапуска бот знал почём купил и НЕ продал дешевле.
            open_trades = [dict(t) for t in (self.data.get("open_trades") or [])]
            self._apply_control_to_config()
            ctrl = self.data["control"]
        if open_trades:
            trader.open_trades = open_trades
            # Чтобы открытые позиции были видны в истории и корректно
            # обновились при закрытии (поиск по id).
            existing_ids = {t.get("id") for t in trader.trades}
            for t in open_trades:
                if t.get("id") not in existing_ids:
                    trader.trades.append(dict(t))
        try:
            note = (
                f"🧠 Память загружена: {len(self.data['trades'])} сделок"
                + (f" | ⏳ {len(open_trades)} открытых позиций восстановлено"
                   if open_trades else "")
                + f" | порог={ctrl['min_conf']:.0f}% ставка={ctrl['trade_amount']:.3f} | "
                f"{'⏸️ ПАУЗА' if ctrl['paused'] else '▶️ активна'}"
            )
            trader.log(note, "INFO")
            for t in open_trades:
                trader.log(
                    f"   ↩️ Позиция: куплено {t.get('amount')} @ {t.get('entry_price')} "
                    f"→ продать не дешевле цели TP={t.get('take_profit')}",
                    "INFO",
                )
        except Exception:  # noqa: BLE001
            pass

    def save_open_trades(self, open_trades):
        """АВТО-СОХРАНЕНИЕ открытых позиций при КАЖДОЙ сделке (открытие/закрытие).
        Хранит цену покупки и цель продажи, чтобы пережить перезапуск."""
        trades_list = [dict(t) for t in (open_trades or [])]
        with self._lock:
            self.data["open_trades"] = trades_list
            self._save_locked()
        db = _db()
        if db:
            try:
                db.open_trades_save(trades_list)
            except Exception as e:
                logger.warning(f"[Experience] DB save_open_trades error: {e}")

    def get_cost_basis(self) -> float | None:
        """Средневзвешенная цена покупки открытых позиций (None если нет).
        Ликвидатор использует её как ОПОРНУЮ, чтобы не продать дешевле покупки."""
        with self._lock:
            ots = self.data.get("open_trades") or []
        total_amt  = sum(float(t.get("amount", 0) or 0) for t in ots)
        if total_amt <= 0:
            return None
        total_cost = sum(
            float(t.get("entry_price", 0) or 0) * float(t.get("amount", 0) or 0)
            for t in ots
        )
        return total_cost / total_amt if total_cost > 0 else None

    def restore_ai(self, ai):
        """Отдаёт сохранённый опыт обратно в ИИ (после pretrain)."""
        with self._lock:
            ai_data = self.data.get("ai") or {}
        try:
            n = ai.import_experience(ai_data)
            return n
        except Exception as e:  # noqa: BLE001
            print(f"[Experience] restore_ai error: {e}")
            return 0

    def ai_memory_summary(self) -> dict:
        """СВЕРКА памяти ИИ перед восстановлением: что лежит на диске.
        Используется для наглядного лога при перезапуске, чтобы было видно —
        опыт сохранён и подхватывается, обучение НЕ начинается с нуля."""
        with self._lock:
            ai = dict(self.data.get("ai") or {})
            trades = len(self.data.get("trades") or [])
        confirmed = len(ai.get("confirmed_X") or [])
        slot_acc = ai.get("slot_acc") or {}
        accs = [sum(h) / len(h) for h in slot_acc.values() if h]
        avg = round(sum(accs) / len(accs) * 100, 1) if accs else None
        return {
            "trades":       trades,
            "confirmed":    confirmed,
            "feature_dim":  ai.get("feature_dim"),
            "avg_accuracy": avg,
        }

    def _apply_control_to_config(self):
        ctrl = self.data["control"]
        try:
            Config.MIN_AI_CONFIDENCE = float(ctrl["min_conf"])
            Config.TRADE_AMOUNT      = float(ctrl["trade_amount"])
        except Exception:  # noqa: BLE001
            pass

    def set_baseline(self, min_conf=None, trade_amount=None):
        """Когда пользователь меняет настройки вручную (UI/API) — обновляем
        опорные значения, от которых адаптируется ИИ, чтобы он не тянул
        параметры обратно к устаревшим значениям."""
        with self._lock:
            ctrl = self.data["control"]
            if min_conf is not None:
                ctrl["base_min_conf"] = float(min_conf)
                ctrl["min_conf"]      = float(min_conf)
            if trade_amount is not None:
                ctrl["base_trade_amount"] = float(trade_amount)
                ctrl["trade_amount"]      = float(trade_amount)
            self._save_locked()

    # ── Публичное состояние ──────────────────────────────────────────────────
    def is_paused(self) -> bool:
        with self._lock:
            return bool(self.data["control"].get("paused"))

    def get_report(self) -> dict:
        with self._lock:
            trades = self.data["trades"]
            wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
            net  = round(sum((t.get("pnl") or 0) for t in trades), 6)
            ctrl = dict(self.data["control"])
            equity = self.data["equity"]
            return {
                "trades_count":  len(trades),
                "wins":          wins,
                "losses":        len(trades) - wins,
                "win_rate":      round(wins / len(trades) * 100, 1) if trades else 0.0,
                "net_pnl_ton":   net,
                "control":       ctrl,
                "equity_points": len(equity),
                "last_equity":   equity[-1] if equity else None,
                "recent_trades": trades[-10:],
            }

    # ── Запись сделки ────────────────────────────────────────────────────────
    def record_trade(self, trade: dict, stats: dict, ai=None):
        with self._lock:
            trade_rec = {
                "id":          trade.get("id"),
                "entry_price": trade.get("entry_price"),
                "exit_price":  trade.get("exit_price"),
                "amount":      trade.get("amount"),
                "pnl":         trade.get("pnl"),
                "fee":         trade.get("fee"),
                "reason":      trade.get("close_reason"),
                "opened_at":   trade.get("opened_at"),
                "closed_at":   trade.get("closed_at"),
            }
            self.data["trades"].append(trade_rec)
            if len(self.data["trades"]) > MAX_TRADES_KEPT:
                self.data["trades"] = self.data["trades"][-MAX_TRADES_KEPT:]
            if stats:
                self.data["stats"] = dict(stats)
            if ai is not None:
                try:
                    self.data["ai"] = ai.export_experience()
                except Exception as e:  # noqa: BLE001
                    print(f"[Experience] export_experience error: {e}")
            self._save_locked()
            # DB: уписываем сделку + AI-опыт
            db = _db()
            if db:
                try:
                    db.trades_upsert(trade_rec)
                    if self.data.get("ai"):
                        db.ai_state_set("ai_export", self.data["ai"])
                except Exception as e:
                    logger.warning(f"[Experience] DB record_trade error: {e}")

    # ── Запись капитала (кривая баланса) ─────────────────────────────────────
    def record_balance(self, balance: dict, grinch_price_usd: float, force: bool = False):
        now = time.time()
        if not force and (now - self._last_equity_ts) < EQUITY_MIN_GAP:
            return
        self._last_equity_ts = now
        try:
            from price_feed import price_feed
            ton_usd = price_feed.get("TON") or 0.0
        except Exception:  # noqa: BLE001
            ton_usd = 0.0
        ton    = float(balance.get("TON", 0) or 0)
        grinch = float(balance.get("GRINCH", 0) or 0)
        gp     = float(grinch_price_usd or 0)
        # Если котировка TON недоступна, а GRINCH на балансе есть — НЕ пишем
        # точку: иначе капитал «схлопнется» до TON-only и даст ложную просадку
        # → ошибочную паузу торговли.
        if grinch > 0 and (ton_usd <= 0 or gp <= 0):
            return
        equity_ton = ton + (grinch * gp / ton_usd if ton_usd else 0.0)
        point = {
            "t":          datetime.utcnow().isoformat(),
            "ton":        round(ton, 6),
            "grinch":     round(grinch, 4),
            "grinch_usd": gp,
            "equity_ton": round(equity_ton, 6),
        }
        with self._lock:
            self.data["equity"].append(point)
            if len(self.data["equity"]) > MAX_EQUITY_KEPT:
                self.data["equity"] = self.data["equity"][-MAX_EQUITY_KEPT:]
            ctrl = self.data["control"]
            if equity_ton > ctrl.get("peak_equity", 0):
                ctrl["peak_equity"] = round(equity_ton, 6)
            self._save_locked()
            # DB: вставляем только новую точку (не перезаписываем всю историю)
            db = _db()
            if db:
                try:
                    db.equity_insert(point)
                except Exception as e:
                    logger.warning(f"[Experience] DB equity_insert error: {e}")

    # ── Анализ опыта и адаптация управления («супер-ИИ управление») ──────────
    def analyze_and_adapt(self, trader=None, ai=None) -> dict:
        with self._lock:
            trades = self.data["trades"]
            ctrl   = self.data["control"]
            equity = self.data["equity"]

            base_conf = float(ctrl["base_min_conf"])
            base_amt  = float(ctrl["base_trade_amount"])

            # — серия убытков подряд (с конца журнала) —
            streak = 0
            for t in reversed(trades):
                if (t.get("pnl") or 0) < 0:
                    streak += 1
                else:
                    break
            # — серия ПРИБЫЛЬНЫХ сделок подряд (для безопасного роста ставки) —
            win_streak = 0
            for t in reversed(trades):
                if (t.get("pnl") or 0) > 0:
                    win_streak += 1
                else:
                    break

            recent = trades[-RECENT_WINDOW:]
            recent_net = sum((t.get("pnl") or 0) for t in recent)

            # — просадка от пика капитала —
            peak = ctrl.get("peak_equity", 0) or 0
            cur_eq = equity[-1]["equity_ton"] if equity else peak
            drawdown = ((peak - cur_eq) / peak * 100) if peak > 0 else 0.0
            drawdown = max(0.0, drawdown)

            # — порог уверенности: строже после убытков —
            conf = base_conf
            if streak >= 2:
                conf += min(3.0 * streak, 15.0)
            if recent_net < 0:
                conf += 5.0
            conf = max(base_conf, min(conf, CONF_CAP))

            # — размер ставки: РАСТЁТ на серии прибыли, СОКРАЩАЕТСЯ при просадке —
            # Рост работает ТОЛЬКО в спокойном режиме (малая просадка) и строго
            # ограничен потолком GROW_CAP. Защита капитала важнее роста: при любой
            # заметной просадке ставка ужимается независимо от серии побед.
            amt = base_amt
            if drawdown < DD_SHRINK_1 and recent_net > 0:
                if win_streak >= WIN_GROW_2:
                    amt = base_amt * GROW_CAP
                elif win_streak >= WIN_GROW_1:
                    amt = base_amt * 1.25
            if drawdown >= DD_SHRINK_2:
                amt = base_amt * 0.35
            elif drawdown >= DD_SHRINK_1:
                amt = base_amt * 0.60
            amt = max(base_amt * 0.25, min(amt, base_amt * GROW_CAP))

            # — пауза при сильной просадке (гистерезис) —
            paused = bool(ctrl.get("paused"))
            if drawdown >= DD_PAUSE:
                paused = True
            elif drawdown <= DD_RESUME:
                paused = False

            changed = (
                round(conf, 2) != round(ctrl["min_conf"], 2)
                or round(amt, 4) != round(ctrl["trade_amount"], 4)
                or paused != ctrl["paused"]
            )

            ctrl["min_conf"]     = round(conf, 2)
            ctrl["trade_amount"] = round(amt, 4)
            ctrl["paused"]       = paused
            ctrl["drawdown_pct"] = round(drawdown, 2)
            ctrl["loss_streak"]  = streak
            ctrl["last_note"]    = (
                f"DD={drawdown:.1f}% loss={streak} win={win_streak} "
                f"recent_net={recent_net:+.4f}"
            )
            self._apply_control_to_config()
            self._save_locked()
            report = dict(ctrl)

        if changed and trader is not None:
            try:
                trader.log(
                    f"🤖 ИИ-управление: порог={report['min_conf']:.0f}% "
                    f"ставка={report['trade_amount']:.3f} TON "
                    f"{'⏸️ ПАУЗА покупок' if report['paused'] else '▶️ активна'} "
                    f"| {report['last_note']}",
                    "INFO",
                )
            except Exception:  # noqa: BLE001
                pass
        return report


# ── Синглтон ─────────────────────────────────────────────────────────────────
experience_manager = ExperienceManager()
