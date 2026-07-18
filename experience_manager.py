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

try:
    import numpy as _np
    class _NpEncoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, _np.integer): return int(o)
            if isinstance(o, _np.floating): return float(o)
            if isinstance(o, _np.bool_): return bool(o)
            if isinstance(o, _np.ndarray): return o.tolist()
            return super().default(o)
    def _jdump(obj, f, **kw): return json.dump(obj, f, cls=_NpEncoder, **kw)
except ImportError:
    def _jdump(obj, f, **kw): return json.dump(obj, f, **kw)

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
EQUITY_MIN_GAP    = 30       # не чаще раза в N секунд писать точку капитала
RECENT_WINDOW     = 5        # окно «недавних» сделок — быстрее реагирует (было 10)
CONF_CAP          = 90.0     # потолок порога уверенности
DD_SHRINK_1       = 8.0      # просадка 8% → уменьшаем ставку (было 10)
DD_SHRINK_2       = 18.0     # просадка 18% → сильно уменьшаем ставку (было 20)
DD_PAUSE          = 28.0     # просадка 28% → пауза новых покупок (было 30)
DD_RESUME         = 12.0     # просадка 12% → снимаем паузу (было 15)
# — безопасный РОСТ ставки на доказанной прибыли (только в спокойном режиме) —
WIN_GROW_1        = 3        # серия прибыльных сделок → ставка +25%
WIN_GROW_2        = 5        # серия 5 побед → ставка до потолка (было 6)
GROW_CAP          = 1.8      # потолок множителя ставки (×1.8 от базовой, было 1.5)
# Режимы рынка для per-regime трекинга
REGIME_KEYS       = ("UPTREND", "DOWNTREND", "RANGING", "VOLATILE", "BREAKOUT", "SQUEEZE", "TRANSITION")


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
            # Авто-TP: ИИ сам подбирает оптимальный тейк-профит по истории
            "take_profit_pct":        float(Config.TAKE_PROFIT_PCT),
            "ai_tp_adapted":          False,    # True когда ИИ уже адаптировал TP
            "ai_tp_trades_used":      0,        # сколько сделок учтено в последней адаптации
            "ai_avg_win_pct":         0.0,      # средний % прибыли в выигрышных сделках
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
                    if stats_raw:
                        _s = stats_raw if isinstance(stats_raw, dict) else json.loads(stats_raw)
                        # Санитайз при загрузке: winning_trades ≤ total_trades (атомарно,
                        # до того как любой фоновый поток успеет прочитать self.data["stats"])
                        _wt = int(_s.get("winning_trades", 0) or 0)
                        _tt = int(_s.get("total_trades", 0) or 0)
                        if _wt > _tt:
                            _s = dict(_s)
                            _s["winning_trades"] = _tt
                            logger.warning(
                                f"[Experience] 🔧 _load: winning_trades ({_wt}) > total_trades ({_tt})"
                                f" — исправлено до {_tt} при загрузке из DB"
                            )
                        self.data["stats"] = _s
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
                # Санитайз stats из JSON — поля могут быть null если файл был записан
                # в момент сбоя или устаревшей версией без защиты
                if isinstance(self.data.get("stats"), dict):
                    _sj = self.data["stats"]
                    _sj["total_trades"]   = int(_sj.get("total_trades")   or 0)
                    _sj["winning_trades"] = int(_sj.get("winning_trades") or 0)
                    _sj["total_pnl"]      = float(_sj.get("total_pnl")    or 0.0)
                    if _sj["winning_trades"] > _sj["total_trades"]:
                        _sj["winning_trades"] = _sj["total_trades"]
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
            # ЗАЩИТА: не перезаписывать open_trades в БД данными из JSON,
            # если в БД уже есть позиции. Это предотвращает ситуацию, когда
            # кратковременный сбой соединения на старте приводит к тому, что
            # _migrate_to_db стирает актуальные позиции из БД и заменяет
            # их устаревшим содержимым experience.json.
            if open_ts:
                existing_db_ots = db.open_trades_get()
                if not existing_db_ots:
                    db.open_trades_save(open_ts)
                    logger.info(f"[Experience] Мигрировано open_trades из JSON: {len(open_ts)} позиций")
                else:
                    logger.info(
                        f"[Experience] open_trades пропущены при миграции — "
                        f"в БД уже {len(existing_db_ots)} позиций (JSON не перезаписывает БД)"
                    )
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
                _jdump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[Experience] ошибка записи {self.path}: {e}")
        # DB: control + stats (AI export сохраняется отдельно в record_trade)
        db = _db()
        if db:
            try:
                ctrl  = self.data.get("control") or {}
                stats = self.data.get("stats") or {}
                # Инвариант: winning_trades ≤ total_trades (иначе winrate >100%)
                # Проверяем здесь потому что _save_locked вызывается из analyze_and_adapt
                # ДО того как санитайзер в trader.__init__ успевает исправить значение.
                if stats:
                    _tt = int(stats.get("total_trades", 0) or 0)
                    _wt = int(stats.get("winning_trades", 0) or 0)
                    if _wt > _tt:
                        stats = dict(stats)
                        stats["winning_trades"] = _tt
                        self.data["stats"] = stats
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
            # Восстанавливаем накопленные счётчики (total_trades / winning_trades / total_pnl)
            # из DB при каждом рестарте, чтобы дашборд показывал ВСЕ исторические сделки,
            # а не только текущую сессию. Исторические данные хранятся в bot_ai_state["stats"].
            saved_stats = self.data.get("stats") or {}
            if saved_stats and isinstance(saved_stats, dict):
                for _k in ("total_trades", "winning_trades", "total_pnl"):
                    if _k in saved_stats and saved_stats[_k] is not None:
                        trader.stats[_k] = saved_stats[_k]
            # Гарантируем что trader.stats не содержит None — защита от
            # устаревших БД-записей где поля были сохранены как null
            trader.stats["total_trades"]   = int(trader.stats.get("total_trades")   or 0)
            trader.stats["winning_trades"] = int(trader.stats.get("winning_trades") or 0)
            trader.stats["total_pnl"]      = float(trader.stats.get("total_pnl")    or 0.0)
            if trader.stats["winning_trades"] > trader.stats["total_trades"]:
                trader.stats["winning_trades"] = trader.stats["total_trades"]
            # ВОССТАНАВЛИВАЕМ открытые позиции: цена покупки + цель продажи —
            # чтобы после перезапуска бот знал почём купил и НЕ продал дешевле.
            all_open = [dict(t) for t in (self.data.get("open_trades") or [])]
            # LONG и SHORT хранятся вместе в одной таблице (bot_open_trades) —
            # разделяем их обратно по trade_type, иначе SHORT-позиции терялись
            # при каждом рестарте бота.
            open_trades       = [t for t in all_open if t.get("trade_type") != "short"]
            open_short_trades = [t for t in all_open if t.get("trade_type") == "short"]
            # ── Само-исцеление: исправляем сохранённый TP если он > 10× цены входа ──
            # Это происходит когда позиция была открыта с очень маленькой ставкой
            # и required_gross_pct_with_gas вернул тысячи процентов (газ > ставки).
            # В режиме ONLY_PROFIT_EXIT поле take_profit не влияет на логику выхода,
            # но неверное значение путает лог и dashboard.
            try:
                from config import Config as _Cfg
                _healed = 0
                for _t in open_trades + open_short_trades:
                    _ep = float(_t.get("entry_price") or 0)
                    _tp = float(_t.get("take_profit") or 0)
                    _st = float(_t.get("stake_ton") or 0)
                    if _ep > 0 and _tp / _ep > 10:
                        _mg = _Cfg.required_gross_pct_with_gas(_st if _st > 0 else None)
                        _tp_pct = max(_Cfg.TAKE_PROFIT_PCT, _mg)
                        _t["take_profit"] = round(_ep * (1 + _tp_pct / 100), 8)
                        _healed += 1
                if _healed:
                    logger.info(f"[Experience] 🔧 Исправлено {_healed} некорректных TP при загрузке")
                    self._save_locked()
            except Exception:
                pass
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
        if open_short_trades:
            trader.open_short_trades = open_short_trades
        # ── Восстанавливаем ЗАКРЫТЫЕ сделки в trader.trades ──────────────────
        # trader.trades — чисто оперативный список (обнуляется в self.trades = []
        # при каждом запуске Trader()), а дашборд ("История сделок") берёт
        # recent_trades именно из него, а не из журнала на диске/в БД.
        # Раньше сюда попадали только открытые позиции → после КАЖДОГО
        # рестарта уже закрытые (и прибыльные) сделки пропадали из истории на
        # дашборде, хотя счётчики (Сделок/Win Rate/P&L) их учитывали.
        try:
            with self._lock:
                journal_closed = [dict(t) for t in (self.data.get("trades") or [])
                                   if t.get("status") == "closed" or t.get("closed_at")]
            existing_ids2 = {t.get("id") for t in trader.trades}
            restored_closed = 0
            for t in journal_closed[-50:]:
                if t.get("id") not in existing_ids2:
                    trader.trades.append(t)
                    existing_ids2.add(t.get("id"))
                    restored_closed += 1
            if restored_closed:
                trader.log(f"🗂️ История сделок восстановлена: {restored_closed} закрытых сделок", "INFO")
        except Exception as _hist_err:
            logger.warning(f"[Experience] restore closed trades error: {_hist_err}")
        try:
            # Показываем ставку, актуальную для текущего режима:
            # в DCA — стейк на вход из конфига, в AI-режиме — адаптивная ставка.
            try:
                from config import Config as _Cfg
                _dca = _Cfg.DCA_MODE
                _stake_label = (f"ставка DCA={_Cfg.DCA_STAKE_TON:.0f} TON/вход"
                                if _dca else f"ставка AI={ctrl['trade_amount']:.3f} TON")
            except Exception:
                _dca = False
                _stake_label = f"ставка={ctrl['trade_amount']:.3f}"
            note = (
                f"🧠 Память загружена: {len(self.data['trades'])} сделок"
                + (f" | ⏳ {len(open_trades)} LONG восстановлено" if open_trades else "")
                + (f" | 📉 {len(open_short_trades)} SHORT восстановлено" if open_short_trades else "")
                + f" | порог={ctrl['min_conf']:.0f}% {_stake_label}"
                # «AI-пауза» — авто-пауза по статистике, не то же что ручной выключатель торговли.
                # Ручной статус («торговля вкл/выкл») логируется отдельно при старте агента.
                + f" | {'⏸️ AI-пауза активна' if ctrl['paused'] else '▶️ AI-пауза: нет'}"
            )
            trader.log(note, "INFO")
            for t in open_trades:
                trader.log(
                    f"   ↩️ Позиция: куплено {t.get('amount')} @ {t.get('entry_price')} "
                    f"→ продать не дешевле цели TP={t.get('take_profit')}",
                    "INFO",
                )
            for t in open_short_trades:
                trader.log(
                    f"   ↩️ SHORT: продано {t.get('amount')} @ {t.get('entry_price')} "
                    f"→ откупить не дороже TP={t.get('take_profit')}",
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
            # Применяем авто-TP только если ИИ уже адаптировал его по реальной истории
            if ctrl.get("ai_tp_adapted") and ctrl.get("take_profit_pct"):
                new_tp = float(ctrl["take_profit_pct"])
                if new_tp > 0:
                    Config.TAKE_PROFIT_PCT = new_tp
                    # TARGET_NET_PCT = TP - комиссия (оба пула по 1%)
                    Config.TARGET_NET_PCT = max(1.0, new_tp - Config.FEE_ROUND_TRIP)
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
            # Храним полный dict сделки — AI-аналитике нужны ВСЕ поля
            # (stake_ton, ai_confidence, SL/TP, regime, RSI при закрытии и т.д.)
            trade_rec = dict(trade)
            # Гарантируем ключи, которые читает experience_manager.analyze_and_adapt
            trade_rec.setdefault("id",          trade.get("id"))
            trade_rec.setdefault("entry_price", trade.get("entry_price"))
            trade_rec.setdefault("exit_price",  trade.get("exit_price"))
            trade_rec.setdefault("amount",      trade.get("amount"))
            trade_rec.setdefault("pnl",         trade.get("pnl", 0))
            trade_rec.setdefault("fee",         trade.get("fee"))
            trade_rec.setdefault("reason",      trade.get("close_reason"))
            trade_rec.setdefault("opened_at",   trade.get("opened_at"))
            trade_rec.setdefault("closed_at",   trade.get("closed_at"))
            self.data["trades"].append(trade_rec)
            if len(self.data["trades"]) > MAX_TRADES_KEPT:
                self.data["trades"] = self.data["trades"][-MAX_TRADES_KEPT:]
            if stats:
                _s = dict(stats)
                # Инвариант: winning_trades ≤ total_trades (защита от race condition
                # когда запись_trade вызывается с устаревшими/некорректными stats)
                _wt2 = int(_s.get("winning_trades", 0) or 0)
                _tt2 = int(_s.get("total_trades", 0) or 0)
                if _wt2 > _tt2:
                    _s["winning_trades"] = _tt2
                    logger.warning(
                        f"[Experience] record_trade sanitize: winning({_wt2})>total({_tt2})"
                        f" — winning исправлено до {_tt2}"
                    )
                self.data["stats"] = _s
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
        # Защита от «битого» снимка баланса: TON=0 при ненулевом GRINCH почти
        # всегда означает временный сбой чтения on-chain баланса (реальный
        # кошелёк с открытой позицией всегда держит газовый резерв, TON
        # никогда не бывает ровно 0). Не пишем такую точку — иначе капитал на
        # графике «проваливается» до нуля и сразу же возвращается обратно.
        if ton == 0 and grinch > 0:
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

            # ── Per-regime win rate tracking ──────────────────────────────
            regime_stats = ctrl.get("regime_stats") or {}
            for t in trades[-20:]:   # обновляем по последним 20 сделкам
                reg = (t.get("regime") or t.get("entry_regime") or "UNKNOWN").upper()
                if reg not in REGIME_KEYS:
                    reg = "TRANSITION"
                rs = regime_stats.setdefault(reg, {"wins": 0, "total": 0, "net": 0.0})
                if t.get("pnl") is not None:
                    rs["total"] += 1
                    if (t.get("pnl") or 0) > 0:
                        rs["wins"] += 1
                    rs["net"] = round(rs.get("net", 0.0) + (t.get("pnl") or 0), 4)
            # Дедупликация: берём только уникальные combo из последних 20 сделок
            # (повторный анализ завышает счётчики) — перестраиваем с нуля каждый раз
            regime_stats = {}
            for t in trades[-50:]:
                reg = (t.get("regime") or t.get("entry_regime") or "UNKNOWN").upper()
                if reg not in REGIME_KEYS:
                    reg = "TRANSITION"
                rs = regime_stats.setdefault(reg, {"wins": 0, "total": 0, "net": 0.0})
                if t.get("pnl") is not None:
                    rs["total"] += 1
                    if (t.get("pnl") or 0) > 0:
                        rs["wins"] += 1
                    rs["net"] = round(rs.get("net", 0.0) + (t.get("pnl") or 0), 4)
            # WR по режиму для текущей торговли
            for reg, rs in regime_stats.items():
                if rs["total"] > 0:
                    rs["wr"] = round(rs["wins"] / rs["total"] * 100, 1)

            # ── Sharpe ratio из equity curve ──────────────────────────────
            sharpe = 0.0
            if len(equity) >= 10:
                try:
                    eq_vals = [e["equity_ton"] for e in equity[-100:]]
                    returns = [(eq_vals[i] - eq_vals[i-1]) / (eq_vals[i-1] + 1e-10)
                               for i in range(1, len(eq_vals))]
                    if returns:
                        import statistics
                        mu_r  = sum(returns) / len(returns)
                        std_r = statistics.stdev(returns) if len(returns) > 1 else 1e-10
                        sharpe = round(mu_r / (std_r + 1e-10) * (len(returns) ** 0.5), 2)
                except Exception:
                    sharpe = 0.0

            # — порог уверенности: строже после убытков —
            conf = base_conf
            if streak >= 2:
                conf += min(3.0 * streak, 15.0)
            if recent_net < 0:
                conf += 5.0
            # При отрицательном Sharpe → дополнительная осторожность
            if sharpe < -0.5:
                conf += 5.0
            conf = max(base_conf, min(conf, CONF_CAP))

            # — размер ставки: Sharpe × win-streak × drawdown-защита —
            # Рост работает ТОЛЬКО в спокойном режиме (малая просадка) и строго
            # ограничен потолком GROW_CAP. Защита капитала важнее роста: при любой
            # заметной просадке ставка ужимается независимо от серии побед.
            amt = base_amt
            if drawdown < DD_SHRINK_1 and recent_net > 0:
                if win_streak >= WIN_GROW_2:
                    # Sharpe > 1 = мы в выгодной полосе, можно чуть агрессивнее
                    cap_mult = min(GROW_CAP * (1.0 + max(0, sharpe) * 0.1), GROW_CAP * 1.2)
                    amt = base_amt * cap_mult
                elif win_streak >= WIN_GROW_1:
                    amt = base_amt * 1.25
            if drawdown >= DD_SHRINK_2:
                amt = base_amt * 0.35
            elif drawdown >= DD_SHRINK_1:
                amt = base_amt * 0.60
            # Sharpe < -1 = плохая полоса → дополнительное сокращение ставки
            if sharpe < -1.0 and drawdown >= 5.0:
                amt = min(amt, base_amt * 0.50)
            amt = max(base_amt * 0.25, min(amt, base_amt * GROW_CAP))

            # — пауза при сильной просадке (гистерезис) —
            paused = bool(ctrl.get("paused"))
            if drawdown >= DD_PAUSE:
                paused = True
            elif drawdown <= DD_RESUME:
                paused = False

            # ── Авто-адаптация тейк-профита по реальной истории ──────────────
            # Пол: MIN_PROFIT_TON задаётся как ПРОЦЕНТ от ставки (5 = 5% всегда).
            # 100 TON × 5% = 5 TON минимум; 200 TON × 5% = 10 TON минимум и т.д.
            min_profit_floor_pct = float(Config.MIN_PROFIT_TON)   # трактуем как %
            # Добавляем комиссию: чтобы НЕТТО был ≥ порогу, gross = нетто + fee_round_trip
            min_tp_gross = min_profit_floor_pct + Config.FEE_ROUND_TRIP

            new_tp = float(ctrl.get("take_profit_pct") or Config.TAKE_PROFIT_PCT)
            prev_tp = new_tp   # запоминаем ДО адаптации — TP может только расти
            ai_tp_adapted = bool(ctrl.get("ai_tp_adapted"))
            avg_win_pct = float(ctrl.get("ai_avg_win_pct") or 0.0)

            if len(trades) >= Config.AI_TP_ADAPT_MIN_TRADES:
                # Берём последние 30 закрытых сделок для статистики
                recent_closed = [t for t in trades[-30:] if (t.get("pnl") is not None)]
                win_trades = [t for t in recent_closed if (t.get("pnl") or 0) > 0]

                if win_trades:
                    # Средний возврат выигрышных сделок (в % от ставки)
                    returns = []
                    for t in win_trades:
                        stake = t.get("stake_ton") or t.get("amount") or amt
                        pnl   = float(t.get("pnl") or 0)
                        if stake and stake > 0:
                            returns.append(pnl / stake * 100.0)
                    if returns:
                        avg_win_pct = round(sum(returns) / len(returns), 2)
                        wr = len(win_trades) / max(len(recent_closed), 1)

                        # Оптимальный TP: медианная прибыль × поправка на win rate
                        # Высокий WR → можем поставить цель выше (рынок предсказуем)
                        # Низкий WR → нужно брать прибыль быстрее (ниже, но не ниже пола)
                        if wr >= 0.65:
                            # ≥65% WR: ставим цель 85% от средней прибыли (с запасом)
                            optimal_tp = avg_win_pct * 0.85
                        elif wr >= 0.5:
                            # 50-64% WR: целимся в 70% от средней прибыли
                            optimal_tp = avg_win_pct * 0.70
                        else:
                            # < 50% WR: берём быстро — 55% от средней прибыли
                            optimal_tp = avg_win_pct * 0.55

                        # Ограничиваем диапазон: не ниже пола + комиссия, не выше потолка
                        optimal_tp = max(min_tp_gross, min(optimal_tp, Config.AI_TP_CAP_PCT))

                        # Плавная адаптация: не прыгаем резко, смешиваем с текущим TP
                        # При первой адаптации — берём вычисленное значение сразу
                        if ai_tp_adapted:
                            new_tp = round(new_tp * 0.7 + optimal_tp * 0.3, 2)
                        else:
                            new_tp = round(optimal_tp, 2)
                        ai_tp_adapted = True

            # Гарантируем пол в любом случае (даже если история пустая)
            new_tp = max(min_tp_gross, new_tp)
            # ТОЛЬКО В ПЛЮС: TP никогда не понижается автоматически.
            # Если адаптация дала бы меньший TP — оставляем текущий.
            new_tp = max(prev_tp, new_tp)
            new_tp = round(new_tp, 2)

            # ── Полная адаптация: DCA-цель и порог защиты прибыли ────────────
            # Адаптируем DCA_TARGET_PROFIT_PCT и PROFIT_PROTECT_TON по истории сделок.
            # Правило «только в плюс»:
            #   DCA_TARGET — только ВВЕРХ (ставим выше при хорошей истории)
            #   PROFIT_PROTECT_TON — только ВНИЗ (ловим прибыль раньше, не выше)
            if len(trades) >= Config.AI_TP_ADAPT_MIN_TRADES and win_trades:
                # Средняя выигрышная прибыль в TON
                avg_win_ton = 0.0
                for t in win_trades:
                    avg_win_ton += float(t.get("pnl") or 0)
                avg_win_ton = avg_win_ton / len(win_trades)

                # ── DCA цель: поднимаем если средняя победа > текущей цели  ──
                try:
                    cur_dca_target = float(Config.DCA_TARGET_PROFIT_PCT)
                    # avg_win_pct = средняя победа в % от ставки (уже вычислена выше)
                    if avg_win_pct > cur_dca_target * 1.1 and avg_win_pct > 0:
                        # Победы стабильно выше цели → поднимаем цель на 20% от разницы
                        new_dca_target = round(
                            cur_dca_target + (avg_win_pct - cur_dca_target) * 0.2, 1
                        )
                        new_dca_target = min(new_dca_target, Config.DCA_AI_TARGET_CAP)
                        if new_dca_target > cur_dca_target:
                            Config.DCA_TARGET_PROFIT_PCT = new_dca_target
                            ctrl["ai_dca_target_adapted"] = True
                except Exception:
                    pass

                # ── Защита прибыли: снижаем порог если средняя победа небольшая  ──
                try:
                    cur_protect = float(Config.PROFIT_PROTECT_TON)
                    # Если средняя победа > 0, опускаем порог до 50% от средней победы
                    if avg_win_ton > 0:
                        optimal_protect = round(max(0.5, avg_win_ton * 0.5), 2)
                        if optimal_protect < cur_protect:
                            # Только вниз — ловим прибыль раньше
                            Config.PROFIT_PROTECT_TON = optimal_protect
                            ctrl["ai_protect_adapted"] = True
                except Exception:
                    pass
            tp_changed = abs(new_tp - float(ctrl.get("take_profit_pct") or 0)) > 0.1

            changed = (
                round(conf, 2) != round(ctrl["min_conf"], 2)
                or round(amt, 4) != round(ctrl["trade_amount"], 4)
                or paused != ctrl["paused"]
                or tp_changed
            )

            ctrl["min_conf"]           = round(conf, 2)
            ctrl["trade_amount"]       = round(amt, 4)
            ctrl["paused"]             = paused
            ctrl["drawdown_pct"]       = round(drawdown, 2)
            ctrl["loss_streak"]        = streak
            ctrl["win_streak"]         = win_streak
            ctrl["take_profit_pct"]    = new_tp
            ctrl["ai_tp_adapted"]      = ai_tp_adapted
            ctrl["ai_tp_trades_used"]  = len(trades)
            ctrl["ai_avg_win_pct"]     = avg_win_pct
            ctrl["min_profit_floor_pct"] = min_profit_floor_pct
            ctrl["sharpe"]             = sharpe
            ctrl["regime_stats"]       = regime_stats
            ctrl["last_note"]          = (
                f"DD={drawdown:.1f}% loss={streak} win={win_streak} "
                f"Sharpe={sharpe:+.2f} recent_net={recent_net:+.4f} "
                f"TP={new_tp:.1f}% (пол={min_profit_floor_pct:.1f}%)"
            )
            self._apply_control_to_config()
            self._save_locked()
            report = dict(ctrl)

        if changed and trader is not None:
            try:
                tp_note = f"TP={report['take_profit_pct']:.1f}% (пол {report.get('min_profit_floor_pct',0):.1f}%)"
                adapted_note = " 🎯 авто-TP" if report.get("ai_tp_adapted") else ""
                sharpe_note = f" Sharpe={report.get('sharpe', 0):+.2f}" if report.get('sharpe') is not None else ""
                trader.log(
                    f"🤖 ИИ-управление: порог={report['min_conf']:.0f}% "
                    f"ставка={report['trade_amount']:.3f} TON "
                    f"{tp_note}{adapted_note}{sharpe_note} "
                    f"{'⏸️ ПАУЗА покупок' if report['paused'] else '▶️ активна'} "
                    f"| {report['last_note']}",
                    "INFO",
                )
            except Exception:  # noqa: BLE001
                pass
        return report


# ── Синглтон ─────────────────────────────────────────────────────────────────
experience_manager = ExperienceManager()
