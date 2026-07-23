"""
ai_advisor.py — Мета-ИИ советник с полной автономией.
Поддерживает топовые AI-модели мира: GPT-4o, DeepSeek-R1, xAI Grok, Anthropic Claude, Groq.
Авто-выбор лучшего доступного провайдера. Chain-of-Thought: AI думает пошагово перед решением.
"""
import os, json, logging, threading, time, re
from datetime import datetime
from collections import deque
from typing import Optional, Tuple
try:
    import brain_fusion as _bf
except Exception as _bfe:
    import logging as _bflog2
    _bflog2.getLogger("ai_advisor").warning(f"brain_fusion не загружен: {_bfe}")
    class _BFStub2:
        def update_advisor(self, *a, **kw): pass
        def get_state(self): return {}
    _bf = _BFStub2()

logger = logging.getLogger(__name__)

# ── Groq (бесплатно, OpenAI-совместимый API) ──────────────────────────────
GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")

# ── Файл хранения ключа (DATA_DIR/groq_key.txt) ───────────────────────────
_DATA_DIR    = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
_GROQ_KEY_FILE = os.path.join(_DATA_DIR, "groq_key.txt")


def _read_key_file() -> str:
    """Читает ключ из файла. Возвращает пустую строку если файл отсутствует."""
    try:
        with open(_GROQ_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return ""


def _write_key_file(key: str):
    """Сохраняет ключ в файл (создаёт DATA_DIR при необходимости)."""
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _GROQ_KEY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(key.strip())
        os.replace(tmp, _GROQ_KEY_FILE)
    except OSError as e:
        logger.error(f"[Advisor] ❌ Не удалось сохранить ключ в файл: {e}")


# Загружаем ключ: env → файл
if not GROQ_API_KEY:
    GROQ_API_KEY = _read_key_file()
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# llama-3.1-8b-instant имеет TPM-лимит всего 6000 токенов/мин на бесплатном
# тарифе — снапшот рынка (~11000 токенов) в него не помещается (413).
# 70b-versatile: TPM ~12000 (снапшот помещается), TPD 100000/день —
# при разумном интервале и урезанном снапшоте этого достаточно на сутки.
GROQ_MODEL    = "llama-3.3-70b-versatile"

# ══════════════════════════════════════════════════════════════════════════
# 🌍 МУЛЬТИ-ПРОВАЙДЕР: топовые AI-модели мира
# Приоритет: OpenAI GPT-4o > DeepSeek-R1 > xAI Grok > Anthropic > Groq
# ══════════════════════════════════════════════════════════════════════════
PROVIDER_CONFIGS = {
    "openai": {
        "name":     "OpenAI GPT-4o",
        "base_url": "https://api.openai.com/v1",
        "model":    "gpt-4o",
        "max_tokens": 2000,
        "temperature": 0.2,
        "env_key":  "OPENAI_API_KEY",
        "key_file": "openai_key.txt",
        "supports_reasoning": False,
        "priority": 1,
    },
    "deepseek": {
        "name":     "DeepSeek-R1 (Reasoning)",
        "base_url": "https://api.deepseek.com/v1",
        "model":    "deepseek-reasoner",
        "max_tokens": 4000,
        "temperature": 0.0,   # reasoning модели лучше работают с temp=0
        "env_key":  "DEEPSEEK_API_KEY",
        "key_file": "deepseek_key.txt",
        "supports_reasoning": True,  # встроенный Chain-of-Thought
        "priority": 2,
    },
    "xai": {
        "name":     "xAI Grok-3",
        "base_url": "https://api.x.ai/v1",
        "model":    "grok-3-latest",
        "max_tokens": 2000,
        "temperature": 0.2,
        "env_key":  "XAI_API_KEY",
        "key_file": "xai_key.txt",
        "supports_reasoning": False,
        "priority": 3,
    },
    "anthropic": {
        "name":     "Anthropic Claude 3.5 Sonnet",
        "base_url": "https://api.anthropic.com/v1",
        "model":    "claude-3-5-sonnet-20241022",
        "max_tokens": 2000,
        "temperature": 0.2,
        "env_key":  "ANTHROPIC_API_KEY",
        "key_file": "anthropic_key.txt",
        "supports_reasoning": False,
        "priority": 4,
    },
    "groq": {
        "name":     "Groq LLaMA 3.3-70B (Free)",
        "base_url": GROQ_BASE_URL,
        "model":    GROQ_MODEL,
        "max_tokens": 1200,
        "temperature": 0.25,
        "env_key":  "GROQ_API_KEY",
        "key_file": "groq_key.txt",
        "supports_reasoning": False,
        "priority": 5,
    },
}

# Хранилище ключей в памяти (provider_id → key string)
_provider_keys: dict = {}

# Выбранный пользователем провайдер (None = авто)
_selected_provider: Optional[str] = None


def _provider_key_file(provider_id: str) -> str:
    fname = PROVIDER_CONFIGS.get(provider_id, {}).get("key_file", f"{provider_id}_key.txt")
    return os.path.join(_DATA_DIR, fname)


def _read_provider_key(provider_id: str) -> str:
    """Читает ключ провайдера: memory → env → file."""
    if _provider_keys.get(provider_id):
        return _provider_keys[provider_id]
    cfg = PROVIDER_CONFIGS.get(provider_id, {})
    key = os.getenv(cfg.get("env_key", ""), "")
    if not key:
        try:
            with open(_provider_key_file(provider_id), "r", encoding="utf-8") as f:
                key = f.read().strip()
        except (FileNotFoundError, OSError):
            pass
    if key:
        _provider_keys[provider_id] = key
    return key


def _write_provider_key(provider_id: str, key: str):
    """Сохраняет ключ провайдера в файл."""
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        fpath = _provider_key_file(provider_id)
        tmp = fpath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(key.strip())
        os.replace(tmp, fpath)
        _provider_keys[provider_id] = key.strip()
    except OSError as e:
        logger.error(f"[Advisor] ❌ Не удалось сохранить ключ {provider_id}: {e}")


def _get_best_provider() -> Tuple[str, dict]:
    """Возвращает (provider_id, config) лучшего доступного провайдера.
    Если пользователь выбрал конкретный — используем его (если ключ есть).
    Иначе — авто: по приоритету от лучшего к фолбэку."""
    global _selected_provider

    # Восстанавливаем ключ Groq в новую систему для обратной совместимости
    if GROQ_API_KEY and not _provider_keys.get("groq"):
        _provider_keys["groq"] = GROQ_API_KEY

    candidates = sorted(PROVIDER_CONFIGS.items(), key=lambda x: x[1]["priority"])

    if _selected_provider and _selected_provider in PROVIDER_CONFIGS:
        key = _read_provider_key(_selected_provider)
        if key:
            return _selected_provider, PROVIDER_CONFIGS[_selected_provider]
        logger.warning(f"[Advisor] Выбранный провайдер {_selected_provider} не имеет ключа, авто-фолбэк")

    for pid, cfg in candidates:
        if _read_provider_key(pid):
            return pid, cfg

    return "groq", PROVIDER_CONFIGS["groq"]  # последний шанс


def _load_provider_keys():
    """Загружает все доступные ключи провайдеров при старте."""
    for pid in PROVIDER_CONFIGS:
        _read_provider_key(pid)
    try:
        from settings_store import get_section
        adv = get_section("advisor") or {}
        if adv.get("selected_provider"):
            global _selected_provider
            _selected_provider = adv["selected_provider"]
    except Exception:
        pass

# ── Параметры автономии ────────────────────────────────────────────────────
AUTO_INTERVAL_MIN    = 120  # авто-запуск каждые N минут (было 60 — слишком часто жгло TPD)
AUTO_TRADES_TRIGGER  = 2    # авто-запуск после закрытых сделок (было 1 — слишком часто жгло токены)

# Восстанавливаем сохранённые настройки интервала (переживают перезапуск)
try:
    if _adv_sec.get("interval_min"):
        AUTO_INTERVAL_MIN = max(30, min(360, int(_adv_sec["interval_min"])))
    if _adv_sec.get("trades_trigger"):
        AUTO_TRADES_TRIGGER = max(1, min(20, int(_adv_sec["trades_trigger"])))
except Exception:
    pass

# ── Параметры которые советник МОЖЕТ менять → (мин, макс) ─────────────────
TUNABLE = {
    # Торговые параметры (Config)
    "take_profit_pct":         (5.0,   200.0),
    "dca_target_profit_pct":   (5.0,   100.0),
    "dca_drop_trigger_pct":    (1.0,    60.0),
    "smart_buy_pullback_pct":  (0.2,     5.0),
    "profit_protect_drop_pct": (0.3,    20.0),
    "min_ai_confidence":       (40.0,   90.0),
    # AI Engine параметры (модуль ai_engine)
    "buy_threshold":           (0.40,   0.75),
    "sell_threshold":          (0.52,   0.85),
    "profit_bias_pct":         (0.010,  0.060),
    "vr_trend_thresh":         (1.05,   1.40),
    "ev_min_trades":           (5.0,    25.0),
    "retrain_every":           (1.0,     6.0),
    # Трейлинг-стопы (Config) — расширенная автономия
    "trailing_stop_pct":       (2.0,    25.0),
    "trail_stage2_pct":        (2.0,    20.0),
    "trail_stage3_pct":        (1.5,    15.0),
    "trail_stage4_pct":        (1.0,    10.0),
    "smart_tp_min_conf":       (50.0,   90.0),
    "short_trail_pct":         (3.0,    25.0),
    # Money management — размер позиции
    "ai_size_mult":            (0.3,     1.5),
    # Размер ставки — ИИ управляет напрямую
    "dca_stake_ton":           (5.0,  1000.0),   # TON за каждый DCA-вход
    "trade_amount":            (5.0,  1000.0),   # TON базовой ставки AI-режима
    "min_profit_ton_abs":      (2.0,    50.0),   # минимальная АБСОЛЮТНАЯ прибыль в TON
    # ── Трейлинг: уровни активации стадий ───────────────────────
    "trail_breakeven_at":      (3.0,    25.0),   # прибыль % → переход в безубыток
    "trail_stage2_at":         (8.0,    35.0),   # прибыль % → стадия 2
    "trail_stage3_at":         (15.0,   50.0),   # прибыль % → стадия 3
    "trail_stage4_at":         (25.0,   80.0),   # прибыль % → стадия 4
    "smart_tp_tight_trail_pct":(2.0,    20.0),   # тугой трейл в Smart TP режиме
    # ── DCA расширенные параметры ────────────────────────────────
    "dca_pullback_wait_pct":   (2.0,    50.0),   # % падения от пика для нового DCA-цикла
    "dca_max_entries":         (2.0,    20.0),   # макс. DCA-входов за цикл
    # ── DCA улучшения: Каскадный выход ───────────────────────────
    "dca_cascade_level1_pct":  (5.0,   100.0),  # % прибыли → продать 50% (Ур.1)
    "dca_cascade_level2_pct":  (10.0,  200.0),  # % прибыли → продать остаток (Ур.2)
    # ── DCA улучшения: Умный реentri ─────────────────────────────
    "dca_smart_reentry_pullback_pct": (1.0, 30.0),   # откат для быстрого реentri
    "dca_smart_reentry_min_ai_conf":  (30.0, 90.0),  # мин. AI-уверенность для реentri
    # ── DCA улучшения: Компаундирование ──────────────────────────
    "dca_compound_ratio":      (0.05,   0.80),   # доля прибыли в реинвест (0-1)
    # ── DCA улучшения: Адаптивный триггер ────────────────────────
    "dca_adaptive_fast_move_pct":  (1.0,  20.0),  # порог «ракетного» движения %
    "dca_adaptive_fast_drop_pct":  (1.0,  20.0),  # агрессивный порог докупки при ракете %
    # ── Крупные продажи ──────────────────────────────────────────
    "large_sell_dca_ton":      (5.0,   500.0),   # TON для закупки на сигнале крупной продажи
    # ── Защита прибыли ───────────────────────────────────────────
    "profit_protect_ton":      (0.5,    50.0),   # мин. TON прибыли для активации защиты
    # ── AI фильтры входа ─────────────────────────────────────────
    "rsi_overbought":          (65.0,   90.0),   # RSI-уровень перекупленности (блок BUY)
    "ai_autonomous_min_conf":  (40.0,   80.0),   # мин. уверенность для авто-входа AI
    "ai_full_rights_min_conf": (45.0,   85.0),   # мин. уверенность для полных прав AI (без ATR-фильтра)
    "short_min_ai_conf":       (50.0,   90.0),   # мин. уверенность для шорт-позиции
}

# ── Стратегии, которые советник МОЖЕТ включать/выключать целиком ─────────
STRATEGY_TOGGLES = {
    "dca_mode":               "DCA-докупка при падении цены",
    "short_trading_enabled":  "Шорт-позиции (заработок на падении)",
    "smart_buy_enabled":      "Smart BUY (ожидание отката перед входом)",
    "smart_tp_enabled":       "Smart TP (удержание позиции при высокой уверенности AI)",
    "profit_protect_enabled": "Защита прибыли (фиксация при откате от пика)",
    "large_sell_dca_enabled": "Докупка на панике при крупных продажах китов",
    # ── DCA улучшения (4 механизма) ──────────────────────────────
    "dca_cascade_enabled":         "Каскадный выход DCA (продажа 50% на Ур.1, 50% на Ур.2)",
    "dca_smart_reentry_enabled":   "Умный реentri DCA (меньший откат если AI бычий)",
    "dca_compound_enabled":        "Компаундирование DCA (реинвест % прибыли в ставку)",
    "dca_adaptive_trigger_enabled":"Адаптивный триггер DCA (агрессивнее при ракетном движении)",
}

TUNABLE_DESCRIPTIONS = {
    "take_profit_pct":         "Цель прибыли на сделку (%)",
    "dca_target_profit_pct":   "Цель прибыли DCA-портфеля (%)",
    "dca_drop_trigger_pct":    "Докупка DCA при падении (%)",
    "smart_buy_pullback_pct":  "Откат перед Smart BUY (%)",
    "profit_protect_drop_pct": "Откат от пика для защиты прибыли (%)",
    "min_ai_confidence":       "Мин. уверенность AI для входа (%)",
    "buy_threshold":           "Порог BUY — вероятность роста (0-1)",
    "sell_threshold":          "Порог SELL — вероятность падения (0-1)",
    "profit_bias_pct":         "Мин. движение для разметки BUY (0-1)",
    "vr_trend_thresh":         "Variance Ratio для тренда (>1)",
    "ev_min_trades":           "Мин. сделок для EV-фильтра",
    "retrain_every":           "Переобучение каждые N тиков",
    "trailing_stop_pct":       "Базовый трейлинг-стоп (%)",
    "trail_stage2_pct":        "Трейлинг стадия 2 (%)",
    "trail_stage3_pct":        "Трейлинг стадия 3 (%)",
    "trail_stage4_pct":        "Трейлинг стадия 4 (%)",
    "smart_tp_min_conf":       "Мин. уверенность для удержания позиции (%)",
    "short_trail_pct":         "Трейлинг для шорт-позиций (%)",
    "ai_size_mult":            "Множитель размера позиции (money management)",
    "dca_stake_ton":           "Размер DCA-ставки в TON (каждый вход)",
    "trade_amount":            "Базовая ставка AI-режима в TON",
    "min_profit_ton_abs":      "Минимальная прибыль в TON (абсолютная, не %)",
    "trail_breakeven_at":      "Прибыль % для перехода стопа в безубыток",
    "trail_stage2_at":         "Прибыль % для активации трейлинга стадии 2",
    "trail_stage3_at":         "Прибыль % для активации трейлинга стадии 3",
    "trail_stage4_at":         "Прибыль % для активации трейлинга стадии 4",
    "smart_tp_tight_trail_pct":"Тугой трейлинг в режиме Smart TP (%)",
    "dca_pullback_wait_pct":   "Падение от пика перед новым DCA-циклом (%)",
    "dca_max_entries":         "Макс. DCA-входов за один цикл (шт.)",
    # DCA улучшения
    "dca_cascade_level1_pct":        "Каскад Ур.1 — % прибыли для продажи 50% позиции",
    "dca_cascade_level2_pct":        "Каскад Ур.2 — % прибыли для продажи остатка",
    "dca_smart_reentry_pullback_pct":"Умный реentri — мин. откат при AI BUY (%)",
    "dca_smart_reentry_min_ai_conf": "Умный реentri — мин. уверенность AI для быстрого входа (%)",
    "dca_compound_ratio":            "Компаунд — доля прибыли в реинвест (0–1)",
    "dca_adaptive_fast_move_pct":    "Адаптивный триггер — порог 'ракетного' движения (%)",
    "dca_adaptive_fast_drop_pct":    "Адаптивный триггер — агрессивный порог докупки (%)",
    "large_sell_dca_ton":      "TON для закупки при сигнале крупной продажи",
    "profit_protect_ton":      "Мин. прибыль TON для активации защиты прибыли",
    "rsi_overbought":          "RSI-уровень перекупленности (блок входа)",
    "ai_autonomous_min_conf":  "Мин. уверенность AI для автономного входа (%)",
    "ai_full_rights_min_conf": "Мин. уверенность AI для полных прав (без ATR-фильтра) (%)",
    "short_min_ai_conf":       "Мин. уверенность AI для открытия шорта (%)",
}

# ── Внутреннее состояние ───────────────────────────────────────────────────
_lock         = threading.RLock()
_history:     deque     = deque(maxlen=20)
_last_advice: Optional[dict] = None


def _persist_history():
    """Best-effort сохранение истории анализов советника в БД (bot_ai_state).
    Раньше _history жил только в памяти (deque) и полностью терялся при
    каждом рестарте процесса — советник каждый раз "забывал" свои прошлые
    рекомендации. Вызывается уже под _lock."""
    try:
        import db_store
        if db_store.is_available():
            db_store.ai_state_set("advisor_history", list(_history))
            # Сохраняем счётчик адаптаций отдельно, чтобы он пережил рестарт
            db_store.ai_state_set("advisor_meta", {
                "total_adaptations": _total_adaptations,
            })
    except Exception as e:
        logger.warning(f"[Advisor] _persist_history ошибка: {e}")


def _load_history():
    """Восстанавливает историю анализов советника и счётчик адаптаций из БД при старте."""
    global _history, _total_adaptations
    try:
        import db_store
        if not db_store.is_available():
            return
        data = db_store.ai_state_get("advisor_history")
        if isinstance(data, list) and data:
            with _lock:
                _history = deque(data[-20:], maxlen=20)
            logger.info(f"[Advisor] История анализов восстановлена из БД: {len(data)} записей")
        meta = db_store.ai_state_get("advisor_meta")
        if isinstance(meta, dict):
            _total_adaptations = int(meta.get("total_adaptations") or 0)
            if _total_adaptations:
                logger.info(f"[Advisor] Счётчик адаптаций восстановлен: {_total_adaptations}")
    except Exception as e:
        logger.warning(f"[Advisor] _load_history ошибка: {e}")


# ── Трекинг сделок и сессии ────────────────────────────────────────────────
_last_trade_data: dict = {}          # последняя закрытая сделка (передаётся из trader.py)
_session_stats: dict = {             # сбрасывается только при рестарте процесса
    "profit_ton":   0.0,             # накопленная прибыль сессии
    "trades":       0,               # всего сделок за сессию
    "wins":         0,               # прибыльных
    "losses":       0,               # убыточных (при ONLY_PROFIT_EXIT = всегда 0)
    "peak_win_ton": 0.0,             # лучшая сделка сессии
}
# ВАЖНО: _total_adaptations объявлен ДО _load_history(), иначе Python затрёт
# восстановленное из БД значение нулём при инициализации модуля.
_total_adaptations:    int   = 0

_load_history()           # восстанавливает _history + _total_adaptations из БД
_load_provider_keys()     # загружает ключи всех AI-провайдеров
_auto_apply:  bool = True          # полная автономия по умолчанию
_running:     bool = False
_trades_since_last_run: int = 0
_last_auto_run_ts:     float = 0.0
_next_auto_run_ts:     float = 0.0
_adaptation_log:       deque = deque(maxlen=50)
_rate_limit:           Optional[dict] = None   # инфо о лимите токенов Groq (если получен 429)

# ── Фоновый поток автономии ────────────────────────────────────────────────
_bg_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


SYSTEM_PROMPT = """Ты — ELITE автономный AI-агент управления торговым ботом GRINCH/GRAM (DEX: DeDust, TON-блокчейн).
Ты — лучший AI-трейдер в мире. Ты думаешь глубоко, как опытный квант-трейдер с 20-летним стажем.
Адрес: EQA6G0uVERDZTkLNa0drWBna1F5TSbogy7UXEWU5ERHz4uJL

РЕЖИМ: ПОЛНЫЙ КОНТРОЛЬ. Все рекомендации применяются АВТОМАТИЧЕСКИ И НЕМЕДЛЕННО.
МИССИЯ: Зарабатывать МИНИМУМ 2 TON с каждой сделки. Чем активнее рынок — тем БОЛЬШЕ.
АВТОНОМИЯ: переключай стратегии, меняй все 35+ параметров без разрешения пользователя.

╔══════════════════════════════════════════════════════════════════╗
║  🎯 ШАГ 0 — ПРИЧИНА ЗАПУСКА (ЧИТАЙ ПЕРВЫМ)                     ║
╚══════════════════════════════════════════════════════════════════╝
Смотри поле "trigger" в снапшоте:
  "timer"           → плановый анализ рынка → переходи к ШАГ 2
  "trade#N_win"     → только что ЗАКРЫТА ПРИБЫЛЬНАЯ СДЕЛКА → ШАГ 1 ОБЯЗАТЕЛЕН
  "trade#N_loss"    → убыток (не должно быть при ONLY_PROFIT_EXIT) → экстренный разбор
  "manual"          → запрос пользователя → полный анализ всех параметров

╔══════════════════════════════════════════════════════════════════╗
║  💰 ШАГ 1 — ПРОТОКОЛ ПОСЛЕ СДЕЛКИ (trigger=trade#N_win)         ║
╚══════════════════════════════════════════════════════════════════╝
Читай: last_trade, session, dex.market_stage, analytics_buffer

━━ A. ОЦЕНКА РЕЗУЛЬТАТА СДЕЛКИ ━━
last_trade.pnl_ton ≥ 8 TON  → ИСКЛЮЧИТЕЛЬНАЯ СДЕЛКА → АГРЕССИВНЫЙ КОМПАУНД:
  • new_stake = min(current × 1.35, spendable × 0.35, max_2pct_liquidity)
  • take_profit_pct × 1.25 (рынок способен на больше!)
  • min_profit_ton_abs = max(2.0, last_trade.pnl_ton × 0.7)
  • next_check_min = 2 (следи за рынком максимально часто)

last_trade.pnl_ton 4-8 TON → СИЛЬНАЯ СДЕЛКА → УМЕРЕННЫЙ КОМПАУНД:
  • new_stake = min(current × 1.20, spendable × 0.30)
  • take_profit_pct × 1.15 если market_stage = PUMP или ACCUMULATION
  • min_profit_ton_abs = max(2.0, last_trade.pnl_ton × 0.6)
  • next_check_min = 2

last_trade.pnl_ton 2-4 TON → ХОРОШАЯ СДЕЛКА → ЛЁГКИЙ РОСТ:
  • new_stake × 1.10 если рынок позитивный (PUMP/ACCUMULATION/CORRECTION)
  • take_profit_pct × 1.10 если PUMP, иначе держать
  • next_check_min = 3

last_trade.pnl_ton < 2.0 TON → НАРУШЕНИЕ МИНИМУМА → НЕМЕДЛЕННО ИСПРАВИТЬ:
  • Пересчитай: take_profit_needed = (2.5 / current_stake × 100) + fee_2pct
  • dca_target_profit_pct = max(take_profit_needed, current + 3%)
  • min_profit_ton_abs = 2.0 (железный пол)
  • Разбери почему: слишком маленький take_profit? слишком маленькая ставка?

━━ B. НАКОПЛЕННАЯ ПРИБЫЛЬ СЕССИИ (session.profit_ton) ━━
session.profit_ton < 5 TON   → базовые ставки, стандартный режим
session.profit_ton 5-15 TON  → компаунд +15%: new_stake = current × 1.15
session.profit_ton 15-30 TON → компаунд +25%: new_stake = current × 1.25
                                profit_protect_ton = session.profit_ton × 0.4
session.profit_ton > 30 TON  → компаунд +40%: new_stake = current × 1.40
                                profit_protect_ton = session.profit_ton × 0.5
                                min_profit_ton_abs = max(2.0, session.avg_win_ton × 0.8)
ОГРАНИЧЕНИЕ ВСЕГДА: new_stake ≤ min(spendable × 0.35, liquidity_usd_ton × 0.02)

━━ C. АДАПТАЦИЯ СТРАТЕГИИ ПОД РЫНОК ПОСЛЕ СДЕЛКИ ━━
market_stage = PUMP       → smart_tp_enabled=true, trail тоньше, take_profit +15-20%
                             smart_buy_enabled=false (вход без ожиданий)
market_stage = CORRECTION → dca_drop_trigger_pct=3-5%, smart_buy_enabled=false
market_stage = ACCUMULATION → стандарт, smart_buy_enabled=true, плавный вход
market_stage = RANGING    → снизь ставку на 10%, min_ai_confidence ≥ 52
market_stage = DUMP       → ai_size_mult=0.3, min_ai_confidence=80, НЕ ВХОДИТЬ

╔══════════════════════════════════════════════════════════════════╗
║  🧬 ПРОФИЛЬ МОНЕТЫ — ЗНАЙ НАИЗУСТЬ                              ║
╚══════════════════════════════════════════════════════════════════╝
Pepe Grinch (GRINCH) — мем-монета TON, пул DeDust GRINCH/GRAM (1% комиссия пула):
▸ ПАРА: GRINCH/GRAM (GRAM ≈ $1.75, НЕ нативный TON — разные цены!)
▸ ЛИКВИДНОСТЬ: $40k-50k (МАЛАЯ). Всегда смотри dex.liquidity_usd актуально.
▸ РЫНОЧНАЯ КЕПКА: ~$830k micro-cap → потенциал 5-20x при хайпе
▸ ДИАПАЗОН 53ч: $0.000682–$0.000982 (+43.8%). Топ-памп: +23.6% за одну 15м свечу!
▸ ПАТТЕРН: памп +10-24% за 1-2 часа → коррекция → боковик → новый памп (6 баров >5%, 2 бара >10% за 53ч)
▸ ПАМП-ПРИЗНАКИ: свеча >5% с объёмом >3× нормы (~$1k/бар) → начало цикла → входи агрессивно!
▸ ДАВЛЕНИЕ: ratio ПЕРЕМЕННОЕ — 1.12 (24ч, слабо бычий) / 0.40 (1ч, медведи). Следи за ratio_h1/h6 в снапшоте!
▸ ATR реальный: 3.24%/свеча 15м, 4.87%/свеча 1h → трейлинг < 10% = ШУМ, не сигнал; TP мин = ATR×3 = 14.6%
▸ ОБЪЁМ: ~$10.6k/24ч, avg $335/бар (15м). Ставка >$870/вход движет рынком (2% от $43.5k ликвидности)
▸ ВОЛАТИЛЬНОСТЬ: avg ret 15м +0.24%, StdDev 3.94% — жирные хвосты! Тик +10-24% случается (обновл. 21.07.2026)
▸ КОНКУРЕНТЫ: второй пул STON.fi (ликвидность $1) — полностью игнорировать

╔══════════════════════════════════════════════════════════════════╗
║  📊 ШАГ 2 — СТАДИЯ РЫНКА (определи первым при timer-запуске)   ║
╚══════════════════════════════════════════════════════════════════╝
Читай dex.market_stage (предрассчитана), подтвердь по dex.change_h24/h6/h1, dex.ratio_h1:

🟢 КОРРЕКЦИЯ (лучшее окно DCA):
   change_h24 > +15% И (change_h1 < -4% ИЛИ change_h6 < -5%)
   → dca_drop_trigger_pct = 3-5%, smart_buy=OFF, take_profit=25-40%
   → large_sell_dca_enabled=true (продажи китов = дешёвая закупка)
   → Это ПАТТЕРН №1 для GRINCH — максимально агрессивное DCA!

🚀 ПАМП (активный рост — держи и расши):
   change_h1 > +8% ИЛИ vol_ratio.cur > 2.5 ИЛИ ratio_h1 > 2.0
   → take_profit=40-60%, smart_tp=ON, min_ai_confidence=48-50
   → trail расширяй (trail_stage2_pct → 5-6%), НЕ продавай рано!
   → ai_size_mult до 1.3, smart_buy=OFF

📈 НАКОПЛЕНИЕ (постепенный рост):
   change_h24: +5% до +15%, ratio_h24 > 1.2
   → dca_drop_trigger_pct = 3-6%, smart_buy=ON, стандарт
   → Хорошее время для наращивания позиции

➡️ БОКОВИК (ждать импульса):
   |change_h24| < 5%, |change_h1| < 2%
   → ставку -10%, min_ai_confidence ≥ 65, НЕ форсировать вход
   → smart_buy=ON (ждать оптимального момента)

🔴 ДАМП (не входить):
   change_h24 < -15% ИЛИ (ratio_h1 < 0.7 И change_h1 < -8%)
   → ai_size_mult=0.3, min_ai_confidence=80, ждём RSI < 30
   → short_trading_enabled=true если adx > 30 (зарабатывай на падении!)

╔══════════════════════════════════════════════════════════════════╗
║  🔬 ШАГ 3 — ANALYTICS_BUFFER (история ~25 мин)                 ║
╚══════════════════════════════════════════════════════════════════╝
▸ price.direction: "↑ РОСТ"→агрессивнее | "↓ ПАДЕНИЕ"→осторожнее
▸ rsi.cur < 35 → перепродан → BUY сигнал (DCA триггер)
▸ rsi.cur > 72 → перекуплен → скоро продажа, тяни trail
▸ adx.avg > 25 → сильный тренд → повышай ставки на 10-15%
▸ atr_%.avg → РЕАЛЬНАЯ ВОЛАТИЛЬНОСТЬ:
   take_profit_min = atr_%.avg × 3 (иначе сделка не покроет комиссию)
   Если текущий take_profit < atr×3 → поднять немедленно!
▸ vol_ratio.cur > 1.5 → объёмный всплеск → входи агрессивно сейчас
▸ regime.dist_%:
   UPTREND >40% → повысь take_profit, smart_tp=true
   DOWNTREND >30% → DCA обязателен, dca_drop_trigger → 10-15%
   VOLATILE >30% → уменьши ставку, min_ai_confidence → 70+
▸ ai_signals:
   buy_rate_% < 15% → снизь buy_threshold до 0.44-0.46
   blocked_% > 70% → разбери top_blocks, устрани главный барьер
   avg_conf < 55% → повысь profit_bias_pct до 0.035-0.045
▸ dca_analytics.max_profit_% > 2×dca_target → рынок способен больше → повысь цель!
▸ smart_money.cur > 0.3 → умные ПОКУПАЮТ → агрессивнее
▸ smart_money.cur < -0.3 → умные ПРОДАЮТ → осторожнее, жди разворота
▸ recent_ticks[mom]=EXPLOSIVE/SURGE → немедленно увеличь stake
▸ recent_ticks[blk] → устрани главную причину блокировки BUY

╔══════════════════════════════════════════════════════════════════╗
║  💹 ШАГ 4 — DEX МУЛЬТИТАЙМФРЕЙМНЫЙ АНАЛИЗ                     ║
╚══════════════════════════════════════════════════════════════════╝
▸ ratio_h24 > 1.3 → бычий рынок → -3% min_ai_confidence, +0.1 ai_size_mult
▸ ratio_h1 > 1.5 → краткосрочный импульс → входи агрессивно НЕМЕДЛЕННО
▸ ratio_h1 < 0.7 → продавцы доминируют → пауза, жди разворота
▸ recent_flow_usd > 0 → чистый приток → бычий сигнал
▸ change_h6 → промежуточный фрейм: КОРРЕКЦИЯ = h24>+15% И h6<-5%
▸ liquidity_usd < $30,000 → ставку ÷2! (проскальзывание резко растёт)
▸ volume_h6_usd >> нормы → событие на рынке → агрессивнее

╔══════════════════════════════════════════════════════════════════╗
║  💼 ШАГ 5 — РАСЧЁТ СТАВКИ (всегда актуализировать)            ║
╚══════════════════════════════════════════════════════════════════╝
БАЗОВАЯ СТАВКА от wallet.spendable_ton:
  spendable < 50 TON  → базовая = spendable × 0.20 (мин 5 TON)
  spendable 50-200    → базовая = spendable × 0.25
  spendable 200-500   → базовая = spendable × 0.30
  spendable > 500     → базовая = spendable × 0.35 (макс 500 TON/вход)

КОМПАУНД ПОПРАВКА: применяй множитель из ШАГ 1-B поверх базовой ставки
MICRO-CAP ОГРАНИЧЕНИЕ: max_stake = liquidity_usd / GRAM_price × 0.02
trade_amount = dca_stake_ton. НИКОГДА > 40% баланса за один вход.

╔══════════════════════════════════════════════════════════════════╗
║  🎯 ПРАВИЛО 2 TON — АДАПТИВНЫЙ МИНИМУМ ПРИБЫЛИ                 ║
╚══════════════════════════════════════════════════════════════════╝
min_profit_ton_abs ≥ 2.0 ВСЕГДА.
dca_target_profit_pct ≥ max(current, (2.5 / stake_ton × 100) + 2%)
Примеры: stake=10 TON → мин 27%, stake=20 → мин 14.5%, stake=50 → мин 7%, stake=100 → мин 4.5%

АДАПТАЦИЯ ВВЕРХ (чем активнее рынок — тем выше цель):
  ATR > 5% И PUMP stage   → min_profit_ton_abs = max(2.0, 3.5)
  session.profit_ton > 10 → min_profit_ton_abs = max(2.0, session.profit_ton × 0.08)
  Последние 3 сделки > 5 TON каждая → повысь take_profit на 20%

╔══════════════════════════════════════════════════════════════════╗
║  ⚡ ПОЛНАЯ АВТОНОМИЯ СТРАТЕГИЙ — МАТРИЦА РЕШЕНИЙ                ║
╚══════════════════════════════════════════════════════════════════╝
Переключай БЕЗ РАЗРЕШЕНИЯ (iron rule: dca_mode всегда true):

dca_mode:               ВСЕГДА true (железное правило — не трогать)

smart_buy_enabled:
  OFF → PUMP + ratio_h1>1.5 (не ждать — входить сразу!)
  OFF → CORRECTION (ловить каждый тик DCA)
  ON  → ACCUMULATION, RANGING (ждать оптимального момента)

smart_tp_enabled:
  ON  → PUMP, ai_conf>65, UPTREND >40% в буфере (держи дольше!)
  ON  → session.profit_ton > 10 TON (защищай накопленное трейлингом)
  OFF → VOLATILE >40%, DUMP

profit_protect_enabled:  ВСЕГДА true

large_sell_dca_enabled:
  ON  → CORRECTION + micro-cap (продажи китов = возможность!)
  ON  → VOLATILE + ratio_h1 < 0.8
  OFF → стабильный ACCUMULATION/RANGING

short_trading_enabled:
  ON  → DUMP + adx>30 + ratio_h1<0.6 (зарабатывай на падении!)
  OFF → всё остальное (не шортить во время роста/бокового)

╔══════════════════════════════════════════════════════════════════╗
║  🔒 ЖЕЛЕЗНЫЕ ПРАВИЛА (НИКОГДА НЕ НАРУШАТЬ)                     ║
╚══════════════════════════════════════════════════════════════════╝
• ONLY_PROFIT_EXIT = True → никогда не продавать в убыток
• dca_mode = true → DCA всегда включён
• buy_threshold < sell_threshold (зазор ≥ 0.05)
• min_profit_ton_abs ≥ 2.0 TON всегда
• dca_stake_ton ≤ 2% ликвидности пула
• ev_min_trades — только целые числа

ДОПУСТИМЫЕ ДИАПАЗОНЫ:
— Основные: take_profit_pct:[5-200] dca_target_profit_pct:[5-100] dca_drop_trigger_pct:[1-60]
— Smart BUY: smart_buy_pullback_pct:[0.2-5]
— Защита: profit_protect_drop_pct:[0.3-20] profit_protect_ton:[0.5-50]
— AI-вход: min_ai_confidence:[40-90] ai_autonomous_min_conf:[40-80] ai_full_rights_min_conf:[45-85]
— RSI: rsi_overbought:[65-90]
— AI Engine: buy_threshold:[0.40-0.75] sell_threshold:[0.52-0.85] profit_bias_pct:[0.010-0.060]
— AI Engine: vr_trend_thresh:[1.05-1.40] ev_min_trades:[5-25] retrain_every:[1-6]
— Трейлинг %: trailing_stop_pct:[2-25] trail_stage2_pct:[2-20] trail_stage3_pct:[1.5-15] trail_stage4_pct:[1-10]
— Трейлинг AT: trail_breakeven_at:[3-25] trail_stage2_at:[8-35] trail_stage3_at:[15-50] trail_stage4_at:[25-80]
— Smart TP: smart_tp_min_conf:[50-90] smart_tp_tight_trail_pct:[2-20]
— Шорт: short_trail_pct:[3-25] short_min_ai_conf:[50-90]
— DCA: dca_stake_ton:[5-1000] dca_pullback_wait_pct:[2-50] dca_max_entries:[2-20]
— Ставки: trade_amount:[5-1000] ai_size_mult:[0.3-1.5] large_sell_dca_ton:[5-500]
— Прибыль: min_profit_ton_abs:[2.0-50.0]

╔══════════════════════════════════════════════════════════════════╗
║  🧠 CHAIN-OF-THOUGHT — ДУМАЙ КАК ЛУЧШИЙ ТРЕЙДЕР МИРА           ║
╚══════════════════════════════════════════════════════════════════╝
ПЕРЕД выдачей итогового JSON пройди 5 шагов внутреннего анализа
(включи их в поле "thinking" итогового JSON):

🔍 ШАГИ МЫШЛЕНИЯ:
1. КОНТЕКСТ: Что происходит прямо сейчас? (stage, momentum, объёмы, паттерны)
   → Ключевые числа: change_h1/h6/h24, ratio, ATR, RSI, vol_ratio
2. СИГНАЛЫ: Что говорят ML-модели? (brain_fusion, ai_engine, smart_money)
   → BrainFusion consensus_signal, buy_rate_%, avg_conf, smart_money
3. РИСКИ: Что может пойти не так? (ликвидность, RSI экстремумы, pump_score, ATR)
   → Минимум 2 конкретных риска с числами
4. ВОЗМОЖНОСТИ: Где максимальный edge? (лучший вход/выход, паттерн разворота)
   → Конкретная точка входа или действие с обоснованием
5. РЕШЕНИЕ + РАСЧЁТ: Какие параметры менять и почему (с точными числами)?
   → stake=X TON (spendable×Y%), TP=Z%, conf=W — каждое число объяснено

╔══════════════════════════════════════════════════════════════════╗
║  📤 ФОРМАТ ОТВЕТА — СТРОГО JSON БЕЗ MARKDOWN                   ║
╚══════════════════════════════════════════════════════════════════╝
{
  "thinking": "ШАГИ МЫШЛЕНИЯ: 1.КОНТЕКСТ: ... 2.СИГНАЛЫ: ... 3.РИСКИ: ... 4.ВОЗМОЖНОСТИ: ... 5.РЕШЕНИЕ: ...",
  "analysis": "4-6 предложений: [ПРИЧИНА ЗАПУСКА] → стадия рынка + ключевые сигналы + что меняю и ПОЧЕМУ (числа) + ожидаемый результат + компаунд-решение",
  "trade_verdict": "WIN_COMPOUND_AGGRESSIVE | WIN_COMPOUND | WIN_HOLD | FIX_MINIMUM | TIMER_ANALYSIS | DUMP_CAUTION",
  "recommendations": [
    {"param": "dca_stake_ton", "current": 100, "suggested": 120, "reason": "WIN 5.2 TON → компаунд ×1.2; spendable=400×0.30=120 ≤ 2% ликв $47k=OK"},
    {"param": "take_profit_pct", "current": 20, "suggested": 25, "reason": "PUMP stage + ATR=5.2% → мин TP=ATR×3=15.6% → 25% с буфером"},
    {"param": "min_profit_ton_abs", "current": 2.0, "suggested": 3.5, "reason": "stake=120 TON, PUMP, сессия=12 TON → поднимаем планку"}
  ],
  "strategy_toggles": {"dca_mode": true, "smart_tp_enabled": true, "smart_buy_enabled": false, "large_sell_dca_enabled": true},
  "market_verdict": "АКТИВНО_ТОРГОВАТЬ | НАКАПЛИВАТЬ | ОСТОРОЖНО | ПАУЗА | ДАМП",
  "confidence": 0.85,
  "next_check_min": 2
}

ОБЯЗАТЕЛЬНО в КАЖДОМ запуске:
1) Проверь trigger → если trade#N_win, выполни ПРОТОКОЛ ПОСЛЕ СДЕЛКИ (ШАГ 1) ПЕРВЫМ
2) Проверь last_trade.pnl_ton ≥ 2.0. Если нет → FIX_MINIMUM первым действием
3) Скорректируй stake: базовая × компаунд × ≤2% ликвидности
4) Проверь take_profit ≥ ATR×3 (иначе не покрыть комиссию + заработать!)
5) Переключи стратегии автономно по матрице решений
6) next_check_min = 2 при активном рынке или сразу после прибыльной сделки
7) Дай конкретные числа в reason — не абстрактные слова, а расчёт
"""


# ──────────────────────────────────────────────────────────────────────────
# Клиент Groq
# ──────────────────────────────────────────────────────────────────────────
def _effective_key() -> str:
    """Актуальный ключ любого доступного провайдера (для проверки 'хоть что-то есть')."""
    global GROQ_API_KEY
    # Сначала обратная совместимость с Groq
    if GROQ_API_KEY:
        return GROQ_API_KEY
    key = _read_key_file()
    if key:
        GROQ_API_KEY = key
        _provider_keys["groq"] = key
        return key
    # Ищем любой доступный провайдер
    for pid in PROVIDER_CONFIGS:
        k = _read_provider_key(pid)
        if k:
            return k
    return ""


def _get_client():
    """[DEPRECATED] Возвращает Groq клиент для обратной совместимости."""
    key = _effective_key()
    if not key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=key, base_url=GROQ_BASE_URL)
    except Exception as e:
        logger.error(f"[Advisor] клиент: {e}")
        return None


def _get_provider_client(provider_id: str, cfg: dict):
    """Создаёт OpenAI-совместимый клиент для указанного провайдера."""
    key = _read_provider_key(provider_id)
    if not key:
        return None
    try:
        from openai import OpenAI
        # Anthropic поддерживает OpenAI-совместимый API через базовый URL
        client = OpenAI(api_key=key, base_url=cfg["base_url"])
        return client
    except Exception as e:
        logger.error(f"[Advisor] клиент {provider_id}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────
# Снимок состояния бота
# ──────────────────────────────────────────────────────────────────────────
def _compact_advisor_snapshot(snap: dict) -> dict:
    """Сжимает снапшот до безопасного размера для Groq TPM.

    Полный снапшот полезен для локальной диагностики, но SYSTEM_PROMPT уже
    занимает значительную часть лимита Groq. Раньше первый запрос отправлялся
    полным и стабильно получал 413, после чего только второй запрос проходил с
    урезанными данными. Компактный вариант сохраняет торговые показатели и
    убирает только повторяющиеся/тяжёлые истории.
    """
    compact = dict(snap or {})

    if isinstance(compact.get("analytics_buffer"), dict):
        ab = dict(compact["analytics_buffer"])
        for key in ("recent_ticks", "trade_history", "tick_details"):
            ab.pop(key, None)
        if isinstance(ab.get("price"), dict):
            price = dict(ab["price"])
            price.pop("mini_candles", None)
            ab["price"] = price
        compact["analytics_buffer"] = ab

    if isinstance(compact.get("recent_trades"), list):
        compact["recent_trades"] = compact["recent_trades"][-3:]

    # Эти секции дублируют сведения из market/portfolio и могут быть большими.
    # Сначала убираем только тяжёлые детали; базовый рынок и позиция остаются.
    if isinstance(compact.get("brain_fusion"), dict):
        bf = compact["brain_fusion"]
        compact["brain_fusion"] = {
            key: bf[key]
            for key in ("signal", "confidence", "regime", "weights", "sources")
            if key in bf
        }
    if isinstance(compact.get("market_hub"), dict):
        hub = compact["market_hub"]
        compact["market_hub"] = {
            key: hub[key]
            for key in (
                "fear_greed_value", "fear_greed_label", "btc_change24h",
                "ton_cex_change24h", "bybit_funding_rate_pct",
                "grinch_trend_rank", "sources",
            )
            if key in hub
        }
    if isinstance(compact.get("dex"), dict):
        dex = compact["dex"]
        compact["dex"] = {
            key: dex[key]
            for key in (
                "market_stage", "liquidity_usd", "change_h1_pct",
                "change_h24_pct", "ratio_h1", "ratio_h24",
                "buys_h24", "sells_h24", "recent_flow_usd",
            )
            if key in dex
        }

    # Последний предохранитель: если внешние модули добавили неожиданно
    # объёмную секцию, убираем её целиком вместо повторного 413.
    try:
        encoded_len = len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))
        if encoded_len > 24000:
            compact.pop("market_hub", None)
        if len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))) > 20000:
            compact.pop("brain_fusion", None)
        if len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))) > 16000:
            compact.pop("analytics_buffer", None)
    except (TypeError, ValueError):
        pass
    return compact


def _build_snapshot(user_message: str = "", compact: bool = False) -> dict:
    snap: dict = {"timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}

    # Config
    try:
        from config import Config
        def _g(attr, default=None):
            return getattr(Config, attr, default)
        snap["config"] = {
            # ── Основные торговые параметры ──
            "take_profit_pct":          _g("TAKE_PROFIT_PCT"),
            "dca_mode":                 _g("DCA_MODE"),
            "dca_target_profit_pct":    _g("DCA_TARGET_PROFIT_PCT"),
            "dca_drop_trigger_pct":     _g("DCA_DROP_TRIGGER_PCT", 12.0),
            "dca_pullback_wait_pct":    _g("DCA_PULLBACK_WAIT_PCT", 25.0),
            "dca_max_entries":          _g("DCA_MAX_ENTRIES", 10),
            "dca_stake_ton":            _g("DCA_STAKE_TON", 100.0),
            # ── DCA улучшения: текущие параметры ──────────────────
            "dca_cascade_enabled":          _g("DCA_CASCADE_ENABLED", True),
            "dca_cascade_level1_pct":       _g("DCA_CASCADE_LEVEL1_PCT", 20.0),
            "dca_cascade_level2_pct":       _g("DCA_CASCADE_LEVEL2_PCT", 40.0),
            "dca_smart_reentry_enabled":    _g("DCA_SMART_REENTRY_ENABLED", True),
            "dca_smart_reentry_pullback_pct": _g("DCA_SMART_REENTRY_PULLBACK_PCT", 8.0),
            "dca_smart_reentry_min_ai_conf":  _g("DCA_SMART_REENTRY_MIN_AI_CONF", 60.0),
            "dca_compound_enabled":         _g("DCA_COMPOUND_ENABLED", True),
            "dca_compound_ratio":           _g("DCA_COMPOUND_RATIO", 0.30),
            "dca_compound_max_ton":         _g("DCA_COMPOUND_MAX_TON", 500.0),
            "dca_adaptive_trigger_enabled": _g("DCA_ADAPTIVE_TRIGGER_ENABLED", True),
            "dca_adaptive_fast_move_pct":   _g("DCA_ADAPTIVE_FAST_MOVE_PCT", 5.0),
            "dca_adaptive_fast_drop_pct":   _g("DCA_ADAPTIVE_FAST_DROP_PCT", 6.0),
            "trade_amount":             _g("TRADE_AMOUNT", 100.0),
            "min_profit_ton_abs":       _g("MIN_PROFIT_TON_ABS", 2.0),
            # ── AI фильтры входа ──
            "min_ai_confidence":        _g("MIN_AI_CONFIDENCE"),
            "ai_autonomous_min_conf":   _g("AI_AUTONOMOUS_MIN_CONF", 55.0),
            "ai_full_rights_min_conf":  _g("AI_FULL_RIGHTS_MIN_CONF", 62.0),
            "rsi_overbought":           _g("RSI_OVERBOUGHT", 78.0),
            # ── Трейлинг ──
            "trailing_stop_pct":        _g("TRAILING_STOP_PCT"),
            "trail_breakeven_at":       _g("TRAIL_BREAKEVEN_AT", 10.0),
            "trail_stage2_at":          _g("TRAIL_STAGE2_AT", 18.0),
            "trail_stage2_pct":         _g("TRAIL_STAGE2_PCT"),
            "trail_stage3_at":          _g("TRAIL_STAGE3_AT", 28.0),
            "trail_stage3_pct":         _g("TRAIL_STAGE3_PCT"),
            "trail_stage4_at":          _g("TRAIL_STAGE4_AT", 40.0),
            "trail_stage4_pct":         _g("TRAIL_STAGE4_PCT"),
            "smart_tp_min_conf":        _g("SMART_TP_MIN_CONF"),
            "smart_tp_tight_trail_pct": _g("SMART_TP_TIGHT_TRAIL_PCT", 6.0),
            # ── Защита прибыли ──
            "profit_protect_drop_pct":  _g("PROFIT_PROTECT_DROP_PCT"),
            "profit_protect_ton":       _g("PROFIT_PROTECT_TON", 3.0),
            # ── Smart BUY ──
            "smart_buy_pullback_pct":   _g("SMART_BUY_PULLBACK_PCT"),
            # ── Крупные продажи ──
            "large_sell_dca_ton":       _g("LARGE_SELL_DCA_TON", 100.0),
            # ── Шорт ──
            "short_min_ai_conf":        _g("SHORT_MIN_AI_CONF", 65.0),
            "short_trail_pct":          _g("SHORT_TRAIL_PCT"),
            # ── Прочее ──
            "ai_size_mult":             _g("AI_SIZE_MULT"),
            "only_profit_exit":         _g("ONLY_PROFIT_EXIT"),
            "fee_round_trip_pct":       _g("FEE_ROUND_TRIP"),
        }
    except Exception:
        snap["config"] = {}

    # AI Engine текущие параметры
    try:
        import ai_engine as ae
        snap["ai_engine"] = {
            "buy_threshold":   ae.BUY_THRESHOLD,
            "sell_threshold":  ae.SELL_THRESHOLD,
            "profit_bias_pct": ae.PROFIT_BIAS_PCT,
            "vr_trend_thresh": ae.VR_TREND_THRESH,
            "ev_min_trades":   ae.EV_MIN_TRADES,
            "retrain_every":   ae.RETRAIN_EVERY,
            "kelly_lookback":  ae.KELLY_LOOKBACK,
        }
    except Exception:
        snap["ai_engine"] = {}

    # ── BrainFusion: полное состояние единого мозга ───────────────────────────
    try:
        snap["brain_fusion"] = _bf.get_state()
    except Exception:
        snap["brain_fusion"] = {}

    # Производительность (история сделок)
    try:
        from experience_manager import experience_manager
        rep = experience_manager.get_report()
        snap["performance"] = {
            "total_trades": rep.get("total_trades", 0),
            "win_rate_pct": rep.get("win_rate", 0),
            "avg_profit_pct": rep.get("avg_profit_pct", 0),
            "sharpe":        rep.get("sharpe", 0),
            "best_regime":   rep.get("best_regime", "?"),
            "worst_regime":  rep.get("worst_regime", "?"),
        }
        # Последние 7 сделок — полный контекст для компаунд-протокола
        trades = experience_manager.data.get("trades", [])[-7:]
        snap["recent_trades"] = [
            {
                "pnl_ton":      t.get("pnl_ton") or t.get("pnl"),
                "pnl_pct":      t.get("pnl_pct", 0),
                "stake_ton":    t.get("stake_ton"),
                "outcome":      t.get("outcome", "?"),
                "regime":       t.get("entry_regime", "?"),
                "close_reason": t.get("close_reason", "?"),
                "strategy":     t.get("strategy", "?"),
                "duration_min": t.get("duration_min"),
                "dca_entries":  t.get("dca_entries"),
            }
            for t in trades
        ]
    except Exception:
        snap["performance"] = {}
        snap["recent_trades"] = []

    # ── Последняя сделка (ключевой вход для компаунд-протокола) ─────────────
    snap["last_trade"] = dict(_last_trade_data) if _last_trade_data else {}

    # ── Статистика сессии ────────────────────────────────────────────────────
    ss = dict(_session_stats)
    snap["session"] = {
        "profit_ton":   round(ss["profit_ton"], 4),
        "trades":       ss["trades"],
        "wins":         ss["wins"],
        "losses":       ss["losses"],
        "win_rate_pct": round(ss["wins"] / ss["trades"] * 100, 1) if ss["trades"] > 0 else 0,
        "peak_win_ton": ss["peak_win_ton"],
        "avg_win_ton":  round(ss["profit_ton"] / ss["wins"], 4) if ss["wins"] > 0 else 0,
    }

    # Рынок
    try:
        from trader import trader
        ai = getattr(trader, "last_ai", None) or {}
        regime = ai.get("regime") or {}
        snap["market"] = {
            "price_usd":  ai.get("price", 0),
            "signal":     ai.get("ai_signal", "HOLD"),
            "regime":     regime.get("name", "?"),
            "atr_pct":    regime.get("atr_pct", 0),
            "rsi":        ai.get("rsi", 50),
            "adx":        regime.get("adx", 0),
            "prob_up":    round(float(ai.get("prob_up", 0)), 3),
            "prob_down":  round(float(ai.get("prob_down", 0)), 3),
            "confidence": ai.get("confidence", 0),
            "pump":       ai.get("pump", "NONE"),
            "var_ratio":  round(float(ai.get("var_ratio", 1.0)), 3),
            "24h_change_pct": 0.0,   # обновляется ниже из DexScreener
        }
        snap["portfolio"] = {
            "open_positions": len(trader.open_trades),
            "total_pnl_ton":  trader.stats.get("total_pnl", 0),
            "winning_trades": trader.stats.get("winning_trades", 0),
            "total_trades":   trader.stats.get("total_trades", 0),
        }
        # Реальная цена из DexScreener
        try:
            from price_feed import price_feed
            snap["market"]["price_usd"]      = price_feed.get("GRINCH") or snap["market"]["price_usd"]
            snap["market"]["24h_change_pct"] = getattr(price_feed, "_last_change_24h", -7.44)
        except Exception:
            pass
    except Exception as ex:
        snap["market"]    = {}
        snap["portfolio"] = {}

    # ── Баланс кошелька + P&L позиции из wallet_manager (полные данные) ────────
    try:
        from wallet_manager import wallet_manager as _wm
        _wsnap = _wm.get_snapshot()
        _ton   = float(_wsnap.get("ton_balance",    0) or 0)
        _grn   = float(_wsnap.get("grinch_balance", 0) or 0)
        from config import Config as _Cfg
        _reserve   = float(getattr(_Cfg, 'GAS_RESERVE_TON', 0.45))
        _buy_gas   = float(getattr(_Cfg, 'BUY_GAS_TON', 0.103))
        _spendable = max(0.0, _ton - _reserve - _buy_gas)
        snap["wallet"] = {
            "ton_balance":      round(_ton, 4),
            "grinch_balance":   round(_grn, 2),
            "spendable_ton":    round(_spendable, 4),
            "gas_reserve":      _reserve,
            "grinch_price_ton": _wsnap.get("grinch_price_ton"),
            "grinch_price_usd": _wsnap.get("grinch_price_usd"),
            "ton_price_usd":    _wsnap.get("ton_price_usd"),
            "grinch_value_ton": _wsnap.get("grinch_value_ton"),
            "total_equity_ton": _wsnap.get("total_equity_ton"),
            "total_equity_usd": _wsnap.get("total_equity_usd"),
        }
        # ── Позиция GRINCH: цена входа и P&L ──────────────────────────────
        snap["position"] = {
            "in_position":       _grn > 0,
            "grinch_count":      round(_grn, 2),
            "entry_price_ton":   _wsnap.get("entry_price_ton"),
            "entry_price_usd":   _wsnap.get("entry_price_usd"),
            "current_price_ton": _wsnap.get("grinch_price_ton"),
            "current_price_usd": _wsnap.get("grinch_price_usd"),
            "pnl_ton":           _wsnap.get("pnl_ton"),
            "pnl_pct":           _wsnap.get("pnl_pct"),
            "pnl_usd":           _wsnap.get("pnl_usd"),
        }
    except Exception:
        try:
            from trader import trader as _tr
            _bal = _tr._get_balance_cached() if hasattr(_tr, '_get_balance_cached') else {}
            if not _bal:
                _bal = _tr.exchange.get_balance() if hasattr(_tr, 'exchange') else {}
            _ton = float(_bal.get("TON", 0) or 0)
            _grn = float(_bal.get("GRINCH", 0) or 0)
            from config import Config as _Cfg
            _reserve   = float(getattr(_Cfg, 'GAS_RESERVE_TON', 0.45))
            _buy_gas   = float(getattr(_Cfg, 'BUY_GAS_TON', 0.103))
            _spendable = max(0.0, _ton - _reserve - _buy_gas)
            snap["wallet"]   = {"ton_balance": round(_ton,4), "grinch_balance": round(_grn,2), "spendable_ton": round(_spendable,4), "gas_reserve": _reserve}
            snap["position"] = {"in_position": _grn>0, "grinch_count": round(_grn,2)}
        except Exception:
            snap["wallet"]   = {"ton_balance": 0, "grinch_balance": 0, "spendable_ton": 0}
            snap["position"] = {"in_position": False}

    # Ликвидность пула (LiquidityGuard)
    try:
        import liquidity_guard
        lg = liquidity_guard.get_status()
        snap["liquidity"] = {
            "current_usd":  lg.get("current_liq", 0),
            "peak_usd":     lg.get("peak_liq", 0),
            "drop_pct":     lg.get("drop_pct", 0),
            "buys_paused":  lg.get("buys_paused", False),
            "pause_reason": lg.get("pause_reason", ""),
        }
    except Exception:
        snap["liquidity"] = {}

    # ── DataHub: внешние рыночные данные из 6 бесплатных источников ─────────
    try:
        from data_hub import get_snapshot as _hub_snap, get_source_status as _hub_st
        _hub = _hub_snap()
        if _hub:
            snap["market_hub"] = {
                # Fear & Greed
                "fear_greed_value": _hub.get("fg_value"),
                "fear_greed_label": _hub.get("fg_label"),
                "fear_greed_delta": _hub.get("fg_delta"),
                # BTC
                "btc_price":        round(_hub.get("btc_price", 0), 0),
                "btc_change24h":    _hub.get("btc_change24h"),
                "btc_volume24h_b":  round((_hub.get("btc_volume24h", 0) or 0) / 1e9, 2),
                # TON на Binance
                "ton_cex_price":    _hub.get("ton_cex_price"),
                "ton_cex_change24h": _hub.get("ton_cex_change24h"),
                "ton_cex_volume24h_m": round((_hub.get("ton_cex_volume24h", 0) or 0) / 1e6, 2),
                # Bybit фьючерсы
                "bybit_funding_rate_pct": _hub.get("bybit_funding_rate_pct"),
                "bybit_oi_m":        round((_hub.get("bybit_oi", 0) or 0) / 1e6, 2),
                # DeFiLlama
                "ton_tvl_m":         round((_hub.get("ton_tvl", 0) or 0) / 1e6, 2),
                "ton_tvl_change_pct": _hub.get("ton_tvl_change"),
                "stonfi_tvl_m":      round((_hub.get("stonfi_tvl", 0) or 0) / 1e6, 2),
                # GeckoTerminal тренды
                "grinch_trend_rank":  _hub.get("grinch_trend_rank", 0),
                "grinch_trend_vol24h_k": round((_hub.get("grinch_trend_vol24h", 0) or 0) / 1e3, 1),
                "ton_trending_pools": _hub.get("ton_trending_pools"),
                # TON сеть
                "ton_tx24h_k":  round((_hub.get("ton_tx24h", 0) or 0) / 1e3, 1),
                "ton_accounts_m": round((_hub.get("ton_accounts", 0) or 0) / 1e6, 3),
                # Статус источников
                "sources": [{s["source"]: ("✅" if s["fresh"] else "⚠️" if not s["stale"] else "❌")}
                            for s in _hub_st()],
            }
    except Exception:
        snap["market_hub"] = {}

    # ── Глубокие данные прямо с DeDust/DexScreener (не из analytics_buffer) ─
    try:
        from coin_info import coin_info
        m = coin_info.market("GRINCH") or {}
        buys  = m.get("buys_h24")  or 0
        sells = m.get("sells_h24") or 0
        recent = coin_info.trades("GRINCH", limit=30) or []
        flow_usd = 0.0
        buy_cnt = sell_cnt = 0
        for t in recent:
            usd = t.get("amount_usd") or 0.0
            if t.get("kind") == "buy":
                flow_usd += usd
                buy_cnt += 1
            elif t.get("kind") == "sell":
                flow_usd -= usd
                sell_cnt += 1
        # ── Определяем стадию рынка по multi-таймфреймным данным ───────────
        ch24 = m.get("change_h24") or 0.0
        ch6  = m.get("change_h6")  or 0.0
        ch1  = m.get("change_h1")  or 0.0
        r_h1 = m.get("ratio_h1")
        if ch24 > 15 and (ch1 < -4 or ch6 < -5):
            _stage = "CORRECTION"     # памп + текущая коррекция → лучшее окно DCA
        elif ch1 > 8 or (r_h1 and r_h1 > 2.0):
            _stage = "PUMP"           # активный памп прямо сейчас
        elif ch24 > 5 and (m.get("ratio_h24") or 1.0) > 1.2:
            _stage = "ACCUMULATION"   # плавный рост с перевесом покупателей
        elif ch24 < -15 or (r_h1 and r_h1 < 0.7 and ch1 < -8):
            _stage = "DUMP"           # дамп — не входить
        else:
            _stage = "RANGING"        # боковик — ждать импульса
        snap["dex"] = {
            "source":            m.get("source", "?"),
            "market_stage":      _stage,
            "volume_h24_usd":    m.get("volume_h24"),
            "volume_h6_usd":     m.get("volume_h6"),
            "volume_h1_usd":     m.get("volume_h1"),
            "liquidity_usd":     m.get("liquidity"),
            "fdv_usd":           m.get("fdv"),
            "change_m5_pct":     m.get("change_m5"),
            "change_h1_pct":     ch1,
            "change_h6_pct":     ch6,
            "change_h24_pct":    ch24,
            "buys_h24":          buys,
            "sells_h24":         sells,
            "ratio_h24":         m.get("ratio_h24") or (round(buys / sells, 3) if sells else None),
            "buys_h6":           m.get("buys_h6"),
            "sells_h6":          m.get("sells_h6"),
            "ratio_h6":          m.get("ratio_h6"),
            "buys_h1":           m.get("buys_h1"),
            "sells_h1":          m.get("sells_h1"),
            "ratio_h1":          r_h1,
            "recent_trades_n":   len(recent),
            "recent_buy_count":  buy_cnt,
            "recent_sell_count": sell_cnt,
            "recent_flow_usd":   round(flow_usd, 2),
        }
    except Exception as _dex_e:
        snap["dex"] = {"error": str(_dex_e)}

    # ── Аналитический буфер (история рынка — ГЛАВНЫЙ инструмент советника) ──
    # Содержит: цену, индикаторы, режимы, AI-сигналы, DCA-прогресс,
    # умные деньги, историю сделок за последние ~7.5 минут тиков.
    try:
        from analytics_buffer import analytics_buffer as _ab
        n_ticks = _ab.tick_count()
        if n_ticks >= 3:
            # window=30 (~7.5 мин, было 50) — снапшот всё ещё упирался в
            # дневной TPD-лимит Groq (100k) при частых авто-запусках, поэтому
            # дополнительно урезано вместе с ростом AUTO_INTERVAL_MIN/TRIGGER.
            # 30×15с = 7.5 мин истории — достаточно для тактических решений
            # по входу/выходу, при этом запрос стабильно укладывается в TPM.
            summary = _ab.get_advisor_summary(window=30)
            # Обрезаем самые тяжёлые поля в ответе для экономии токенов
            if isinstance(summary, dict):
                if "recent_ticks" in summary:
                    summary["recent_ticks"] = summary["recent_ticks"][-3:]  # только 3 последних
                if isinstance(summary.get("price"), dict):
                    summary["price"].pop("mini_candles", None)  # убираем мини-свечи из промпта
            snap["analytics_buffer"] = summary
        else:
            snap["analytics_buffer"] = {
                "status": f"накапливается данные: {n_ticks}/3 тиков получено",
                "ticks": n_ticks,
            }
    except Exception as _ab_e:
        snap["analytics_buffer"] = {"error": str(_ab_e)}

    # Адаптации советника
    snap["advisor_stats"] = {
        "total_adaptations": _total_adaptations,
        "trades_since_last_run": _trades_since_last_run,
        "last_run": datetime.utcfromtimestamp(_last_auto_run_ts).strftime("%H:%M") if _last_auto_run_ts else "—",
    }

    if user_message:
        snap["user_question"] = user_message

    return _compact_advisor_snapshot(snap) if compact else snap


# ──────────────────────────────────────────────────────────────────────────
# Парсинг ответа LLM
# ──────────────────────────────────────────────────────────────────────────
def _parse_response(text: str) -> dict:
    text = text.strip()
    s, e = text.find("{"), text.rfind("}") + 1
    if s == -1 or e == 0:
        return {"analysis": text, "recommendations": [],
                "market_verdict": "ОСТОРОЖНО", "confidence": 0.5, "next_check_min": AUTO_INTERVAL_MIN}
    try:
        return json.loads(text[s:e])
    except json.JSONDecodeError:
        return {"analysis": text, "recommendations": [],
                "market_verdict": "ОСТОРОЖНО", "confidence": 0.5, "next_check_min": AUTO_INTERVAL_MIN}


# ──────────────────────────────────────────────────────────────────────────
# Применение рекомендаций
# ──────────────────────────────────────────────────────────────────────────
def _apply_recommendations(recs: list) -> list[str]:
    global _total_adaptations
    applied = []
    if not recs:
        return applied

    try:
        from config import Config
        import ai_engine as ae
        from settings_store import update_section
    except Exception as ex:
        logger.error(f"[Advisor] импорт: {ex}")
        return applied

    config_upd: dict = {}

    for rec in recs:
        param   = rec.get("param", "")
        val_raw = rec.get("suggested")
        if param not in TUNABLE or val_raw is None:
            continue
        try:
            val = float(val_raw)
        except (TypeError, ValueError):
            continue
        lo, hi = TUNABLE[param]
        val = max(lo, min(hi, val))

        # ── Config параметры ──────────────────────────────────────────
        if param == "take_profit_pct":
            Config.TAKE_PROFIT_PCT = val
            config_upd["take_profit_pct"] = str(val)
        elif param == "dca_target_profit_pct":
            Config.DCA_TARGET_PROFIT_PCT = val
            config_upd["dca_target_profit_pct"] = str(val)
        elif param == "dca_drop_trigger_pct":
            Config.DCA_DROP_TRIGGER_PCT = val
            config_upd["dca_drop_trigger_pct"] = str(val)
        elif param == "smart_buy_pullback_pct":
            Config.SMART_BUY_PULLBACK_PCT = val
            config_upd["smart_buy_pullback_pct"] = str(val)
        elif param == "profit_protect_drop_pct":
            Config.PROFIT_PROTECT_DROP_PCT = val
            config_upd["profit_protect_drop_pct"] = str(val)
        elif param == "min_ai_confidence":
            Config.MIN_AI_CONFIDENCE = val
            config_upd["min_ai_confidence"] = str(val)
        elif param == "trailing_stop_pct":
            Config.TRAILING_STOP_PCT = val
            config_upd["trailing_stop_pct"] = str(val)
        elif param == "trail_stage2_pct":
            Config.TRAIL_STAGE2_PCT = val
            config_upd["trail_stage2_pct"] = str(val)
        elif param == "trail_stage3_pct":
            Config.TRAIL_STAGE3_PCT = val
            config_upd["trail_stage3_pct"] = str(val)
        elif param == "trail_stage4_pct":
            Config.TRAIL_STAGE4_PCT = val
            config_upd["trail_stage4_pct"] = str(val)
        elif param == "smart_tp_min_conf":
            Config.SMART_TP_MIN_CONF = val
            config_upd["smart_tp_min_conf"] = str(val)
        elif param == "short_trail_pct":
            Config.SHORT_TRAIL_PCT = val
            config_upd["short_trail_pct"] = str(val)
        elif param == "ai_size_mult":
            Config.AI_SIZE_MULT = val
            config_upd["ai_size_mult"] = str(val)
        elif param == "dca_stake_ton":
            Config.DCA_STAKE_TON = val
            config_upd["dca_stake_ton"] = str(val)
        elif param == "trade_amount":
            Config.TRADE_AMOUNT = val
            config_upd["trade_amount"] = str(val)
        elif param == "min_profit_ton_abs":
            # Гарантируем абсолютный минимум 2 TON
            val = max(val, 2.0)
            Config.MIN_PROFIT_TON_ABS = val
            config_upd["min_profit_ton_abs"] = str(val)

        # ── AI Engine параметры ───────────────────────────────────────
        elif param == "buy_threshold":
            # Гарантируем buy < sell
            if val < ae.SELL_THRESHOLD:
                ae.BUY_THRESHOLD = val
            else:
                ae.BUY_THRESHOLD = max(lo, ae.SELL_THRESHOLD - 0.05)
                val = ae.BUY_THRESHOLD
        elif param == "sell_threshold":
            # Гарантируем sell > buy
            if val > ae.BUY_THRESHOLD:
                ae.SELL_THRESHOLD = val
            else:
                ae.SELL_THRESHOLD = min(hi, ae.BUY_THRESHOLD + 0.05)
                val = ae.SELL_THRESHOLD
        elif param == "profit_bias_pct":
            ae.PROFIT_BIAS_PCT = val
        elif param == "vr_trend_thresh":
            ae.VR_TREND_THRESH = val
        elif param == "ev_min_trades":
            ae.EV_MIN_TRADES = int(round(val))
            val = ae.EV_MIN_TRADES
        elif param == "retrain_every":
            ae.RETRAIN_EVERY = max(1, int(round(val)))
            val = ae.RETRAIN_EVERY

        # ── Трейлинг: уровни активации стадий ────────────────────
        elif param == "trail_breakeven_at":
            Config.TRAIL_BREAKEVEN_AT = val
            config_upd["trail_breakeven_at"] = str(val)
        elif param == "trail_stage2_at":
            Config.TRAIL_STAGE2_AT = val
            config_upd["trail_stage2_at"] = str(val)
        elif param == "trail_stage3_at":
            Config.TRAIL_STAGE3_AT = val
            config_upd["trail_stage3_at"] = str(val)
        elif param == "trail_stage4_at":
            Config.TRAIL_STAGE4_AT = val
            config_upd["trail_stage4_at"] = str(val)
        elif param == "smart_tp_tight_trail_pct":
            Config.SMART_TP_TIGHT_TRAIL_PCT = val
            config_upd["smart_tp_tight_trail_pct"] = str(val)

        # ── DCA расширенные ───────────────────────────────────────
        elif param == "dca_pullback_wait_pct":
            Config.DCA_PULLBACK_WAIT_PCT = val
            config_upd["dca_pullback_wait_pct"] = str(val)
        elif param == "dca_max_entries":
            Config.DCA_MAX_ENTRIES = max(2, int(round(val)))
            val = Config.DCA_MAX_ENTRIES
            config_upd["dca_max_entries"] = str(val)

        # ── Крупные продажи ───────────────────────────────────────
        elif param == "large_sell_dca_ton":
            Config.LARGE_SELL_DCA_TON = val
            config_upd["large_sell_dca_ton"] = str(val)

        # ── Защита прибыли ────────────────────────────────────────
        elif param == "profit_protect_ton":
            Config.PROFIT_PROTECT_TON = val
            config_upd["profit_protect_ton"] = str(val)

        # ── AI фильтры входа ──────────────────────────────────────
        elif param == "rsi_overbought":
            Config.RSI_OVERBOUGHT = val
            config_upd["rsi_overbought"] = str(val)
        elif param == "ai_autonomous_min_conf":
            Config.AI_AUTONOMOUS_MIN_CONF = val
            config_upd["ai_autonomous_min_conf"] = str(val)
        elif param == "ai_full_rights_min_conf":
            Config.AI_FULL_RIGHTS_MIN_CONF = val
            config_upd["ai_full_rights_min_conf"] = str(val)
        elif param == "short_min_ai_conf":
            Config.SHORT_MIN_AI_CONF = val
            config_upd["short_min_ai_conf"] = str(val)

        else:
            continue

        desc  = TUNABLE_DESCRIPTIONS.get(param, param)
        label = f"{desc}: {rec.get('current', '?')} → {val:.3g}"
        applied.append(label)
        logger.info(f"[Advisor] ✅ {label}")

    # Железные замки
    try:
        Config.ONLY_PROFIT_EXIT = True
        Config.DCA_MODE = True  # DCA всегда включён
        # Гарантируем мин. абсолютную прибыль 2 TON
        if not hasattr(Config, "MIN_PROFIT_TON_ABS") or Config.MIN_PROFIT_TON_ABS < 2.0:
            Config.MIN_PROFIT_TON_ABS = 2.0
    except Exception:
        pass

    if config_upd:
        try:
            update_section("config", config_upd)
        except Exception as ex:
            logger.warning(f"[Advisor] settings_store: {ex}")

    if applied:
        _total_adaptations += len(applied)
        ts = datetime.utcnow().strftime("%H:%M:%S")
        for a in applied:
            _adaptation_log.append({"ts": ts, "change": a})

    return applied


_TOGGLE_ATTR = {
    "dca_mode":               "DCA_MODE",
    "short_trading_enabled":  "SHORT_TRADING_ENABLED",
    "smart_buy_enabled":      "SMART_BUY_ENABLED",
    "smart_tp_enabled":       "SMART_TP_ENABLED",
    "profit_protect_enabled": "PROFIT_PROTECT_ENABLED",
    "large_sell_dca_enabled": "LARGE_SELL_DCA_ENABLED",
    # ── DCA улучшения ───────────────────────────────────────────
    "dca_cascade_enabled":          "DCA_CASCADE_ENABLED",
    "dca_smart_reentry_enabled":    "DCA_SMART_REENTRY_ENABLED",
    "dca_compound_enabled":         "DCA_COMPOUND_ENABLED",
    "dca_adaptive_trigger_enabled": "DCA_ADAPTIVE_TRIGGER_ENABLED",
}


def _apply_strategy_toggles(toggles: dict) -> list[str]:
    """Включает/выключает целые торговые стратегии по решению советника."""
    global _total_adaptations
    applied = []
    if not toggles:
        return applied
    try:
        from config import Config
        from settings_store import update_section
    except Exception as ex:
        logger.error(f"[Advisor] импорт (toggles): {ex}")
        return applied

    config_upd: dict = {}
    for key, raw_val in toggles.items():
        if key not in STRATEGY_TOGGLES:
            continue
        attr = _TOGGLE_ATTR[key]
        try:
            new_val = bool(raw_val)
        except Exception:
            continue
        old_val = bool(getattr(Config, attr, None))
        if old_val == new_val:
            continue
        setattr(Config, attr, new_val)
        config_upd[key] = "1" if new_val else "0"
        desc  = STRATEGY_TOGGLES[key]
        label = f"{desc}: {'ВКЛ' if old_val else 'ВЫКЛ'} → {'ВКЛ' if new_val else 'ВЫКЛ'}"
        applied.append(label)
        logger.info(f"[Advisor] 🔀 {label}")

    # DCA ВСЕГДА включён — железный замок после применения переключателей
    try:
        from config import Config as _cfg
        if not getattr(_cfg, "DCA_MODE", True):
            _cfg.DCA_MODE = True
            config_upd["dca_mode"] = "1"
            logger.info("[Advisor] 🔒 DCA принудительно возвращён в ВКЛ (железное правило)")
    except Exception:
        pass

    if config_upd:
        try:
            update_section("config", config_upd)
        except Exception as ex:
            logger.warning(f"[Advisor] settings_store (toggles): {ex}")

    if applied:
        _total_adaptations += len(applied)
        ts = datetime.utcnow().strftime("%H:%M:%S")
        for a in applied:
            _adaptation_log.append({"ts": ts, "change": a})

    return applied


# ──────────────────────────────────────────────────────────────────────────
# Основной запрос к советнику
# ──────────────────────────────────────────────────────────────────────────
def run_advisor(auto_apply: bool = None, user_message: str = "",
                trigger: str = "manual") -> dict:
    global _running, _last_advice, _last_auto_run_ts, _next_auto_run_ts
    global _trades_since_last_run

    apply = auto_apply if auto_apply is not None else _auto_apply

    if not _effective_key():
        return {"ok": False, "error": "Нет ключа ни одного AI-провайдера. Добавь ключ в настройках советника."}

    # ── Выбираем лучший доступный провайдер ──────────────────────────────────
    provider_id, provider_cfg = _get_best_provider()
    client = _get_provider_client(provider_id, provider_cfg)
    if not client:
        return {"ok": False, "error": f"Клиент {provider_cfg['name']} недоступен"}

    # ── Проактивная проверка rate-limit (только для Groq) ────────────────────
    if provider_id == "groq":
        rl = _rate_limit_status()
        if rl and rl.get("reset_in_sec") is not None and rl.get("reset_in_sec", 0) > 0:
            wait_sec = rl["reset_in_sec"]
            # Пробуем фолбэк на другой провайдер
            for pid, pcfg in sorted(PROVIDER_CONFIGS.items(), key=lambda x: x[1]["priority"]):
                if pid == "groq":
                    continue
                k = _read_provider_key(pid)
                if k:
                    client = _get_provider_client(pid, pcfg)
                    if client:
                        provider_id, provider_cfg = pid, pcfg
                        logger.info(f"[Advisor] Groq rate-limit → фолбэк на {pcfg['name']}")
                        break
            else:
                return {
                    "ok": False,
                    "error": f"Groq rate-limit активен, сброс через ~{wait_sec}с",
                    "rate_limit": rl,
                }

    with _lock:
        if _running:
            return {"ok": False, "error": "Советник уже работает…"}
        _running = True

    try:
        # Для Groq сразу используем компактный payload: полный снапшот
        # превышает TPM и раньше вызывал гарантированный 413 + повтор.
        snap          = _build_snapshot(user_message, compact=(provider_id == "groq"))
        snap["trigger"] = trigger
        snap_str      = json.dumps(snap, ensure_ascii=False, indent=2)

        # ── Chain-of-Thought инструкция для максимального качества мышления ──
        cot_hint = (
            "\n\n🧠 ВАЖНО: Заполни поле 'thinking' в JSON — пройди 5 шагов мышления "
            "(КОНТЕКСТ→СИГНАЛЫ→РИСКИ→ВОЗМОЖНОСТИ→РЕШЕНИЕ) ПЕРЕД итоговыми рекомендациями. "
            "Думай как лучший квант-трейдер мира. Каждый параметр обоснуй числами."
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content":
                f"Текущее состояние бота:\n```json\n{snap_str}\n```{cot_hint}"},
        ]

        model_name = provider_cfg["model"]
        max_toks   = provider_cfg.get("max_tokens", 1500)
        temperature = provider_cfg.get("temperature", 0.2)

        logger.info(f"[Advisor] 🤖 Запрос к {provider_cfg['name']} ({trigger})…")
        t0   = time.time()

        # Anthropic claude через OpenAI-совместимый API (messages API поддерживается)
        create_kwargs = dict(
            model=model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_toks,
        )

        resp    = client.chat.completions.create(**create_kwargs)
        elapsed = round(time.time() - t0, 1)
        raw     = resp.choices[0].message.content or ""
        logger.info(f"[Advisor] ✅ {provider_cfg['name']} ответ за {elapsed}s")

        parsed  = _parse_response(raw)
        applied = []
        if apply:
            applied = _apply_recommendations(parsed.get("recommendations", []))
            applied += _apply_strategy_toggles(parsed.get("strategy_toggles", {}))

        # Следующий запуск через столько минут, сколько советник сам рекомендовал
        suggested_next = int(parsed.get("next_check_min", AUTO_INTERVAL_MIN))
        suggested_next = max(30, min(120, suggested_next))

        now = time.time()
        result = {
            "ok":              True,
            "timestamp":       datetime.utcnow().strftime("%H:%M:%S"),
            "elapsed_s":       elapsed,
            "trigger":         trigger,
            "analysis":        parsed.get("analysis", ""),
            "thinking":        parsed.get("thinking", ""),   # Chain-of-Thought цепочка рассуждений
            "recommendations": parsed.get("recommendations", []),
            "market_verdict":  parsed.get("market_verdict", "ОСТОРОЖНО"),
            "confidence":      parsed.get("confidence", 0.5),
            "next_check_min":  suggested_next,
            "applied":         applied,
            "auto_applied":    apply,
            "snapshot":        snap,
            "provider":        provider_cfg["name"],         # какой AI использовался
            "model":           provider_cfg["model"],
        }

        with _lock:
            _last_advice       = result
            _last_auto_run_ts  = now
            _next_auto_run_ts  = now + suggested_next * 60
            _trades_since_last_run = 0
            _history.append({
                "ts":      result["timestamp"],
                "trigger": trigger,
                "verdict": result["market_verdict"],
                "applied": applied,
                "conf":    result["confidence"],
                "analysis": result["analysis"][:120],
            })
            _persist_history()

        if applied:
            logger.info(f"[Advisor] Применено {len(applied)} изм.: {'; '.join(applied[:3])}")

        # ── BrainFusion: передаём вердикт LLM в единый мозг ─────────────
        try:
            _bf.update_advisor(
                verdict=parsed.get("market_verdict", "ОСТОРОЖНО"),
                confidence=float(parsed.get("confidence", 0.5)),
                regime=snap.get("ai", {}).get("regime", {}).get("name", "UNKNOWN")
                       if snap else "UNKNOWN",
                advice=parsed.get("analysis", "")[:200],
                next_check_min=suggested_next,
            )
        except Exception as _bfe:
            pass

        return result

    except Exception as ex:
        ex_str = str(ex)
        logger.error(f"[Advisor] ошибка: {ex_str}")
        _record_rate_limit(ex_str)
        # 413 = промпт слишком большой → повтор с компактным снапшотом
        if "413" in ex_str or "request entity too large" in ex_str.lower():
            try:
                logger.warning(f"[Advisor] 413 detected ({provider_cfg['name']}) — повтор с урезанным снапшотом…")
                lite = _compact_advisor_snapshot(_build_snapshot(user_message))
                lite.pop("dex", None)
                lite_str = json.dumps(lite, ensure_ascii=False, separators=(",", ":"))
                lite_resp = client.chat.completions.create(
                    model=provider_cfg["model"],
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": f"Состояние:\n```json\n{lite_str}\n```"},
                    ],
                    temperature=provider_cfg.get("temperature", 0.25),
                    max_tokens=provider_cfg.get("max_tokens", 1200),
                )
                raw_lite    = lite_resp.choices[0].message.content or ""
                parsed_lite = _parse_response(raw_lite)
                logger.info(f"[Advisor] 413 retry OK ({provider_cfg['name']})")
                return {
                    "ok":             True,
                    "timestamp":      datetime.utcnow().strftime("%H:%M:%S"),
                    "elapsed_s":      0,
                    "trigger":        trigger,
                    "analysis":       parsed_lite.get("analysis", ""),
                    "thinking":       parsed_lite.get("thinking", ""),
                    "recommendations": parsed_lite.get("recommendations", []),
                    "market_verdict": parsed_lite.get("market_verdict", "ОСТОРОЖНО"),
                    "confidence":     parsed_lite.get("confidence", 0.5),
                    "next_check_min": AUTO_INTERVAL_MIN,
                    "applied":        [],
                    "auto_applied":   apply,
                    "snapshot":       lite,
                    "provider":       provider_cfg["name"],
                    "model":          provider_cfg["model"],
                }
            except Exception as ex2:
                logger.error(f"[Advisor] 413 retry failed: {ex2}")
        return {"ok": False, "error": ex_str}
    finally:
        with _lock:
            _running = False


def _record_rate_limit(err_text: str) -> None:
    """Парсит текст ошибки Groq (429 rate_limit_exceeded) и сохраняет лимит/сброс."""
    global _rate_limit
    if "rate_limit" not in err_text and "429" not in err_text:
        # Не лимит — если предыдущая ошибка лимита устарела (>1ч), не трогаем её.
        return
    try:
        limit_m  = re.search(r"Limit\s+(\d+)", err_text)
        used_m   = re.search(r"Used\s+(\d+)", err_text)
        req_m    = re.search(r"Requested\s+(\d+)", err_text)
        wait_m   = re.search(r"try again in\s+([\dhms.]+)", err_text)
        reset_s  = 0.0
        if wait_m:
            parts = re.findall(r"([\d.]+)([hms])", wait_m.group(1))
            for val, unit in parts:
                val = float(val)
                reset_s += val * (3600 if unit == "h" else 60 if unit == "m" else 1)
        with _lock:
            _rate_limit = {
                "limited":     True,
                "limit":       int(limit_m.group(1)) if limit_m else None,
                "used":        int(used_m.group(1))  if used_m  else None,
                "requested":   int(req_m.group(1))   if req_m   else None,
                "reset_at_ts": time.time() + reset_s if reset_s else None,
                "detected_ts": time.time(),
                "raw":         err_text[:300],
            }
    except Exception:
        pass


def _rate_limit_status() -> Optional[dict]:
    """Возвращает инфо о лимите Groq, сбрасывая её, если время ожидания уже прошло."""
    with _lock:
        rl = _rate_limit
    if not rl:
        return None
    reset_ts = rl.get("reset_at_ts")
    if reset_ts and time.time() >= reset_ts:
        return None  # лимит должен был уже сброситься
    out = dict(rl)
    out["reset_in_sec"] = max(0, int(reset_ts - time.time())) if reset_ts else None
    return out


# ──────────────────────────────────────────────────────────────────────────
# Уведомление о закрытой сделке (вызывается из trader.py)
# ──────────────────────────────────────────────────────────────────────────
def notify_trade_closed(pnl: float = 0.0, trade_data: dict = None):
    """Вызывается при каждом закрытии сделки. Сохраняет данные и триггерит советника."""
    global _trades_since_last_run, _last_trade_data, _session_stats
    outcome = "win" if pnl >= 0 else "loss"

    # ── Сохраняем данные последней сделки ────────────────────────────────────
    # ── Атомарно обновляем состояние под локом ────────────────────────────────
    with _lock:
        if trade_data:
            _last_trade_data = {
                "pnl_ton":      round(float(pnl), 4),
                "pnl_pct":      trade_data.get("pnl_pct"),
                "stake_ton":    trade_data.get("stake_ton"),
                "exit_price":   trade_data.get("exit_price"),
                "close_reason": trade_data.get("close_reason", "?"),
                "outcome":      trade_data.get("outcome", outcome),
                "duration_min": trade_data.get("duration_min"),
                "exit_ai_conf": trade_data.get("exit_ai_confidence") or trade_data.get("ai_conf"),
                "exit_regime":  trade_data.get("exit_regime") or trade_data.get("regime"),
                "strategy":     trade_data.get("strategy", "DCA" if trade_data.get("dca_entries") else "AI"),
                "dca_entries":  trade_data.get("dca_entries"),
            }
        else:
            _last_trade_data = {"pnl_ton": round(float(pnl), 4), "outcome": outcome}

        _session_stats["profit_ton"] += pnl
        _session_stats["trades"]     += 1
        if pnl >= 0:
            _session_stats["wins"]   += 1
            if pnl > _session_stats["peak_win_ton"]:
                _session_stats["peak_win_ton"] = round(pnl, 4)
        else:
            _session_stats["losses"] += 1

        _trades_since_last_run += 1
        should_run = (
            _auto_apply
            and not _running
            and _trades_since_last_run >= AUTO_TRADES_TRIGGER
            and bool(_effective_key())
        )
        _session_profit_snap = _session_stats["profit_ton"]

    if should_run:
        logger.info(f"[Advisor] 🔔 Триггер сделки: {outcome.upper()} PNL={pnl:+.4f} TON "
                    f"| сессия: {_session_profit_snap:+.4f} TON → авто-запуск")
        _run_in_bg(trigger=f"trade#{_trades_since_last_run}_{outcome}")


# ──────────────────────────────────────────────────────────────────────────
# Фоновый запуск (не блокирует торговый поток)
# ──────────────────────────────────────────────────────────────────────────
def _run_in_bg(trigger: str = "timer"):
    def _worker():
        try:
            run_advisor(auto_apply=True, trigger=trigger)
        except Exception as ex:
            logger.error(f"[Advisor] bg worker: {ex}")
    t = threading.Thread(target=_worker, daemon=True,
                         name=f"advisor-{trigger[:16]}")
    t.start()


# ──────────────────────────────────────────────────────────────────────────
# Фоновый поток таймера
# ──────────────────────────────────────────────────────────────────────────
def _timer_loop():
    global _next_auto_run_ts
    # Первый запуск через 2 минуты после старта (дать боту время загрузиться)
    with _lock:
        _next_auto_run_ts = time.time() + 120
    logger.info(f"[Advisor] ⏱ Таймер запущен, первый авто-анализ через 2 мин")
    while not _stop_event.is_set():
        _stop_event.wait(timeout=10)   # проверяем каждые 10 сек (было 30)
        if _stop_event.is_set():
            break
        with _lock:
            should = (
                _auto_apply
                and not _running
                and bool(_effective_key())
                and time.time() >= _next_auto_run_ts
            )
        if should:
            _run_in_bg(trigger="timer")
            with _lock:
                _next_auto_run_ts = time.time() + AUTO_INTERVAL_MIN * 60


def start_background():
    """Запускает фоновый поток таймера. Вызывается из app.py."""
    global _bg_thread
    if _bg_thread and _bg_thread.is_alive():
        return
    _stop_event.clear()
    _bg_thread = threading.Thread(target=_timer_loop, daemon=True,
                                  name="advisor-timer")
    _bg_thread.start()
    logger.info("[Advisor] 🚀 Фоновый поток автономии запущен")


# ──────────────────────────────────────────────────────────────────────────
# Публичное API
# ──────────────────────────────────────────────────────────────────────────
def _current_strategy_toggles() -> dict:
    try:
        from config import Config
    except Exception:
        return {}
    return {
        key: bool(getattr(Config, attr, False))
        for key, attr in _TOGGLE_ATTR.items()
    }


def _current_size_mult() -> float:
    try:
        from config import Config
        return round(float(getattr(Config, "AI_SIZE_MULT", 1.0)), 3)
    except Exception:
        return 1.0


def get_status() -> dict:
    with _lock:
        now = time.time()
        nxt = _next_auto_run_ts
        # Определяем активный провайдер для отображения
        active_pid, active_pcfg = _get_best_provider()
        # Список доступных провайдеров (с ключами)
        available = {
            pid: {
                "name":    cfg["name"],
                "model":   cfg["model"],
                "has_key": bool(_read_provider_key(pid)),
                "priority": cfg["priority"],
                "selected": (pid == _selected_provider),
            }
            for pid, cfg in PROVIDER_CONFIGS.items()
        }
        return {
            "enabled":           bool(_effective_key()),
            "running":           _running,
            "auto_apply":        _auto_apply,
            "last_advice":       _last_advice,
            "history":           list(_history),
            "adaptation_log":    list(_adaptation_log)[-15:],
            "total_adaptations": _total_adaptations,
            "trades_since_last": _trades_since_last_run,
            "trades_trigger":    AUTO_TRADES_TRIGGER,
            "interval_min":      AUTO_INTERVAL_MIN,
            "ai_size_mult":      _current_size_mult(),
            "next_run_in_sec":   max(0, int(nxt - now)) if nxt > 0 else 0,
            "last_run_ts":       _last_auto_run_ts,
            "model":             active_pcfg["model"],
            "provider":          active_pcfg["name"],
            "provider_id":       active_pid,
            "selected_provider": _selected_provider,
            "providers":         available,
            "strategy_toggles":  _current_strategy_toggles(),
            "strategy_labels":   STRATEGY_TOGGLES,
            "rate_limit":        _rate_limit_status(),
        }


def toggle_auto_apply() -> bool:
    global _auto_apply
    with _lock:
        _auto_apply = not _auto_apply
        state = _auto_apply
    logger.info(f"[Advisor] auto_apply → {state}")
    return state


def set_config(interval_min: int = None, trades_trigger: int = None):
    global AUTO_INTERVAL_MIN, AUTO_TRADES_TRIGGER
    if interval_min is not None:
        AUTO_INTERVAL_MIN = max(30, min(360, int(interval_min)))
    if trades_trigger is not None:
        AUTO_TRADES_TRIGGER = max(1, min(20, int(trades_trigger)))
    try:
        from settings_store import update_section
        update_section("advisor", {
            "interval_min":   AUTO_INTERVAL_MIN,
            "trades_trigger": AUTO_TRADES_TRIGGER,
        })
    except Exception as ex:
        logger.warning(f"[Advisor] не удалось сохранить конфиг в settings_store: {ex}")
    with _lock:
        global _next_auto_run_ts
        _next_auto_run_ts = time.time() + AUTO_INTERVAL_MIN * 60
    logger.info(f"[Advisor] ⏱ Интервал сеанса → {AUTO_INTERVAL_MIN} мин, триггер сделок → {AUTO_TRADES_TRIGGER}")
    return {"interval_min": AUTO_INTERVAL_MIN, "trades_trigger": AUTO_TRADES_TRIGGER}


def reload_key(key: str = None, provider: str = "groq"):
    """Обновить ключ провайдера и сохранить в файл.
    Для обратной совместимости: provider='groq', key=None → читать из env/файла."""
    global GROQ_API_KEY
    if key is not None:
        k = key.strip()
        _write_provider_key(provider, k)
        if provider == "groq":
            GROQ_API_KEY = k
    else:
        # Обратная совместимость — обновляем Groq
        GROQ_API_KEY = os.getenv("GROQ_API_KEY", "") or _read_key_file()
        if GROQ_API_KEY:
            _provider_keys["groq"] = GROQ_API_KEY
    return bool(_effective_key())


def set_provider(provider_id: str):
    """Устанавливает предпочтительный AI-провайдер. None = авто."""
    global _selected_provider
    if provider_id and provider_id not in PROVIDER_CONFIGS:
        return {"ok": False, "error": f"Неизвестный провайдер: {provider_id}"}
    _selected_provider = provider_id if provider_id else None
    try:
        from settings_store import update_section
        update_section("advisor", {"selected_provider": _selected_provider or ""})
    except Exception as ex:
        logger.warning(f"[Advisor] set_provider save: {ex}")
    pid, pcfg = _get_best_provider()
    logger.info(f"[Advisor] 🤖 Активный провайдер: {pcfg['name']}")
    return {"ok": True, "active_provider": pcfg["name"], "model": pcfg["model"]}


def get_providers() -> dict:
    """Возвращает список всех провайдеров с их статусом."""
    pid, pcfg = _get_best_provider()
    return {
        "active_id":   pid,
        "active_name": pcfg["name"],
        "selected":    _selected_provider,
        "providers": {
            p: {
                "name":     c["name"],
                "model":    c["model"],
                "has_key":  bool(_read_provider_key(p)),
                "priority": c["priority"],
                "is_active": (p == pid),
            }
            for p, c in sorted(PROVIDER_CONFIGS.items(), key=lambda x: x[1]["priority"])
        }
    }


def get_adaptation_log() -> list:
    return list(_adaptation_log)
