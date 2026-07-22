import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    EXCHANGE = os.getenv("EXCHANGE", "binance")
    API_KEY = os.getenv("API_KEY", "")
    API_SECRET = os.getenv("API_SECRET", "")
    SYMBOL = os.getenv("SYMBOL", "GRINCH/TON")
    TIMEFRAME = os.getenv("TIMEFRAME", "1h")
    # Начальная ставка 100 TON — полный боевой режим
    TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "100"))

    # ── AI-управляемый множитель размера позиции (money management) ──
    # Советник сам крутит его в диапазоне [0.3..1.5] по уверенности сигнала
    # и текущей просадке портфеля. Итоговая ставка = TRADE_AMOUNT × conf_factor
    # × kelly_mult × power_mult × AI_SIZE_MULT (см. trader.py._open_trade).
    AI_SIZE_MULT = float(os.getenv("AI_SIZE_MULT", "1.5"))  # максимально агрессивный размер позиции

    # ── 1 сделка за раз: весь капитал в одну лучшую позицию ──
    MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "1"))

    # ── Комиссия DEX (реальный пул GRINCH/TON — 1% за сторону) ──
    # Пул GRINCH/TON на DeDust имеет нестандартную комиссию 1% за каждый своп:
    # вход 1% + выход 1% = 2% за полный цикл. Считаем по реальной комиссии,
    # чтобы «нетто 20%» было честным после всех издержек.
    FEE_PCT = float(os.getenv("FEE_PCT", "1.0"))
    FEE_ROUND_TRIP = FEE_PCT * 2   # = 2.0%

    # ── Цели: +20% НЕТТО минимум (после всех комиссий) ──────────────────
    # Gross TP = 20% + 2% комиссии (1%+1%) = 22% от цены входа.
    # Никогда не фиксируем прибыль меньше +20% нетто.
    # GRINCH реал. (обновлено 21.07.2026, 100 свечей 15м + 100 свечей 1h):
    #   ATR(14,15m)=3.24%  ATR(14,1h)=4.87%  диапазон 53ч≈43.8%
    #   Топ-памп: +23.6% за одну 15м свечу, 6 баров >5% за 53ч
    #   Цель достижима за 3-5 свечей 1h, выше 2×ATR(1h) шума (≈9.7%)
    TARGET_NET_PCT  = float(os.getenv("TARGET_NET_PCT",  "13.0"))  # минимальная нетто-прибыль
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "22.0"))  # gross: диапазон 45%/24ч → цель 22% (≈50% дневного диапазона)
    STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "5.0"))   # запасной стоп (не используется при ONLY_PROFIT_EXIT)

    @classmethod
    def required_gross_pct(cls):
        """Минимальный gross-% движения цены, при котором нетто-прибыль после
        комиссий DEX ОБЕИХ сторон ≥ TARGET_NET_PCT (без учёта газа).

        Используется как нижняя граница, когда размер ставки неизвестен.
        Для честного расчёта с газом используй required_gross_pct_with_gas(stake_ton).
        """
        denom = 1.0 - cls.FEE_PCT / 100.0
        if denom <= 0:
            return cls.TARGET_NET_PCT + cls.FEE_ROUND_TRIP
        return (cls.TARGET_NET_PCT + 2.0 * cls.FEE_PCT) / denom

    @classmethod
    def required_gross_pct_with_gas(cls, stake_ton=None):
        """Минимальный gross-% движения цены с учётом газа ОБОИХ свопов.

        Чем меньше ставка — тем больший % нужен чтобы покрыть фиксированный
        газ. Для 1 TON сделки газ съедает ~50% ставки, для 10 TON — только 5%.

        Вывод формулы:
          total_cost  = stake + BUY_GAS_TON           (реальные затраты)
          proceeds    = stake * (1-fee)² * (1+g/100)  − SELL_GAS_TON
          net         = proceeds − total_cost

          Решая net ≥ TARGET_NET_PCT/100 * total_cost:
            (1+g/100) = [total_cost*(1+target) + SELL_GAS] / [stake*(1-fee)²]

        Если stake_ton не задан — фолбэк на required_gross_pct() (DEX-only).
        """
        if stake_ton is None or stake_ton <= 0:
            return cls.required_gross_pct()
        fee      = cls.FEE_PCT / 100.0
        buy_gas  = cls.BUY_GAS_TON
        sell_gas = cls.SELL_GAS_TON
        target   = cls.TARGET_NET_PCT / 100.0
        total_cost  = stake_ton + buy_gas
        numerator   = total_cost * (1.0 + target) + sell_gas
        denominator = stake_ton * (1.0 - fee) ** 2
        if denominator <= 0:
            return cls.required_gross_pct()
        gross = (numerator / denominator - 1.0) * 100.0
        # Не ниже DEX-only минимума, не выше разумного потолка (500%)
        return max(gross, cls.required_gross_pct())

    # ── Режим «только в плюс»: никогда не выходим в убыток ──────────────
    # Убыточный стоп отключён НАВСЕГДА: позиция закрывается ТОЛЬКО по тейк-
    # профиту (+22% gross / +20% нетто) или по трейлингу, который встаёт не
    # ниже безубытка (после покрытия комиссии) — то есть всегда в плюс.
    # Если позиция в минусе — бот ЖДЁТ, пока цена вырастет («бриллиантовые
    # руки»): реальный GRINCH в минус не продаётся. Отключить нельзя (по
    # требованию владельца), поэтому жёстко True, а не из настроек/env.
    ONLY_PROFIT_EXIT = True

    # ── Резерв TON на комиссию/газ — ВСЕГДА остаётся на кошельке ────────
    # Покупка никогда не тратит этот резерв: он нужен на газ будущей продажи
    # GRINCH→TON. Реальный газ продажи: 0.25 TON gas_nano + 0.18 TON fwd = 0.43 TON,
    # часть возвращается как excess. Резерв 0.45 TON — подтверждён on-chain.
    GAS_RESERVE_TON = float(os.getenv("GAS_RESERVE_TON", "0.45"))

    # Реально потребляемый газ продажи GRINCH→TON (не резерв, а оценка
    # фактических потерь на сеть). В dedust_client прикладывается 0.35 TON,
    # из которых ~0.25 уходит на forward в пул; остаток может вернуться.
    # Используется для честного расчёта «если продать сейчас».
    SELL_GAS_TON = float(os.getenv("SELL_GAS_TON", "0.253"))  # подтверждено on-chain (eventExtra всех продаж = -0.2526)

    # Реально потребляемый газ покупки TON→GRINCH (отдельно от ставки stake_ton).
    # В dedust_client прикладывается ~0.3 TON газа + 0.05 буфер = 0.35 TON.
    # Эта сумма ТРАТИТСЯ ПОВЕРХ stake_ton, поэтому учитывается в расчёте
    # «настоящей» стоимости позиции (честный безубыток и чистый результат).
    # On-chain данные: из 0.35 TON прикреплённых к покупке возвращается 0.2474 TON
    # (refund пула), значит реально сгорает только 0.1026 TON = ~0.10 TON.
    BUY_GAS_TON = float(os.getenv("BUY_GAS_TON", "0.103"))

    # Минимальная осмысленная ставка: после резерва на комиссию покупка
    # меньше этого порога не открывается (пыль не оправдывает газ свопа).
    # 0.1 TON — минимум, подтверждённый рабочим BUY on-chain.
    # Минимальная ставка пропорциональна базовой (100 TON × 5% = 5 TON)
    MIN_STAKE_TON = float(os.getenv("MIN_STAKE_TON", "5.0"))

    # ── Smart BUY: умная покупка с откатом ───────────────────────────────
    # Когда все условия для покупки выполнены, бот НЕ покупает сразу по рынку.
    # Он ставит цель-откат чуть ниже текущей цены и ждёт N тиков.
    # Если цена откатилась → покупаем дешевле (лучший вход, ниже безубыток).
    # Если откат не пришёл за SMART_BUY_MAX_WAIT_TICKS → берём по рынку.
    # При AI >= SMART_BUY_SKIP_CONF% — покупаем сразу (слишком сильный сигнал).
    SMART_BUY_ENABLED       = bool(int(os.getenv("SMART_BUY_ENABLED", "1")))
    SMART_BUY_PULLBACK_PCT  = float(os.getenv("SMART_BUY_PULLBACK_PCT", "0.2"))  # супер агрессия: почти сразу по рынку
    SMART_BUY_MAX_WAIT_TICKS = int(os.getenv("SMART_BUY_MAX_WAIT_TICKS", "2"))   # супер агрессия: макс 2 тика (~60 сек)
    SMART_BUY_SKIP_CONF     = float(os.getenv("SMART_BUY_SKIP_CONF", "88.0"))    # ≥88% → сразу

    # ── Smart TP: умная продажа с ИИ ─────────────────────────────────────
    # Когда позиция достигает минимального порога прибыли, бот проверяет сигнал
    # ИИ. Если уверенность >= SMART_TP_MIN_CONF% и сигнал BUY — держим позицию
    # с тугим трейлингом (SMART_TP_TIGHT_TRAIL_PCT%), давая цене расти дальше.
    # Как только ИИ слабеет — переключаемся на обычный трейлинг и фиксируем.
    SMART_TP_ENABLED        = bool(int(os.getenv("SMART_TP_ENABLED", "1")))
    SMART_TP_MIN_CONF       = float(os.getenv("SMART_TP_MIN_CONF", "70.0"))   # мин. уверенность ИИ для удержания
    # GRINCH (обновлено 21.07.2026): ATR(15m)=3.24%, ATR(1h)=4.87%
    # Тугой трейл Smart-TP не может быть < 2×ATR(1h) = 9.7% — иначе выбивается шумом.
    # Ставим 10%: минимально допустимое, держит в памп, фиксирует до разворота.
    SMART_TP_TIGHT_TRAIL_PCT = float(os.getenv("SMART_TP_TIGHT_TRAIL_PCT", "10.0"))  # 2×ATR(1h)=9.7% → 10% (мин. допустимый тугой трейл)

    # ── Прогрессивный трейлинг-стоп, откалиброван под GRINCH ────────────
    # GRINCH реальные данные 21.07.2026 (100 свечей 15м + 100 свечей 1h):
    #   ATR-14 (15m) = 3.24%,  ATR-14 (1h) = 4.87%
    #   Диапазон 53ч ≈ 43.8%,  топ-памп: +23.6% за 15м свечу; 6 баров >5%
    #   2×ATR(1h) = 9.7% = минимальный трейл против шума 1h свечей
    # Правило: каждый этап должен пережить 1-2 откатные свечи 1h в памп-движении
    #   ≥ 2×ATR(1h) = 9.7% → ранние этапы ≥ 10%
    #   финальный этап ≥ 5-6% (близко к вершине — фиксация быстрее)
    # Этап 1 (прибыль > 6%):  безубыток (покрывает 2% комиссии)
    # Этап 2 (прибыль > 12%): трейлинг 10% — выживает откат внутри памп-движения
    # Этап 3 (прибыль > 18%): трейлинг  7.5% — ловим топ 45%-го хода
    # Этап 4 (прибыль > 26%): трейлинг  6.0% — финальная фиксация у вершины
    #   Пример: памп +50% → вход $0.00068, пик $0.00102
    #     Stage4 trail 6%: выход $0.000959 (+41% нетто) вместо Stage4=4.5% (+43%) —
    #     чуть меньше, но Stage3=7.5% и Stage2=10% НАМНОГО лучше удерживают позицию
    #     в более ранних фазах памп-движения, не давая выбить шумом.
    TRAIL_BREAKEVEN_AT  = float(os.getenv("TRAIL_BREAKEVEN_AT",  "6.0"))   # безубыток после перекрытия 2% комиссии
    TRAIL_STAGE2_AT     = float(os.getenv("TRAIL_STAGE2_AT",    "12.0"))   # достижимо за 2-3 свечи 1h в памп
    TRAIL_STAGE2_PCT    = float(os.getenv("TRAIL_STAGE2_PCT",   "17.0"))   # ОБНОВЛЕНО: 2×ATR(1h)хар.=8-10% — выдерживает откат в памп
    TRAIL_STAGE3_AT     = float(os.getenv("TRAIL_STAGE3_AT",    "18.0"))   # диапазон 45% → этап 3 достижим в ~40% торговых дней
    TRAIL_STAGE3_PCT    = float(os.getenv("TRAIL_STAGE3_PCT",    "12.0"))   # ОБНОВЛЕНО: 1.5×ATR(1h)хар. — более широкий трейл для удержания
    TRAIL_STAGE4_AT     = float(os.getenv("TRAIL_STAGE4_AT",    "26.0"))   # диапазон 45% → ловим памп выше 26%
    TRAIL_STAGE4_PCT    = float(os.getenv("TRAIL_STAGE4_PCT",    "6.0"))   # ОБНОВЛЕНО: было 4.5% — выбивался шумом финальных свечей
    TRAILING_STOP_PCT   = float(os.getenv("TRAILING_STOP_PCT",   "13.0"))   # 4×ATR(15m)=13.0% (21.07: ATR=3.24%) — держит против шума 15m свечей
    # ── Адаптивный трейлинг по силе тренда (даём прибыли разрастись) ────────
    # В сильном восходящем тренде стоп идёт ШИРЕ (winner runs), в боковике/
    # слабости — ТУЖЕ (быстрее фиксируем). Нижний пол прибыли НЕ затрагивается.
    TRAIL_TREND_WIDEN   = float(os.getenv("TRAIL_TREND_WIDEN",   "1.5"))    # было 3.0 — трейл уже широкий
    TRAIL_CHOP_TIGHTEN  = float(os.getenv("TRAIL_CHOP_TIGHTEN",  "0.8"))    # было 0.55 — не тянуть ниже 0.8×TRAILING_STOP(9%)=7.2%
    TRAIL_TREND_ADX     = float(os.getenv("TRAIL_TREND_ADX",    "28.0"))    # ADX ≥ → тренд «сильный»

    # ── ATR-цели: динамические ────────────────────────────────────────────
    USE_DYNAMIC_TARGETS = os.getenv("USE_DYNAMIC_TARGETS", "true").lower() == "true"
    # GRINCH 21.07.2026: ATR(15m)=3.24% → 2.5×ATR=8.1%; TRAILING_STOP_PCT=13% > SL → floor активен
    # ATR(1h)=4.87% → 2.5×ATR_1h=12.2% — динамич. SL шире базового → базовый (13%) берётся как пол
    ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "2.5"))
    # TP цель = мин TAKE_PROFIT_PCT(22%) → ATR_TP_MULT=3.0 → динамич. TP = max(3×ATR_1h, 22%)
    # ATR(1h)=4.87% → 3×4.87=14.6% < 22% → базовый 22% в силе; при ATR>7.3% даёт выше базового
    ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "3.0"))

    # ── Фильтры качества входа ──
    # Не покупать в нисходящем тренде
    TREND_FILTER = os.getenv("TREND_FILTER", "true").lower() == "true"
    # RSI 78 — для мем-монеты GRINCH RSI 68-75 это норма в памп; блокируем только экстремум
    RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "78"))
    # AI уверенность мин — снижено для более активной торговли (было 60%)
    # Минимальная уверенность для BUY — AI торгует при 52%+
    MIN_AI_CONFIDENCE = float(os.getenv("MIN_AI_CONFIDENCE", "50"))  # макс. агрессия: входим раньше
    # AI-овверрайд только при 78%+ — очень сильный сигнал против тренда
    AI_OVERRIDE_CONFIDENCE = float(os.getenv("AI_OVERRIDE_CONFIDENCE", "78"))
    # AI жёсткий овверрайд: при ≥93% уверенности игнорируем RSI/аномалию (только DOWNTREND блокирует)
    AI_HARD_OVERRIDE_CONFIDENCE = float(os.getenv("AI_HARD_OVERRIDE_CONFIDENCE", "93"))
    # Mean Reversion Override: RSI < 25 + AI > 85% → входим даже в DOWNTREND (отскок от дна)
    RSI_OVERSOLD_REVERSAL = float(os.getenv("RSI_OVERSOLD_REVERSAL", "25"))
    REVERSAL_AI_MIN       = float(os.getenv("REVERSAL_AI_MIN", "70"))  # агрессия: mean-reversion входы чаще

    # ── Двусторонняя торговля (BUY + SHORT) ──────────────────────────
    # SHORT: когда AI ожидает падение — продаём GRINCH→TON, откупаем дешевле.
    # Прибыль = получаем обратно БОЛЬШЕ GRINCH чем продали (минимум +20% нетто).
    SHORT_TRADING_ENABLED = bool(int(os.getenv("SHORT_TRADING_ENABLED", "1")))
    # Трейлинг шорта: если цена выросла на X% от минимума → фиксируем
    # GRINCH 21.07.2026: ATR(15m)=3.24% → 2×ATR=6.5%; ATR(1h)=4.87% → 2×ATR=9.7%
    # Шорт-трейл должен пережить 1-2 свечи 1h → мин 10%; широкий = ловим полный откат.
    SHORT_TRAIL_PCT = float(os.getenv("SHORT_TRAIL_PCT", "10.0"))   # 2×ATR(1h)=9.7% (21.07) — минимум против шума
    # Резерв GRINCH — это количество GRINCH, которое бот НИКОГДА не включает в шорт.
    # Нужен, чтобы авто-ликвидатор всегда мог продать свои «зафиксированные» GRINCH.
    GRINCH_RESERVE = float(os.getenv("GRINCH_RESERVE", "500"))
    # Минимальная уверенность AI для открытия шорта (чуть выше BUY — шорт рискованнее)
    SHORT_MIN_AI_CONF = float(os.getenv("SHORT_MIN_AI_CONF", "58.0"))  # супер агрессия

    @classmethod
    def required_drop_pct_for_short(cls, grinch_value_ton=None):
        """Минимальный процент падения цены для прибыльного шорта.
        Симметрично required_gross_pct_with_gas() для лонгов — те же комиссии
        DEX (1%+1% пула) + газ обеих ног (продажи + обратной покупки).
        grinch_value_ton = количество GRINCH × текущий курс TON — аналог stake_ton для лонга.
        """
        return cls.required_gross_pct_with_gas(grinch_value_ton)

    # ── Полная автономия AI ───────────────────────────────────────────
    # Когда True, AI — единственный распорядитель сделок. Технический сигнал
    # (RSI/EMA/etc.) становится лишь входными данными для AI, не требуется
    # совпадение с AI-сигналом. AI сам считает выгодность с учётом всех комиссий.
    # Отключать нельзя — это и есть смысл системы.
    AI_AUTONOMOUS_MODE = True

    # Минимальная уверенность AI для самостоятельного входа в автономном режиме
    # Снижен порог входа: AI доверяем больше — 55% уже достаточно для сигнала
    AI_AUTONOMOUS_MIN_CONF = float(os.getenv("AI_AUTONOMOUS_MIN_CONF", "50.0"))  # было 44% — слишком агрессивно для тонкого рынка

    # ── ПОЛНЫЕ ПРАВА ТОРГОВЛИ ──────────────────────────────────────────
    # Когда True и уверенность AI >= AI_FULL_RIGHTS_MIN_CONF%, AI имеет полные
    # права на открытие позиции: ATR-фильтр не применяется. ONLY_PROFIT_EXIT
    # по-прежнему активен — выход только в плюс гарантирован.
    # Это даёт AI реальную автономию — торгует при высокой уверенности,
    # даже если рынок сейчас «спокойный» по ATR.
    AI_FULL_RIGHTS = bool(int(os.getenv("AI_FULL_RIGHTS", "1")))
    # При 62%+ AI получает полные права — без ATR-фильтра (был 68%)
    AI_FULL_RIGHTS_MIN_CONF = float(os.getenv("AI_FULL_RIGHTS_MIN_CONF", "52.0"))  # было 48% — повышаем для надёжности

    # Коэффициент «реалистичности» входа: минимальный ATR в % от цены, при котором
    # рынок способен дать нужный gross-% (если ATR × mult < required_gross → не входим).
    # Применяется ТОЛЬКО если AI_FULL_RIGHTS=False или уверенность AI ниже порога.
    AI_ATR_FEASIBILITY_MULT = float(os.getenv("AI_ATR_FEASIBILITY_MULT", "1.2"))

    # ── DCA (Усреднение позиции) стратегия ───────────────────────────
    # Когда включена — ИИ и технический анализ отключаются.
    # Бот торгует по чистым ценовым уровням: фиксированная ставка на вход,
    # докупка при падении, продажа всего при достижении цели по портфелю.
    # ── Минимальная прибыль на сделку (авто-ТП от ИИ) ───────────────────
    # ИИ после обучения сам выбирает оптимальный TAKE_PROFIT_PCT на основе
    # реальной истории, но НИКОГДА ниже этого ПРОЦЕНТА от ставки.
    # Значение трактуется как %: 5 = 5% от любой ставки.
    # Примеры: 100 TON × 5% = 5 TON минимум
    #          200 TON × 5% = 10 TON минимум
    #          500 TON × 5% = 25 TON минимум  (и так далее)
    MIN_PROFIT_TON      = float(os.getenv("MIN_PROFIT_TON", "5.0"))   # фактически %
    # Порог «обучен» — со скольки закрытых сделок ИИ начинает адаптировать TP
    AI_TP_ADAPT_MIN_TRADES = int(os.getenv("AI_TP_ADAPT_MIN_TRADES", "5"))
    # Потолок автоматического TP (не выше X% чтобы AI не поднял нереальную цель)
    AI_TP_CAP_PCT       = float(os.getenv("AI_TP_CAP_PCT", "80.0"))

    DCA_MODE            = bool(int(os.getenv("DCA_MODE", "1")))   # DCA включён по умолчанию
    # TON за каждый вход (первая покупка и каждая докупка)
    DCA_STAKE_TON       = float(os.getenv("DCA_STAKE_TON", "100"))
    # Продать ВСЁ когда общая стоимость GRINCH выросла на N% относительно суммарных затрат
    DCA_TARGET_PROFIT_PCT = float(os.getenv("DCA_TARGET_PROFIT_PCT", "22"))   # реал. диапазон 53ч≈43.8% → цель ~50% диапазона; TP мин=3×ATR(1h)=14.6%
    # Докупать ещё когда цена упала N% от цены ПОСЛЕДНЕЙ покупки
    DCA_DROP_TRIGGER_PCT  = float(os.getenv("DCA_DROP_TRIGGER_PCT", "10"))   # ATR(1h)=4.87%, p75≈9.7% → 10% = уверенное движение (≈ p75 по 1h свечам)
    # После продажи: ждать падения цены на N% от пика перед следующей покупкой
    DCA_PULLBACK_WAIT_PCT = float(os.getenv("DCA_PULLBACK_WAIT_PCT", "13"))  # диапазон 45% → 10% = 22% диапазона (защита от покупки на хаях)
    # Максимальное количество DCA-входов за один цикл (защита от бесконечного усреднения)
    DCA_MAX_ENTRIES     = int(os.getenv("DCA_MAX_ENTRIES", "10"))

    # ── DCA AI авто-адаптация: поднимает проценты только ВВЕРХ ───────────────
    # Срабатывает после N полных циклов (купил → продал всё → ждёт), анализируя
    # суммарную стоимость кошелька (TON + GRINCH в TON). Только рост, никаких снижений.
    DCA_AI_ADAPT_MIN_CYCLES = int(os.getenv("DCA_AI_ADAPT_MIN_CYCLES", "3"))
    DCA_AI_TARGET_CAP       = float(os.getenv("DCA_AI_TARGET_CAP",   "60"))  # макс. цель %
    DCA_AI_DROP_CAP         = float(os.getenv("DCA_AI_DROP_CAP",     "50"))  # макс. порог докупки %
    DCA_AI_PULLBACK_CAP     = float(os.getenv("DCA_AI_PULLBACK_CAP", "50"))  # макс. ожидание отката %

    # ── Каскадный выход: продаём частями, ловим памп ─────────────────────────
    # Уровень 1 (+20%): продаём 50% позиции, фиксируем гарантированную прибыль.
    # Уровень 2 (+40%): продаём оставшиеся 50%, ловим дополнительный памп.
    # При отключении — стандартная продажа всего на уровне 1.
    DCA_CASCADE_ENABLED    = bool(int(os.getenv("DCA_CASCADE_ENABLED",    "1")))
    DCA_CASCADE_LEVEL1_PCT = float(os.getenv("DCA_CASCADE_LEVEL1_PCT", "28"))  # выше DCA_TARGET (22%) — нет конкуренции уровней
    DCA_CASCADE_LEVEL2_PCT = float(os.getenv("DCA_CASCADE_LEVEL2_PCT", "52"))  # BUG-FIX: было 42% < реал. диапазон 45% → пропускал ракеты; 52% ловит движение выше диапазона

    # ── Временной фильтр: мёртвые UTC-часы (низкий объём, не открываем новые позиции) ──
    # По умолчанию: 0, 22, 23 UTC — самый низкий объём и диапазон по статистике GRINCH.
    # В мёртвые часы первый вход и ре-вход блокируются; докупка к существующим позициям
    # допускается только при расширенном триггере (x DEAD_HOURS_DROP_MULT).
    # Мёртвые часы по статистике объёмов за 7д (20.07.2026):
    # Самые низкие: 0,3,8,12,14 UTC. Было: 0,22,23 — устарело.
    DEAD_HOURS_UTC = [
        int(h) for h in os.getenv("DEAD_HOURS_UTC", "0,3,8,12,14").split(",")
        if h.strip().lstrip("-").isdigit()
    ]  # мёртвые часы UTC; 0,22,23 по анализу объёмов GRINCH
    # Множитель для DCA_DROP_TRIGGER в мёртвые часы (1.0 = не менять)
    DEAD_HOURS_DROP_MULT = float(os.getenv("DEAD_HOURS_DROP_MULT", "1.5"))

    # ── Умный реentri: после ТП входим быстрее если AI бычий ────────────────
    # Вместо ожидания -25% отката: при AI-уверенности ≥ порога достаточно -8%.
    DCA_SMART_REENTRY_ENABLED    = bool(int(os.getenv("DCA_SMART_REENTRY_ENABLED",    "1")))
    DCA_SMART_REENTRY_PULLBACK_PCT = float(os.getenv("DCA_SMART_REENTRY_PULLBACK_PCT", "7"))   # p75 ATR=3.4% → 4% быстрее ловит отскок
    DCA_SMART_REENTRY_MIN_AI_CONF  = float(os.getenv("DCA_SMART_REENTRY_MIN_AI_CONF",  "50")) # супер агрессия: больше реентри
    # Минимальная пауза между DCA-докупками (секунды) — защита от переторговли.
    # При низких порогах входа (drop 9%, conf 55%) без паузы бот может войти 3+ раз
    # за один тик волатильности. 300 сек = 5 минут — GRINCH свеча 15 мин, хватает.
    DCA_REENTRY_COOLDOWN_SEC = int(os.getenv("DCA_REENTRY_COOLDOWN_SEC", "30"))  # супер агрессия: чаще входы

    # ── Компаундирование: автоматический реинвест части прибыли ─────────────
    # После каждого прибыльного цикла ставка растёт на RATIO% от профита.
    # Пример: прибыль 20 TON → +30% = +6 TON к следующей ставке.
    # Накопленный бонус ограничен MAX_TON и сохраняется между циклами.
    DCA_COMPOUND_ENABLED   = bool(int(os.getenv("DCA_COMPOUND_ENABLED",   "1")))
    DCA_COMPOUND_RATIO     = float(os.getenv("DCA_COMPOUND_RATIO",     "0.45")) # макс. агрессия: реинвестируем больше прибыли
    DCA_COMPOUND_MAX_TON   = float(os.getenv("DCA_COMPOUND_MAX_TON",   "500"))  # макс. бонус (TON)

    # ── Адаптивный DCA-триггер: докупаем агрессивнее в ракетных движениях ───
    # Если цена выросла > FAST_MOVE_PCT за последние несколько тиков → рынок
    # летит вверх. В этом режиме порог докупки снижается до FAST_DROP_PCT
    # (вместо стандартных 12%) чтобы не пропустить откат во время ракеты.
    DCA_ADAPTIVE_TRIGGER_ENABLED  = bool(int(os.getenv("DCA_ADAPTIVE_TRIGGER_ENABLED",  "1")))
    DCA_ADAPTIVE_FAST_MOVE_PCT    = float(os.getenv("DCA_ADAPTIVE_FAST_MOVE_PCT",    "6"))  # ATR_1h=2.31% → 4% = 1.7×ATR; значимое движение за тик
    DCA_ADAPTIVE_FAST_DROP_PCT    = float(os.getenv("DCA_ADAPTIVE_FAST_DROP_PCT",    "4"))  # TR(1h) p50=4.4% → 4% = откат в норм. волатильности (оптимально)

    # ── Защита прибыли: если портфель +N TON И рынок падает → продаём всё ───
    # Продаёт весь GRINCH немедленно, если:
    #   1) текущая прибыль портфеля >= PROFIT_PROTECT_TON (в TON)
    #   2) цена откатилась от пика портфеля на >= PROFIT_PROTECT_DROP_PCT %
    #      ИЛИ AI-сигнал = SELL с уверенностью >= 55%
    # Защита «только в плюс»: выход по рынку, но никогда в убыток (ONLY_PROFIT_EXIT).
    PROFIT_PROTECT_ENABLED  = bool(int(os.getenv("PROFIT_PROTECT_ENABLED",  "1")))
    PROFIT_PROTECT_TON      = float(os.getenv("PROFIT_PROTECT_TON",         "3.0"))   # мин. 3 TON прибыли для активации
    PROFIT_PROTECT_DROP_PCT = float(os.getenv("PROFIT_PROTECT_DROP_PCT",    "9.0"))   # ATR(1h)=4.87%, p75≈9.7% → 9% = портфельный разворот (ниже p75; 21.07.2026)
    PROFIT_PROTECT_AI_SELL  = bool(int(os.getenv("PROFIT_PROTECT_AI_SELL",  "1")))    # также при AI SELL

    # ── Минимальная АБСОЛЮТНАЯ прибыль в TON — ниже этого не закрываем сделку ──
    # Советник управляет этим значением, жёсткий минимум = 2 TON.
    MIN_PROFIT_TON_ABS = float(os.getenv("MIN_PROFIT_TON_ABS", "2.0"))

    # ── Детектор крупных продаж: автоматическая контрарная закупка ──────────
    # Когда в пуле кто-то продаёт крупный объём GRINCH — бот немедленно
    # покупает на LARGE_SELL_DCA_TON. Покупка безусловная (обходит AI-фильтры).
    # Работает и в AI-режиме, и в DCA-режиме. Между двумя такими покупками
    # выдерживается пауза LARGE_SELL_COOLDOWN_SEC секунд.
    LARGE_SELL_DCA_ENABLED  = bool(int(os.getenv("LARGE_SELL_DCA_ENABLED",  "1")))
    LARGE_SELL_DCA_TON      = float(os.getenv("LARGE_SELL_DCA_TON",          "60.0"))   # = DCA_STAKE_TON; было 100
    LARGE_SELL_MIN_TON      = float(os.getenv("LARGE_SELL_MIN_TON",         "150.0"))   # супер агрессия: реагируем на меньшие продажи
    LARGE_SELL_COOLDOWN_SEC = int(os.getenv("LARGE_SELL_COOLDOWN_SEC",      "300"))     # пауза между сигналами

    # ── ALL-IN на дне: покупка на весь доступный баланс при экстремальной ────
    # перепроданности (RSI≤ALLIN_RSI_MAX + score≥ALLIN_BOTTOM_CONF из 100).
    # По умолчанию выключено — включить через дашборд или переменную окружения.
    # Кулдаун между срабатываниями: 4 часа (хардкод в bottom_detector.py).
    ALLIN_ON_BOTTOM    = bool(int(os.getenv("ALLIN_ON_BOTTOM",    "0")))    # 0/1 — вкл/выкл
    ALLIN_BOTTOM_CONF  = float(os.getenv("ALLIN_BOTTOM_CONF",    "65"))    # мин. score для all-in
    ALLIN_RSI_MAX      = float(os.getenv("ALLIN_RSI_MAX",        "32"))    # RSI не выше этого
    ALLIN_MIN_FREE_TON = float(os.getenv("ALLIN_MIN_FREE_TON",   "50"))    # мин. TON чтобы смысл был

    # ── Кулдаун после убыточного закрытия ────────────────────────────────────
    # После SL-выхода бот выжидает N секунд прежде чем входить снова.
    # Защищает от повторного входа в нисходящий тренд сразу после выбивания стопа.
    LOSS_COOLDOWN_SEC = int(os.getenv("LOSS_COOLDOWN_SEC", "120"))   # 2 минуты пауза после убытка (агрессия)

    # ── Дневной автовыключатель (Circuit Breaker) ─────────────────────────────
    # Если суммарный убыток за текущие сутки UTC превышает порог — торговля
    # автоматически приостанавливается до следующего дня 00:00 UTC.
    # Защищает капитал от «чёрного дня»: аномальный рынок, ошибка стратегии,
    # зависший внешний API — нет смысла продолжать серийные убытки.
    CIRCUIT_BREAKER_ENABLED        = bool(int(os.getenv("CIRCUIT_BREAKER_ENABLED",        "1")))
    CIRCUIT_BREAKER_DAILY_LOSS_PCT = float(os.getenv("CIRCUIT_BREAKER_DAILY_LOSS_PCT",  "15.0"))  # % от портфеля на начало дня

    # ── Репер устаревших позиций (Stale Position Reaper) ─────────────────────
    # Позиция, открытая дольше MAX_HOURS без достижения TP/SL и без прибыли,
    # считается «мёртвой» — выходим по рынку, высвобождая капитал.
    # По умолчанию выключен (opt-in): включать осторожно, может срезать длинные
    # удержания при DCA-стратегии.
    STALE_POSITION_ENABLED        = bool(int(os.getenv("STALE_POSITION_ENABLED",         "0")))
    STALE_POSITION_MAX_HOURS      = float(os.getenv("STALE_POSITION_MAX_HOURS",         "72.0"))  # 3 дня
    STALE_POSITION_MIN_PROFIT_PCT = float(os.getenv("STALE_POSITION_MIN_PROFIT_PCT",     "1.0"))  # если прибыль > N% — не трогаем

    # ── DCA AI-guard: не докупать в "падающий нож" ───────────────────────────
    # Если AI уверен в продолжении падения (≥ порога) — блокируем DCA-докупку.
    # Обычная DCA логика включается вновь как только AI сигнал меняется.
    DCA_AI_SELL_BLOCK_CONF = float(os.getenv("DCA_AI_SELL_BLOCK_CONF", "85.0"))  # SELL ≥ N% → блок докупки (агрессия)

    # ── Confluence фильтр входа: RSI + объём ─────────────────────────────────
    # BUY только если RSI не перегрет И объём подтверждает движение.
    # Отключается при hard_override (AI ≥ 85%) и ai_full_rights_active.
    CONFLUENCE_ENABLED      = bool(int(os.getenv("CONFLUENCE_ENABLED",      "1")))
    CONFLUENCE_RSI_MAX      = float(os.getenv("CONFLUENCE_RSI_MAX",      "78.0"))  # RSI < 78 для входа
    CONFLUENCE_VOL_MIN_RATIO = float(os.getenv("CONFLUENCE_VOL_MIN_RATIO", "0.6")) # объём ≥ 0.6×MA20

    # ── EV-порог (вынесен из hardcode для тюнинга) ───────────────────────────
    # EV > EV_THRESHOLD → ожидаемая прибыль положительна → BUY не блокируется.
    # Уменьшить до -0.05 для агрессивного режима, увеличить до 0.02 для консервативного.
    EV_THRESHOLD = float(os.getenv("EV_THRESHOLD", "-1.0"))

    DEMO_MODE  = os.getenv("DEMO_MODE",  "false").lower() == "true"
    SECRET_KEY = os.getenv("SECRET_KEY", "grinch-gram-secret-2024")
    # ⚠️ Предупреждение: если SECRET_KEY не задан в env — Flask-сессии подделываемы.
    # На боевом деплое ОБЯЗАТЕЛЬНО выставить SECRET_KEY в секретах Bothost/Replit.
    if SECRET_KEY == "grinch-gram-secret-2024":
        import logging as _log
        _log.getLogger("config").warning(
            "⚠️  SECRET_KEY использует дефолтное значение. "
            "Задайте переменную SECRET_KEY в секретах для защиты сессий!"
        )
    # EQ-адрес выводится из TON_MNEMONIC (WalletV5R1 / W5 — кошелёк TonKeeper)
    TON_WALLET = os.getenv("TON_WALLET", "EQDDgb2BTM-KCjntOoUg6uHllvnu3KGqEquKw6IySVP3hGXJ")
    # Адрес контракта токена GRINCH (TON-джеттон)
    GRINCH_TOKEN_ADDRESS = os.getenv("GRINCH_TOKEN_ADDRESS", "EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL")
    # Реальный ликвидный пул GRINCH/TON на DeDust (нестандартная комиссия 1%).
    # Factory.get_pool возвращает канонический адрес дефолтной комиссии, который
    # on-chain НЕ существует — поэтому свопы нужно слать прямо в этот пул.
    GRINCH_POOL_ADDRESS = os.getenv("GRINCH_POOL_ADDRESS", "EQDpVwTQr53cwgaT_VCFsmrleg5fBvStTjMrvyvprF_ROC9Z")
    # Мнемоника TON-кошелька (24 слова через пробел) — хранить только в секретах!
    TON_MNEMONIC = os.getenv("TON_MNEMONIC", "")
    # API-ключ TonCenter (опционально) — снимает rate-limit 429 на бесплатном плане.
    # Получить: https://toncenter.com/  (бесплатный tier даёт 10 rps вместо 1 rps)
    TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")

    # ── Сигнал «умных денег» (мониторинг кошельков пула) ──────────────────
    # Бот наблюдает за всеми кошельками в пуле GRINCH и учится у прибыльных.
    # При сильной распродаже умных денег — блокируем вход; при накоплении —
    # чуть смягчаем требуемый порог уверенности AI. Безопасность (не продавать
    # в убыток) сигнал НЕ трогает — влияет только на ВХОД (покупку).
    SMART_MONEY_BLOCK   = float(os.getenv("SMART_MONEY_BLOCK",   "-0.6"))  # ≤ → блок входа
    SMART_MONEY_BOOST_AT = float(os.getenv("SMART_MONEY_BOOST_AT", "0.5")) # ≥ → смягчить порог
    SMART_MONEY_CONF_BONUS = float(os.getenv("SMART_MONEY_CONF_BONUS", "5"))  # на сколько % смягчить
    SMART_MONEY_MIN_FLOOR  = float(os.getenv("SMART_MONEY_MIN_FLOOR", "45")) # ниже не опускаем
    # Ранний вход: если прибыльные кошельки ТОЛЬКО НАЧАЛИ покупать (свежая
    # волна накопления), бот входит быстрее — без ожидания 2-го подтверждения.
    SMART_EARLY_WINDOW_SEC = int(os.getenv("SMART_EARLY_WINDOW_SEC", "600"))  # окно «прямо сейчас» (10 мин)
    SMART_EARLY_MIN_TON    = float(os.getenv("SMART_EARLY_MIN_TON", "10"))    # мин. покупки умных за окно

    # -- On-chain whale balance analysis (tonapi.io free) --
    WHALE_BALANCE_POLL_SEC  = int(os.getenv("WHALE_BALANCE_POLL_SEC",  "300"))
    WHALE_TOP_N             = int(os.getenv("WHALE_TOP_N",              "25"))
    WHALE_MIN_GRINCH        = float(os.getenv("WHALE_MIN_GRINCH",  "100000"))
    # ── Скальпинг-режим: быстрые сделки 5-8% в RANGING/SQUEEZE рынке ──────
    # Когда AI обнаруживает боковик и ATR < порога — используем меньшие цели.
    # ТОЛЬКО В ПЛЮС: скальп TP ≥ 2×DEX_fees + газ (~5% gross → ~3% нетто).
    # Режимы где скальп работает: RANGING, SQUEEZE, TRANSITION
    SCALPING_ENABLED        = bool(int(os.getenv("SCALPING_ENABLED",       "1")))
    SCALP_TARGET_NET_PCT    = float(os.getenv("SCALP_TARGET_NET_PCT",      "3.0"))   # снижено с 4% — быстрее фиксируем прибыль
    SCALP_TP_PCT            = float(os.getenv("SCALP_TP_PCT",              "5.0"))   # gross (3% нетто + 2% DEX)
    SCALP_TRAIL_PCT         = float(os.getenv("SCALP_TRAIL_PCT",           "7.0"))   # trail в боковике; ATR_1h=2.31% → 4% = 1.73×ATR выживает 1h свечу
    SCALP_MIN_AI_CONF       = float(os.getenv("SCALP_MIN_AI_CONF",         "52.0"))  # снижено с 55% — больше скальп-входов
    SCALP_MAX_ATR_PCT       = float(os.getenv("SCALP_MAX_ATR_PCT",         "8.0"))   # BUG-FIX: было 3.0% < ATR_15m=3.745% → скальп ВЕЧНО ВЫКЛЮЧЕН; 5.5% = активен в норм. условиях

    # ── BrainFusion: единый мозг (AI + TA + советник) ───────────────────
    # Когда все три источника согласны с ≥78% → входим без ожидания тика
    FUSION_ENABLED              = bool(int(os.getenv("FUSION_ENABLED",          "1")))
    FUSION_SKIP_CONFIRM_CONF    = float(os.getenv("FUSION_SKIP_CONFIRM_CONF",   "68.0"))
    # Мультипликатор позиции при памп-сигнале от fusion (ограничен 2×)
    FUSION_PUMP_BOOST_MAX       = float(os.getenv("FUSION_PUMP_BOOST_MAX",      "1.8"))

    # ── Быстрый ре-вход: после прибыльного закрытия ──────────────────────
    # Fusion бычий + прибыль была → не ждём полного отката DCA_PULLBACK_WAIT
    # Используется только если AI BUY ≥ FAST_REENTRY_MIN_CONF
    FAST_REENTRY_ENABLED        = bool(int(os.getenv("FAST_REENTRY_ENABLED",    "1")))
    FAST_REENTRY_PULLBACK_PCT   = float(os.getenv("FAST_REENTRY_PULLBACK_PCT",  "7.0"))  # снижено с 5% — заходим на меньшем откате
    FAST_REENTRY_MIN_CONF       = float(os.getenv("FAST_REENTRY_MIN_CONF",      "55.0")) # снижено с 60% — быстрее реентри

    # ── Онлайн-инъекция ордер-флоу в AI: DEX buy/sell ratio ─────────────
    # Реальный поток заявок из DexScreener/GeckoTerminal обогащает AI-фичи
    # (без этого AI видит только OHLCV свечи, а не живой поток сделок)
    ORDER_FLOW_INJECT_ENABLED   = bool(int(os.getenv("ORDER_FLOW_INJECT_ENABLED", "1")))

    # Режим торговли: "demo" | "dedust"
    TRADE_MODE = os.getenv("TRADE_MODE", "dedust")
    # Защита от проскальзывания: максимально допустимое отклонение цены свопа
    # от справедливой (внешний прайс-фид) в процентах. Покрывает комиссию пула
    # (~1.35%) + проскальзывание/импакт/устаревание цены. Если фактический выход
    # окажется ниже этого порога, своп откатится в блокчейне (а не исполнится по
    # убыточному курсу). Если цену вычислить нельзя — сделка отклоняется.
    # Жёстко ограничиваем диапазон 0.1..50%, чтобы ошибочная конфигурация
    # (0, отрицательное или ≥100) не отключила защиту и не создала некорректный min-out.
    SLIPPAGE_PCT = min(50.0, max(0.1, float(os.getenv("SLIPPAGE_PCT", "5"))))


# ── Применяем сохранённые в дашборде настройки (settings.json) ─────────────────
# Эти значения переопределяют дефолты/env и сохраняются между перезапусками.
try:
    from settings_store import get_section as _get_section

    _persisted = _get_section("config")
    for _key, _val in _persisted.items():
        if not hasattr(Config, _key):
            continue
        # Приводим к типу дефолта, чтобы повреждённый settings.json не сломал логику
        _default = getattr(Config, _key)
        try:
            if isinstance(_default, bool):
                # Строки 'False'/'0'/'no' из DB/JSON → False (стандартный bool() не работает!)
                if isinstance(_val, str):
                    _val = _val.strip().lower() not in ("false", "0", "no", "none", "")
                else:
                    _val = bool(_val)
            elif isinstance(_default, int):
                _val = int(_val)
            elif isinstance(_default, float):
                _val = float(_val)
            elif isinstance(_default, str):
                _val = str(_val)
            setattr(Config, _key, _val)
        except (TypeError, ValueError):
            continue
    # Производное значение пересчитываем после применения переопределений
    Config.FEE_ROUND_TRIP = Config.FEE_PCT * 2
except Exception as _e:  # noqa: BLE001 — настройки не должны ломать запуск
    print(f"[Config] Не удалось применить сохранённые настройки: {_e}")

# Гарантия владельца «не продавать в минус» НЕ конфигурируется и не может быть
# отключена через settings.json/env — жёстко возвращаем True после загрузки.
Config.ONLY_PROFIT_EXIT = True
