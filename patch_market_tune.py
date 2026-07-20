#!/usr/bin/env python3
"""
Патч: перекалибровка параметров под ATR(15m)=4.67% + on-chain анализ китов.
Запускать прямо на VPS: python3 patch_market_tune.py
"""
import re, shutil, os

# ───────────────────────── config.py ─────────────────────────────────────────
CFG = "/opt/bot/config.py"
shutil.copy(CFG, CFG + ".bak_market_tune")
txt = open(CFG, encoding="utf-8").read()

# ATR-блок в начале (комментарий с датой и данными)
OLD_ATR_COMMENT = (
    "    # GRINCH реал. Jul 2026 (обновлено 20.07.2026):\n"
    "    #   ATR(14,15m)=2.225%  ATR(14,1h)≈4-5% (харак.)  диапазон 24ч≈45%  7д≈126%\n"
    "    #   Топ-памп за 7д: +50% за одну свечу — значит цель 22-28% реалистична\n"
    "    #   Цель достижима за 3-5 свечей 1h, выше 2×ATR_1h шума (>8-10%)"
)
NEW_ATR_COMMENT = (
    "    # GRINCH реал. Jul 2026 (обновлено 20.07.2026, переизмерено 20.07.2026 ~18:00 UTC):\n"
    "    #   ATR(14,15m)=4.67%  ATR(14,1h)≈9-10% (характерный)  диапазон 25ч=39.2%  7д≈126%\n"
    "    #   Топ-памп за 7д: +50% за одну свечу — значит цель 22-28% реалистична\n"
    "    #   Цель достижима за 3-5 свечей 1h, выше 2×ATR_1h шума (>18-20%)"
)
txt = txt.replace(OLD_ATR_COMMENT, NEW_ATR_COMMENT, 1)

# SMART_TP_TIGHT_TRAIL_PCT: 7.0 → 10.0
txt = txt.replace(
    '    SMART_TP_TIGHT_TRAIL_PCT = float(os.getenv("SMART_TP_TIGHT_TRAIL_PCT", "7.0"))  # обновлено: 2×ATR_1h≈8-10% → 7% компромисс',
    '    SMART_TP_TIGHT_TRAIL_PCT = float(os.getenv("SMART_TP_TIGHT_TRAIL_PCT", "10.0"))  # 20.07 ATR(1h)≈9-10% → 1×ATR_1h=10% (выживает 1 свечу)',
)

# TRAIL_STAGE2_PCT: 10.0 → 17.0
txt = txt.replace(
    '    TRAIL_STAGE2_PCT    = float(os.getenv("TRAIL_STAGE2_PCT",   "10.0"))   # ОБНОВЛЕНО: 2×ATR(1h)хар.=8-10% — выдерживает откат в памп',
    '    TRAIL_STAGE2_PCT    = float(os.getenv("TRAIL_STAGE2_PCT",   "17.0"))   # 20.07: ATR(1h)≈9-10% → 2×ATR=18-20%, используем 17% (выдерживает откат в памп)',
)

# TRAIL_STAGE3_PCT: 7.5 → 12.0
txt = txt.replace(
    '    TRAIL_STAGE3_PCT    = float(os.getenv("TRAIL_STAGE3_PCT",    "7.5"))   # ОБНОВЛЕНО: 1.5×ATR(1h)хар. — более широкий трейл для удержания',
    '    TRAIL_STAGE3_PCT    = float(os.getenv("TRAIL_STAGE3_PCT",   "12.0"))   # 20.07: 1.5×ATR(1h)≈9% = 13.5% → 12% (компромисс, удержание позиции)',
)

# TRAILING_STOP_PCT: 9.0 → 11.0
txt = txt.replace(
    '    TRAILING_STOP_PCT   = float(os.getenv("TRAILING_STOP_PCT",   "9.0"))   # 4×ATR(15m)=8.9% — держит против шума 15m свечей',
    '    TRAILING_STOP_PCT   = float(os.getenv("TRAILING_STOP_PCT",  "11.0"))   # 20.07: ATR(1h)≈9-10% → 11% (пережить 1h-свечу шума)',
)

# TRAIL_CHOP_TIGHTEN: 0.8 → обновить комментарий (новый TRAILING_STOP=11% → 0.8×11%=8.8%)
txt = txt.replace(
    '    TRAIL_CHOP_TIGHTEN  = float(os.getenv("TRAIL_CHOP_TIGHTEN",  "0.8"))    # было 0.55 — не тянуть ниже 0.8×TRAILING_STOP(9%)=7.2%',
    '    TRAIL_CHOP_TIGHTEN  = float(os.getenv("TRAIL_CHOP_TIGHTEN",  "0.8"))    # не тянуть ниже 0.8×TRAILING_STOP(11%)=8.8%',
)

# DCA_DROP_TRIGGER_PCT: 8 → 10
txt = txt.replace(
    '    DCA_DROP_TRIGGER_PCT  = float(os.getenv("DCA_DROP_TRIGGER_PCT", "8"))   # TR(1h) p50=4.4% p75=7.3% → 8% = уверенное движение (выше p75)',
    '    DCA_DROP_TRIGGER_PCT  = float(os.getenv("DCA_DROP_TRIGGER_PCT", "10"))  # 20.07: ATR(1h)≈9-10% → 10% = пережить 1h-шум (p75 новый ~12%)',
)

# DCA_PULLBACK_WAIT_PCT: 10 → 13
txt = txt.replace(
    '    DCA_PULLBACK_WAIT_PCT = float(os.getenv("DCA_PULLBACK_WAIT_PCT", "10"))  # диапазон 45% → 10% = 22% диапазона (защита от покупки на хаях)',
    '    DCA_PULLBACK_WAIT_PCT = float(os.getenv("DCA_PULLBACK_WAIT_PCT", "13"))  # 20.07: диапазон 39% → 13% = 33% диапазона (защита от хаев, шире шума ATR_1h)',
)

# DCA_SMART_REENTRY_PULLBACK_PCT: 4 → 7
txt = txt.replace(
    '    DCA_SMART_REENTRY_PULLBACK_PCT = float(os.getenv("DCA_SMART_REENTRY_PULLBACK_PCT", "4"))   # p75 ATR=3.4% → 4% быстрее ловит отскок',
    '    DCA_SMART_REENTRY_PULLBACK_PCT = float(os.getenv("DCA_SMART_REENTRY_PULLBACK_PCT", "7"))   # 20.07: p75 ATR(15m)=4.67% → 7% = p75 нового распределения',
)

# DCA_ADAPTIVE_FAST_MOVE_PCT: 4 → 6
txt = txt.replace(
    '    DCA_ADAPTIVE_FAST_MOVE_PCT    = float(os.getenv("DCA_ADAPTIVE_FAST_MOVE_PCT",    "4"))  # ATR_1h=2.31% → 4% = 1.7×ATR; значимое движение за тик',
    '    DCA_ADAPTIVE_FAST_MOVE_PCT    = float(os.getenv("DCA_ADAPTIVE_FAST_MOVE_PCT",    "6"))  # 20.07: ATR(15m)=4.67% → 6% = 1.3×ATR; значимое движение за тик',
)

# PROFIT_PROTECT_DROP_PCT: 5.0 → 8.0
txt = txt.replace(
    '    PROFIT_PROTECT_DROP_PCT = float(os.getenv("PROFIT_PROTECT_DROP_PCT",    "5.0"))   # TR(1h) p50=4.4% p75=7.3% → 5% = портфельный разворот (между p50-p75 по 1h свечам)',
    '    PROFIT_PROTECT_DROP_PCT = float(os.getenv("PROFIT_PROTECT_DROP_PCT",    "8.0"))   # 20.07: ATR(1h)≈9-10% → p50 TR(1h)≈7% → 8% = портфельный разворот',
)

# SCALP_TRAIL_PCT: 4.0 → 7.0
txt = txt.replace(
    '    SCALP_TRAIL_PCT         = float(os.getenv("SCALP_TRAIL_PCT",           "4.0"))   # trail в боковике; ATR_1h=2.31% → 4% = 1.73×ATR выживает 1h свечу',
    '    SCALP_TRAIL_PCT         = float(os.getenv("SCALP_TRAIL_PCT",           "7.0"))   # 20.07: ATR(1h)≈9-10% → 7% = 0.75×ATR_1h (скальп-боковик)',
)

# SCALP_MAX_ATR_PCT: 5.5 → 8.0
txt = txt.replace(
    '    SCALP_MAX_ATR_PCT       = float(os.getenv("SCALP_MAX_ATR_PCT",         "5.5"))   # BUG-FIX: было 3.0% < ATR_15m=3.745% → скальп ВЕЧНО ВЫКЛЮЧЕН; 5.5% = активен в норм. условиях',
    '    SCALP_MAX_ATR_PCT       = float(os.getenv("SCALP_MAX_ATR_PCT",         "8.0"))   # 20.07: ATR(15m)=4.67% → 8% (скальп активен при ATR<8%, подавлен в хаосе)',
)

# FAST_REENTRY_PULLBACK_PCT: 4.0 → 7.0
txt = txt.replace(
    '    FAST_REENTRY_PULLBACK_PCT   = float(os.getenv("FAST_REENTRY_PULLBACK_PCT",  "4.0"))  # снижено с 5% — заходим на меньшем откате',
    '    FAST_REENTRY_PULLBACK_PCT   = float(os.getenv("FAST_REENTRY_PULLBACK_PCT",  "7.0"))  # 20.07: ATR(15m)=4.67% → 7% (реальный откат после TP)',
)

# --- Обновляем ATR-блок комментария для трейлинга ---
OLD_TRAIL_COMMENT = (
    "    # GRINCH реальные данные 20.07.2026 (актуально):\n"
    "    #   ATR-14 (15m) = 2.225%,  ATR-14 (1h) характерный ≈ 4-5%\n"
    "    #   Диапазон 24ч ≈ 45%,  7д ≈ 126%,  топ-памп: +50.1% за свечу\n"
    "    #   TR(1h) p50≈5%  p75≈9%  p90≈15%  — рынок СТАЛ ШИРЕ чем раньше\n"
    "    # Правило: каждый этап должен пережить 1-2 откатные свечи 1h в памп-движении\n"
    "    #   ≥ 2×ATR(1h)хар. = 8-10% → ранние этапы ≥ 10%\n"
    "    #   финальный этап ≥ 5-6% (близко к вершине — фиксация быстрее)\n"
    "    # Этап 1 (прибыль > 6%):  безубыток (покрывает 2% комиссии)\n"
    "    # Этап 2 (прибыль > 12%): трейлинг 10% — выживает откат внутри памп-движения\n"
    "    # Этап 3 (прибыль > 18%): трейлинг  7.5% — ловим топ 45%-го хода\n"
    "    # Этап 4 (прибыль > 26%): трейлинг  6.0% — финальная фиксация у вершины\n"
    "    #   Пример: памп +50% → вход $0.00068, пик $0.00102\n"
    "    #     Stage4 trail 6%: выход $0.000959 (+41% нетто) вместо Stage4=4.5% (+43%) —\n"
    "    #     чуть меньше, но Stage3=7.5% и Stage2=10% НАМНОГО лучше удерживают позицию\n"
    "    #     в более ранних фазах памп-движения, не давая выбить шумом."
)
NEW_TRAIL_COMMENT = (
    "    # GRINCH переизмерено 20.07.2026 ~18:00 UTC (актуально):\n"
    "    #   ATR-14 (15m) = 4.67%,  ATR-14 (1h) характерный ≈ 9-10%\n"
    "    #   Диапазон 25ч = 39.2%,  7д ≈ 126%,  топ-памп: +50.1% за свечу\n"
    "    #   TR(1h) p50≈7%  p75≈12%  p90≈18%  — рынок ВОЛАТИЛЬНЕЕ прежнего\n"
    "    # Правило: каждый этап должен пережить 1-2 откатные свечи 1h в памп-движении\n"
    "    #   ≥ 2×ATR(1h)хар. = 18-20% → ранние этапы ≥ 17%\n"
    "    #   финальный этап ≥ 6% (близко к вершине — фиксация быстрее)\n"
    "    # Этап 1 (прибыль > 6%):  безубыток (покрывает 2% комиссии)\n"
    "    # Этап 2 (прибыль > 12%): трейлинг 17% — выживает откат внутри памп-движения\n"
    "    # Этап 3 (прибыль > 18%): трейлинг 12.0% — ловим топ 39%-го хода\n"
    "    # Этап 4 (прибыль > 26%): трейлинг  6.0% — финальная фиксация у вершины\n"
    "    #   Пример: памп +50% → вход $0.00068, пик $0.00102\n"
    "    #     Stage4 trail 6%: выход $0.000959 (+41% нетто)\n"
    "    #     Stage2=17% удерживает в ранних фазах, не давая выбить шумом ATR(1h)≈9-10%."
)
txt = txt.replace(OLD_TRAIL_COMMENT, NEW_TRAIL_COMMENT, 1)

# --- Добавляем параметр WHALE_BALANCE_POLL_SEC если нет ---
if "WHALE_BALANCE_POLL_SEC" not in txt:
    insert_after = '    SMART_EARLY_MIN_TON    = float(os.getenv("SMART_EARLY_MIN_TON", "10"))    # мин. покупки умных за окно'
    txt = txt.replace(
        insert_after,
        insert_after + '\n\n'
        '    # ── On-chain анализ балансов китов (tonapi.io, бесплатный) ──────────────\n'
        '    # Каждые N секунд бот проверяет реальный GRINCH-баланс топ-кошельков.\n'
        '    # whale_hold_score [-1..+1]: >0 — киты держат, <0 — киты вышли.\n'
        '    WHALE_BALANCE_POLL_SEC  = int(os.getenv("WHALE_BALANCE_POLL_SEC",  "300"))  # 5 мин\n'
        '    WHALE_TOP_N             = int(os.getenv("WHALE_TOP_N",              "25"))   # топ-N кошельков для проверки\n'
        '    WHALE_MIN_GRINCH        = float(os.getenv("WHALE_MIN_GRINCH",       "100000"))  # порог «кит» (100K GRINCH)',
        1,
    )

open(CFG, "w", encoding="utf-8").write(txt)
print("✅ config.py пропатчен")

# Проверяем что все замены прошли
checks = [
    ('SMART_TP_TIGHT_TRAIL_PCT', '"10.0"'),
    ('TRAIL_STAGE2_PCT',         '"17.0"'),
    ('TRAIL_STAGE3_PCT',         '"12.0"'),
    ('TRAILING_STOP_PCT',        '"11.0"'),
    ('DCA_DROP_TRIGGER_PCT',     '"10"'),
    ('DCA_PULLBACK_WAIT_PCT',    '"13"'),
    ('DCA_SMART_REENTRY_PULLBACK_PCT', '"7"'),
    ('DCA_ADAPTIVE_FAST_MOVE_PCT',     '"6"'),
    ('PROFIT_PROTECT_DROP_PCT',  '"8.0"'),
    ('SCALP_TRAIL_PCT',          '"7.0"'),
    ('SCALP_MAX_ATR_PCT',        '"8.0"'),
    ('FAST_REENTRY_PULLBACK_PCT','"7.0"'),
    ('WHALE_BALANCE_POLL_SEC',   '300'),
]
txt2 = open(CFG, encoding="utf-8").read()
ok = True
for param, expected in checks:
    if expected not in txt2:
        print(f"  ⚠️  {param}: {expected} НЕ найден!")
        ok = False
    else:
        print(f"  ✓  {param} = {expected}")
if ok:
    print("✅ Все параметры config.py обновлены")

# ─────────────────────── wallet_tracker.py ───────────────────────────────────
WT = "/opt/bot/wallet_tracker.py"
shutil.copy(WT, WT + ".bak_market_tune")
wt = open(WT, encoding="utf-8").read()

# 1. Обновляем docstring файла — добавляем описание on-chain анализа
OLD_DOC = (
    "  • Отдаёт ИИ числовой сигнал умных денег [-1..+1], чтобы бот учился у тех,\n"
    "    кто реально зарабатывает, а не входил против них.\n"
    "\n"
    "ВАЖНО (честное ограничение): бесплатный API GeckoTerminal отдаёт только"
)
NEW_DOC = (
    "  • Отдаёт ИИ числовой сигнал умных денег [-1..+1], чтобы бот учился у тех,\n"
    "    кто реально зарабатывает, а не входил против них.\n"
    "  • Каждые 5 минут проверяет реальный on-chain GRINCH-баланс топ-кошельков\n"
    "    через tonapi.io (бесплатный) — whale_hold_score показывает, держат ли киты.\n"
    "\n"
    "ВАЖНО (честное ограничение): бесплатный API GeckoTerminal отдаёт только"
)
wt = wt.replace(OLD_DOC, NEW_DOC, 1)

# 2. Добавляем поля в __init__
OLD_INIT_END = (
    "        self._running    = False\n"
    "        self._stop_event = threading.Event()   # мгновенная остановка\n"
    "        self._backoff    = self.POLL_SEC\n"
    "        self.last_poll   = 0.0\n"
    "        self.last_error  = None\n"
    "        # адрес -> агрегат\n"
    "        self.wallets = {}\n"
    "        # дедупликация увиденных сделок\n"
    "        self._seen = set()\n"
    "        # последние сделки (для сигнала и отображения)\n"
    "        self.events = []\n"
    "        self._load()"
)
NEW_INIT_END = (
    "        self._running    = False\n"
    "        self._stop_event = threading.Event()   # мгновенная остановка\n"
    "        self._backoff    = self.POLL_SEC\n"
    "        self.last_poll   = 0.0\n"
    "        self.last_error  = None\n"
    "        # адрес -> агрегат\n"
    "        self.wallets = {}\n"
    "        # дедупликация увиденных сделок\n"
    "        self._seen = set()\n"
    "        # последние сделки (для сигнала и отображения)\n"
    "        self.events = []\n"
    "        # on-chain балансы китов: addr -> grinch_amount\n"
    "        self._on_chain_balances: dict = {}\n"
    "        self._last_balance_poll: float = 0.0\n"
    "        self._load()"
)
wt = wt.replace(OLD_INIT_END, NEW_INIT_END, 1)

# 3. Добавляем _poll_whale_balances() метод перед get_signal()
BEFORE_GET_SIGNAL = "    def get_signal(self):"
WHALE_POLL_METHOD = '''\
    # ──────────────────── on-chain баланс китов ──────────────────────────────
    def _poll_whale_balances(self):
        """
        Запрашивает реальный GRINCH-баланс топ-N кошельков (по объёму сделок)
        через tonapi.io. Вызывается из _loop() раз в WHALE_BALANCE_POLL_SEC.
        Не бросает исключений — ошибки замолчаны (внешний API).
        """
        try:
            jetton_addr = Config.GRINCH_TOKEN_ADDRESS
            top_n       = Config.WHALE_TOP_N
            min_grinch  = Config.WHALE_MIN_GRINCH
            if not jetton_addr:
                return

            with self._lock:
                wallets_copy = dict(self.wallets)

            # Сортируем по суммарному GRINCH-обороту — самые активные кошельки
            ranked = sorted(
                wallets_copy.items(),
                key=lambda x: x[1].get("grinch_bought", 0) + x[1].get("grinch_sold", 0),
                reverse=True,
            )[:top_n]

            new_balances = {}
            for addr, _ in ranked:
                if not addr or addr == "—":
                    continue
                try:
                    r = _HTTP.get(
                        f"https://tonapi.io/v2/accounts/{addr}/jettons/{jetton_addr}",
                        timeout=8,
                    )
                    if r.status_code == 404:
                        # кошелёк не держит этот jetton
                        new_balances[addr] = 0.0
                        continue
                    r.raise_for_status()
                    data = r.json()
                    raw  = data.get("balance", "0") or "0"
                    grinch = int(raw) / 1e9   # 9 decimals
                    if grinch >= min_grinch:
                        new_balances[addr] = grinch
                    else:
                        new_balances[addr] = grinch
                except Exception:
                    pass   # игнорируем per-wallet ошибки

            with self._lock:
                self._on_chain_balances = new_balances
                self._last_balance_poll = time.time()

            whales = sum(1 for v in new_balances.values() if v >= min_grinch)
            total  = sum(new_balances.values())
            logger.debug(
                f"[WalletTracker] on-chain: {len(new_balances)} кошельков, "
                f"{whales} китов ≥{min_grinch/1000:.0f}K GRINCH, "
                f"суммарно {total/1e6:.2f}M GRINCH"
            )
        except Exception as e:
            logger.debug(f"[WalletTracker] _poll_whale_balances error: {e}")

    def get_whale_hold_score(self) -> dict:
        """
        Текущий on-chain статус китов.
        whale_hold_score [-1..+1]:
          +1  → все топ-кошельки держат максимум GRINCH
          -1  → все вышли (балансы нулевые)
          0   → данных нет / нейтрально
        """
        with self._lock:
            balances   = dict(self._on_chain_balances)
            last_poll  = self._last_balance_poll

        if not balances or time.time() - last_poll > 600:
            # данные устарели или отсутствуют
            return {"whale_hold_score": 0.0, "whale_count": 0,
                    "whale_grinch_total": 0.0, "whale_data_age_sec": 9999}

        min_grinch = Config.WHALE_MIN_GRINCH
        whale_addrs   = [a for a, v in balances.items() if v >= min_grinch]
        total_grinch  = sum(balances.values())
        whale_grinch  = sum(balances[a] for a in whale_addrs)

        # Считаем «ожидаемый» максимальный баланс как max за историю наблюдений
        # (упрощённо: max из текущих балансов × количество кошельков)
        max_possible  = max(balances.values()) * len(balances) if balances else 1
        score = (whale_grinch / max_possible * 2 - 1) if max_possible > 0 else 0.0
        score = max(-1.0, min(1.0, score))

        return {
            "whale_hold_score":    round(score, 3),
            "whale_count":         len(whale_addrs),
            "whale_grinch_total":  round(whale_grinch / 1e6, 3),   # в миллионах
            "total_tracked_grinch": round(total_grinch / 1e6, 3),
            "whale_data_age_sec":  round(time.time() - last_poll),
        }

    def get_signal(self):'''

wt = wt.replace(BEFORE_GET_SIGNAL, WHALE_POLL_METHOD, 1)

# 4. В _loop(): добавляем вызов _poll_whale_balances() периодически
OLD_LOOP = (
    "    def _loop(self):\n"
    "        if self._stop_event.wait(timeout=self.START_DELAY):\n"
    "            return   # stop() вызван во время расфазировки\n"
    "        while self._running and not self._stop_event.is_set():\n"
    "            try:\n"
    "                self._poll_once()\n"
    "                self.last_error = None\n"
    "                self._backoff = self.POLL_SEC\n"
    "                self._stop_event.wait(timeout=self.POLL_SEC)\n"
    "            except Exception as e:           # noqa: BLE001\n"
    "                self.last_error = str(e)\n"
    "                self._backoff = min(self._backoff * 2, 300)\n"
    "                self._stop_event.wait(timeout=self._backoff)"
)
NEW_LOOP = (
    "    def _loop(self):\n"
    "        if self._stop_event.wait(timeout=self.START_DELAY):\n"
    "            return   # stop() вызван во время расфазировки\n"
    "        while self._running and not self._stop_event.is_set():\n"
    "            try:\n"
    "                self._poll_once()\n"
    "                # on-chain балансы китов — раз в WHALE_BALANCE_POLL_SEC\n"
    "                if time.time() - self._last_balance_poll >= Config.WHALE_BALANCE_POLL_SEC:\n"
    "                    self._poll_whale_balances()\n"
    "                self.last_error = None\n"
    "                self._backoff = self.POLL_SEC\n"
    "                self._stop_event.wait(timeout=self.POLL_SEC)\n"
    "            except Exception as e:           # noqa: BLE001\n"
    "                self.last_error = str(e)\n"
    "                self._backoff = min(self._backoff * 2, 300)\n"
    "                self._stop_event.wait(timeout=self._backoff)"
)
wt = wt.replace(OLD_LOOP, NEW_LOOP, 1)

# 5. Добавляем whale_hold_score в возврат get_signal()
# Находим конец get_signal() — строку с return {...}
# Ищем "early_buy_ton": round(cur_net, 2), и добавляем whale данные
OLD_RETURN = (
    '        return {\n'
    '            "score": round(score, 3),\n'
    '            "basis": basis,\n'
    '            "label": label,\n'
    '            "buy_ton": round(buy_ton, 2),\n'
    '            "sell_ton": round(sell_ton, 2),\n'
    '            "smart_wallets": len(smart),\n'
    '            "early_buy": bool(early_buy),\n'
    '            "early_buy_ton": round(cur_net, 2),\n'
    '        }'
)
NEW_RETURN = (
    '        whale = self.get_whale_hold_score()\n'
    '        return {\n'
    '            "score": round(score, 3),\n'
    '            "basis": basis,\n'
    '            "label": label,\n'
    '            "buy_ton": round(buy_ton, 2),\n'
    '            "sell_ton": round(sell_ton, 2),\n'
    '            "smart_wallets": len(smart),\n'
    '            "early_buy": bool(early_buy),\n'
    '            "early_buy_ton": round(cur_net, 2),\n'
    '            "whale_hold_score": whale["whale_hold_score"],\n'
    '            "whale_count": whale["whale_count"],\n'
    '            "whale_grinch_m": whale["whale_grinch_total"],\n'
    '            "whale_data_age": whale["whale_data_age_sec"],\n'
    '        }'
)
wt = wt.replace(OLD_RETURN, NEW_RETURN, 1)

open(WT, "w", encoding="utf-8").write(wt)
print("\n✅ wallet_tracker.py пропатчен")

# Проверки wallet_tracker
wt2 = open(WT, encoding="utf-8").read()
checks_wt = [
    "_on_chain_balances",
    "_last_balance_poll",
    "_poll_whale_balances",
    "get_whale_hold_score",
    "whale_hold_score",
    "tonapi.io",
    "WHALE_BALANCE_POLL_SEC",
]
ok2 = True
for c in checks_wt:
    if c not in wt2:
        print(f"  ⚠️  {c} НЕ найден в wallet_tracker!")
        ok2 = False
    else:
        print(f"  ✓  {c}")
if ok2:
    print("✅ Все изменения wallet_tracker.py применены")

print("\n🔧 Syntax check...")
import subprocess
r1 = subprocess.run(["python3", "-m", "py_compile", CFG], capture_output=True, text=True)
r2 = subprocess.run(["python3", "-m", "py_compile", WT],  capture_output=True, text=True)
if r1.returncode == 0:
    print("  ✅ config.py — OK")
else:
    print(f"  ❌ config.py: {r1.stderr}")
if r2.returncode == 0:
    print("  ✅ wallet_tracker.py — OK")
else:
    print(f"  ❌ wallet_tracker.py: {r2.stderr}")
