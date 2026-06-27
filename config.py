import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    EXCHANGE = os.getenv("EXCHANGE", "binance")
    API_KEY = os.getenv("API_KEY", "")
    API_SECRET = os.getenv("API_SECRET", "")
    SYMBOL = os.getenv("SYMBOL", "GRINCH/USDT")
    TIMEFRAME = os.getenv("TIMEFRAME", "1h")
    TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "1"))

    # ── 1 сделка за раз: весь капитал в одну позицию ──
    # Торговля по 1 TON на сделку — концентрируемся на одном лучшем входе
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
    TARGET_NET_PCT  = float(os.getenv("TARGET_NET_PCT",  "20.0"))  # минимальная нетто-прибыль
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "22.0"))  # gross = net + комиссии
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
    # GRINCH→TON (своп на DeDust требует ~0.65 TON газа, иначе отскок).
    GAS_RESERVE_TON = float(os.getenv("GAS_RESERVE_TON", "0.7"))

    # Реально потребляемый газ продажи GRINCH→TON (не резерв, а оценка
    # фактических потерь на сеть). В dedust_client прикладывается 0.35 TON,
    # из которых ~0.25 уходит на forward в пул; остаток может вернуться.
    # Используется для честного расчёта «если продать сейчас».
    SELL_GAS_TON = float(os.getenv("SELL_GAS_TON", "0.25"))

    # Реально потребляемый газ покупки TON→GRINCH (отдельно от ставки stake_ton).
    # В dedust_client прикладывается ~0.3 TON газа + 0.05 буфер = 0.35 TON.
    # Эта сумма ТРАТИТСЯ ПОВЕРХ stake_ton, поэтому учитывается в расчёте
    # «настоящей» стоимости позиции (честный безубыток и чистый результат).
    BUY_GAS_TON = float(os.getenv("BUY_GAS_TON", "0.25"))

    # Минимальная осмысленная ставка: после резерва на комиссию покупка
    # меньше этого порога не открывается (пыль не оправдывает газ свопа).
    MIN_STAKE_TON = float(os.getenv("MIN_STAKE_TON", "0.5"))

    # ── Прогрессивный трейлинг-стоп (защита прибыли на пути к +20%) ─────
    # Лестница масштабирована под цель +20% нетто, строго ниже TP-пола (22%):
    # Этап 1 (прибыль > 5%):  стоп в безубыток (покрывает комиссию — не теряем)
    # Этап 2 (прибыль > 10%): трейлинг 6% от максимума
    # Этап 3 (прибыль > 15%): трейлинг 4% от максимума
    # Этап 4 (прибыль > 20%): трейлинг 2% от максимума → фиксируем прибыль
    TRAIL_BREAKEVEN_AT  = float(os.getenv("TRAIL_BREAKEVEN_AT", "5.0"))    # % прибыли → стоп в безубыток
    TRAIL_STAGE2_AT     = float(os.getenv("TRAIL_STAGE2_AT",    "10.0"))   # % → трейлинг 6%
    TRAIL_STAGE2_PCT    = float(os.getenv("TRAIL_STAGE2_PCT",    "6.0"))
    TRAIL_STAGE3_AT     = float(os.getenv("TRAIL_STAGE3_AT",    "15.0"))   # % → трейлинг 4%
    TRAIL_STAGE3_PCT    = float(os.getenv("TRAIL_STAGE3_PCT",    "4.0"))
    TRAIL_STAGE4_AT     = float(os.getenv("TRAIL_STAGE4_AT",    "20.0"))   # % → трейлинг 2%
    TRAIL_STAGE4_PCT    = float(os.getenv("TRAIL_STAGE4_PCT",    "2.0"))
    TRAILING_STOP_PCT   = float(os.getenv("TRAILING_STOP_PCT",   "7.0"))   # начальный трейлинг (до безубытка)
    # ── Адаптивный трейлинг по силе тренда (даём прибыли разрастись) ────────
    # В сильном восходящем тренде стоп идёт ШИРЕ (winner runs), в боковике/
    # слабости — ТУЖЕ (быстрее фиксируем). Нижний пол прибыли НЕ затрагивается.
    TRAIL_TREND_WIDEN   = float(os.getenv("TRAIL_TREND_WIDEN",   "2.5"))    # множитель в сильном тренде
    TRAIL_CHOP_TIGHTEN  = float(os.getenv("TRAIL_CHOP_TIGHTEN",  "0.6"))    # множитель в боковике/слабости
    TRAIL_TREND_ADX     = float(os.getenv("TRAIL_TREND_ADX",    "30.0"))    # ADX ≥ → тренд «сильный»

    # ── ATR-цели: динамические ────────────────────────────────────────────
    USE_DYNAMIC_TARGETS = os.getenv("USE_DYNAMIC_TARGETS", "true").lower() == "true"
    # Стоп = 2.5×ATR — шире чем раньше, чтобы сделка дышала до 50%
    ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "2.5"))
    # Тейк = мин 50.6% — ATR×multiplier используется только если он ВЫШЕ 50.6%
    ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "8.0"))

    # ── Фильтры качества входа ──
    # Не покупать в нисходящем тренде
    TREND_FILTER = os.getenv("TREND_FILTER", "true").lower() == "true"
    # RSI 78 — для мем-монеты GRINCH RSI 68-75 это норма в памп; блокируем только экстремум
    RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "78"))
    # AI уверенность мин 62% — только высококонвикционные сигналы
    MIN_AI_CONFIDENCE = float(os.getenv("MIN_AI_CONFIDENCE", "62"))
    # AI-овверрайд только при 78%+ — очень сильный сигнал против тренда
    AI_OVERRIDE_CONFIDENCE = float(os.getenv("AI_OVERRIDE_CONFIDENCE", "78"))
    # AI жёсткий овверрайд: при ≥93% уверенности игнорируем RSI/аномалию (только DOWNTREND блокирует)
    AI_HARD_OVERRIDE_CONFIDENCE = float(os.getenv("AI_HARD_OVERRIDE_CONFIDENCE", "93"))
    # Mean Reversion Override: RSI < 25 + AI > 85% → входим даже в DOWNTREND (отскок от дна)
    RSI_OVERSOLD_REVERSAL = float(os.getenv("RSI_OVERSOLD_REVERSAL", "25"))
    REVERSAL_AI_MIN       = float(os.getenv("REVERSAL_AI_MIN", "85"))

    DEMO_MODE  = os.getenv("DEMO_MODE",  "false").lower() == "true"
    SECRET_KEY = os.getenv("SECRET_KEY", "grinch-gram-secret-2024")
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
