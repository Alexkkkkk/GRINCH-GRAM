"""
BrainFusion v1 — единый интеллект торгового бота GRINCH/TON.

Объединяет три источника сигналов в один консенсусный «организм»:
  1. AI-движок (ML ансамбль: RF/ET/GB/HGB/XGB/MLP)
  2. Технический анализ (RSI/EMA/MACD/ATR/BB)
  3. LLM-советник (Groq — рыночный контекст + долгосрочная стратегия)

Возможности:
  • Консенсусный сигнал с весовым объединением источников
  • Детектор скальпинг-окна (RANGING/SQUEEZE режим → быстрые сделки 5-8%)
  • Детектор памп-ускорителя (PUMP/BREAKOUT → увеличенная позиция)
  • «Пропуск подтверждения» — когда все три источника согласны ≥78%
  • Анализ баланса кошелька — TON + GRINCH в TON эквиваленте
  • Журнал решений с самооценкой (что сработало, что нет)

Использование:
    import brain_fusion as bf

    # В trader.py после ai.analyze():
    bf.update_ai(ai_result)
    fusion = bf.get_fusion_signal()

    # В ai_advisor.py после LLM-ответа:
    bf.update_advisor(verdict="ПОКУПАТЬ", confidence=0.72, regime="UPTREND")

    # Проверка на скальп:
    if bf.is_scalp_window():
        use scalp params (TP=6%, trail=3%)

    # Пропустить ожидание подтверждения:
    if bf.should_skip_confirmation(ai_conf):
        confirm_needed = 0
"""
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("brain_fusion")

# ─── Параметры ──────────────────────────────────────────────────────────────
# Вес каждого источника в консенсусном сигнале
_W_AI       = 0.70   # ML ансамбль — наибольший вес (быстро, объективно)
_W_TA       = 0.20   # Технический анализ — традиционные фильтры
_W_ADVISOR  = 0.10   # LLM советник — контекст и стратегия

# Порог консенсуса для «пропуска подтверждения»
FUSION_SKIP_CONFIRM_CONF = 68.0   # все согласны ≥68% → входим сразу

# Режимы рынка, в которых включается скальпинг
SCALP_REGIMES = {"RANGING", "SQUEEZE", "TRANSITION"}

# Режимы рынка, в которых включается памп-ускоритель
PUMP_REGIMES  = {"UPTREND", "BREAKOUT"}

# Максимальное время жизни сигнала (секунды) — устаревшие игнорируются
AI_SIGNAL_TTL     = 60    # 1 минута
ADVISOR_SIGNAL_TTL = 600  # 10 минут (LLM работает реже)

# ─── Структуры данных ────────────────────────────────────────────────────────

@dataclass
class AIState:
    """Последний результат ML-движка."""
    signal:     str   = "HOLD"    # BUY / SELL / HOLD
    confidence: float = 0.0       # 0-100%
    prob_up:    float = 0.0
    prob_down:  float = 0.0
    regime:     str   = "UNKNOWN"
    atr_pct:    float = 0.0
    pump_score: float = 0.0       # 0-1: вероятность накопления
    momentum:   str   = "CALM"    # EXPLOSIVE / SURGE / BUILDING / CALM
    breakout:   str   = "FLAT"    # BREAKOUT / RUNAWAY / FLAT
    ev_ok:      bool  = True      # EV-фильтр пройден
    rsi:        float = 50.0
    updated_at: float = 0.0

@dataclass
class AdvisorState:
    """Последний результат LLM-советника."""
    verdict:    str   = "ОСТОРОЖНО"  # ПОКУПАТЬ / ПРОДАВАТЬ / ОСТОРОЖНО
    confidence: float = 0.5          # 0-1
    regime:     str   = "UNKNOWN"
    advice:     str   = ""
    next_check: int   = 10           # мин до следующей проверки
    updated_at: float = 0.0

@dataclass
class TAState:
    """Последний результат технического анализа."""
    signal:     str   = "HOLD"   # BUY / SELL / HOLD
    entry_quality: str = "C"     # A / B / C
    entry_score:   int = 0
    rsi:           float = 50.0
    trend_ok:      bool  = True   # EMA/тренд в нашу сторону
    updated_at:    float = 0.0

@dataclass
class WalletState:
    """Состояние кошелька для анализа баланса."""
    ton_balance:       float = 0.0
    grinch_balance:    float = 0.0
    grinch_price_ton:  float = 0.0
    total_value_ton:   float = 0.0   # TON + GRINCH×price
    open_pnl_pct:      float = 0.0   # текущий нереализованный PnL %
    has_position:      bool  = False
    updated_at:        float = 0.0

@dataclass
class FusionSignal:
    """Консенсусный сигнал от всех источников."""
    action:             str   = "HOLD"    # BUY / SELL / HOLD
    consensus_conf:     float = 0.0       # взвешенная уверенность 0-100%
    skip_confirmation:  bool  = False     # входить сразу, без ожидания тика
    is_scalp_window:    bool  = False     # скальпинг-режим (маленький TP)
    is_pump_window:     bool  = False     # памп-ускоритель (увеличенная позиция)
    scalp_tp_pct:       float = 6.0       # TP для скальпа (gross %)
    scalp_trail_pct:    float = 3.5       # Trail для скальпа (%)
    position_boost:     float = 1.0       # множитель размера позиции
    regime:             str   = "UNKNOWN"
    sources:            dict  = field(default_factory=dict)  # вклад каждого источника
    reasoning:          str   = ""        # краткое объяснение для лога

# ─── Класс BrainFusion ──────────────────────────────────────────────────────

class BrainFusion:
    """
    Центральный мозг торгового бота.

    Аккумулирует сигналы от AI-движка, ТА и LLM-советника,
    вычисляет взвешенный консенсус и принимает решения о:
      • Качестве точки входа
      • Режиме торговли (скальп / нормальный / памп)
      • Допустимости пропуска подтверждения
      • Усилении позиции
    """

    def __init__(self):
        self._lock      = threading.RLock()  # RLock — поддерживает вложенные захваты
        self._ai        = AIState()
        self._advisor   = AdvisorState()
        self._ta        = TAState()
        self._wallet    = WalletState()

        # Журнал решений для самооценки
        self._decision_log: list[dict] = []   # последние 50 решений
        # Статистика: сколько раз fusion-сигнал совпал с результатом
        self._fusion_wins:   int = 0
        self._fusion_total:  int = 0

        # ── Динамические веса: точность каждого источника за последние сделки ─
        # Позволяет автоматически усиливать лучший источник сигнала и
        # ослаблять худший на основе реальных торговых результатов.
        self._ai_wins:   int = 0    # ML-движок: сколько раз его сигнал совпал с прибылью
        self._ai_total:  int = 0    # ML-движок: всего оценок
        self._ta_wins:   int = 0    # TA: сколько раз его сигнал совпал с прибылью
        self._ta_total:  int = 0    # TA: всего оценок
        self._adv_wins:  int = 0    # LLM советник: побед
        self._adv_total: int = 0    # LLM советник: всего оценок

        # Состояние скальп-серии (сколько скальпов подряд)
        self._scalp_streak:  int = 0
        self._scalp_profit:  float = 0.0  # накопленная прибыль скальп-серии (TON)

        # Восстанавливаем самообучение (fusion_wins/total, скальп-серия, журнал
        # решений) из БД — раньше эта статистика полностью терялась при каждом
        # рестарте процесса и "мозг" каждый раз заново набирал точность с нуля.
        self._load_state()

    # ── Персистентность самообучения (bot_ai_state) ─────────────────────────
    def _load_state(self):
        try:
            import db_store
            if not db_store.is_available():
                return
            state = db_store.ai_state_get("brain_fusion") or {}
            if not isinstance(state, dict) or not state:
                return
            self._fusion_wins  = int(state.get("fusion_wins", 0) or 0)
            self._fusion_total = int(state.get("fusion_total", 0) or 0)
            self._scalp_streak = int(state.get("scalp_streak", 0) or 0)
            self._scalp_profit = float(state.get("scalp_profit", 0.0) or 0.0)
            # Динамические веса: точность каждого источника
            self._ai_wins   = int(state.get("ai_wins",   0) or 0)
            self._ai_total  = int(state.get("ai_total",  0) or 0)
            self._ta_wins   = int(state.get("ta_wins",   0) or 0)
            self._ta_total  = int(state.get("ta_total",  0) or 0)
            self._adv_wins  = int(state.get("adv_wins",  0) or 0)
            self._adv_total = int(state.get("adv_total", 0) or 0)
            log_data = state.get("decision_log")
            if isinstance(log_data, list):
                self._decision_log = log_data[-50:]
            log.info(
                f"[BrainFusion] Самообучение восстановлено из БД: "
                f"точность={self._fusion_wins}/{self._fusion_total}, "
                f"серия скальпов={self._scalp_streak} | "
                f"точность источников: AI={self._ai_wins}/{self._ai_total} "
                f"TA={self._ta_wins}/{self._ta_total} "
                f"LLM={self._adv_wins}/{self._adv_total}"
            )
        except Exception as e:
            log.warning(f"[BrainFusion] _load_state ошибка: {e}")

    def _save_state(self):
        """Best-effort сохранение самообучения. Никогда не должно бросать —
        вызывается из горячего пути on_trade_closed/log_decision."""
        try:
            import db_store
            if not db_store.is_available():
                return
            db_store.ai_state_set("brain_fusion", {
                "fusion_wins":   self._fusion_wins,
                "fusion_total":  self._fusion_total,
                "scalp_streak":  self._scalp_streak,
                "scalp_profit":  self._scalp_profit,
                "decision_log":  self._decision_log[-50:],
                # Динамические веса — точность источников
                "ai_wins":   self._ai_wins,
                "ai_total":  self._ai_total,
                "ta_wins":   self._ta_wins,
                "ta_total":  self._ta_total,
                "adv_wins":  self._adv_wins,
                "adv_total": self._adv_total,
            })
        except Exception as e:
            log.warning(f"[BrainFusion] _save_state ошибка: {e}")

    # ── Обновление состояний ─────────────────────────────────────────────────

    def update_ai(self, ai_result: dict):
        """Вызывать после ai_engine.analyze() в каждом тике."""
        with self._lock:
            ai = self._ai
            ai.signal     = ai_result.get("ai_signal", "HOLD")
            ai.confidence = float(ai_result.get("confidence", 0))
            ai.prob_up    = float(ai_result.get("prob_up", 0))
            ai.prob_down  = float(ai_result.get("prob_down", 0))
            ai.ev_ok      = ai_result.get("ev_ok", True)
            ai.updated_at = time.time()

            regime_dict   = ai_result.get("regime") or {}
            ai.regime     = regime_dict.get("name", "UNKNOWN")
            ai.atr_pct    = float(regime_dict.get("atr_pct", 0))

            pump_dict     = ai_result.get("pump_detector") or {}
            ai.pump_score = float(pump_dict.get("score", 0)) / 100.0  # 0-1

            mom_dict      = ai_result.get("momentum") or {}
            ai.momentum   = (mom_dict.get("signal") or "CALM").upper()

            bo_dict       = ai_result.get("breakout") or {}
            ai.breakout   = (bo_dict.get("signal") or "FLAT").upper()

            # RSI из анализа
            ai.rsi = float(ai_result.get("rsi", 50))

    def update_ta(self, ta_result: dict):
        """Вызывать после analyze() из strategy.py в каждом тике."""
        with self._lock:
            ta = self._ta
            ta.signal        = ta_result.get("signal", "HOLD")
            ta.entry_quality = ta_result.get("entry_quality", "C")
            ta.entry_score   = int(ta_result.get("entry_score", 0))
            ta.rsi           = float(ta_result.get("rsi", 50))
            # Тренд OK если EMA разворот не против нас
            ta.trend_ok      = ta_result.get("signal", "HOLD") != "SELL"
            ta.updated_at    = time.time()

    def update_advisor(self, verdict: str, confidence: float,
                       regime: str = "UNKNOWN", advice: str = "",
                       next_check_min: int = 10):
        """Вызывать после LLM-советника получил ответ."""
        with self._lock:
            adv = self._advisor
            adv.verdict    = verdict.upper() if verdict else "ОСТОРОЖНО"
            adv.confidence = max(0.0, min(1.0, confidence))
            adv.regime     = regime
            adv.advice     = advice[:200]  # обрезаем для памяти
            adv.next_check = next_check_min
            adv.updated_at = time.time()

    def update_wallet(self, ton_bal: float, grinch_bal: float,
                      grinch_price_ton: float, open_pnl_pct: float = 0.0):
        """Обновляет состояние кошелька — вызывать раз в тик."""
        with self._lock:
            w = self._wallet
            w.ton_balance      = ton_bal
            w.grinch_balance   = grinch_bal
            w.grinch_price_ton = grinch_price_ton
            w.total_value_ton  = ton_bal + grinch_bal * grinch_price_ton
            w.open_pnl_pct     = open_pnl_pct
            w.has_position     = grinch_bal > 100  # > 100 GRINCH = открытая позиция
            w.updated_at       = time.time()

    # ── Основной метод: консенсусный сигнал ─────────────────────────────────

    def get_fusion_signal(self) -> FusionSignal:
        """
        Вычисляет взвешенный консенсусный сигнал от всех источников.
        Потокобезопасно.
        """
        with self._lock:
            return self._compute_fusion()

    def _compute_fusion(self) -> FusionSignal:
        now = time.time()
        fs  = FusionSignal()

        # ── Проверяем свежесть сигналов ──────────────────────────────────
        ai_fresh  = (now - self._ai.updated_at)  < AI_SIGNAL_TTL
        adv_fresh = (now - self._advisor.updated_at) < ADVISOR_SIGNAL_TTL
        ta_fresh  = (now - self._ta.updated_at)  < AI_SIGNAL_TTL

        # ── Переводим сигналы в числа (+1=BUY, 0=HOLD, -1=SELL) ─────────
        def _sig_to_num(sig: str) -> float:
            s = (sig or "HOLD").upper()
            if s in ("BUY",  "ПОКУПАТЬ"):  return +1.0
            if s in ("SELL", "ПРОДАВАТЬ"): return -1.0
            return 0.0

        ai_num  = _sig_to_num(self._ai.signal)     if ai_fresh  else 0.0
        ta_num  = _sig_to_num(self._ta.signal)     if ta_fresh  else 0.0
        adv_num = _sig_to_num(self._advisor.verdict) if adv_fresh else 0.0

        # Нормализованная уверенность (0-1) для взвешивания
        ai_conf_n  = self._ai.confidence / 100.0   if ai_fresh  else 0.5
        adv_conf_n = self._advisor.confidence       if adv_fresh else 0.5
        ta_conf_n  = min(1.0, (self._ta.entry_score or 0) / 10.0) if ta_fresh else 0.5

        # ── Динамическая корректировка базовых весов по точности источника ──
        # После ≥5 оценок каждый источник получает поправочный коэффициент:
        # точность 60% → +25% к весу; 40% → -25%; при <5 оценках — без изменений.
        # Ни один источник не опускается ниже 30% от своего базового веса.
        def _dyn_base_w(base_w: float, wins: int, total: int) -> float:
            if total < 5:
                return base_w
            accuracy = wins / total          # [0..1]
            factor   = 1.0 + (accuracy - 0.5) * 0.5   # [0.75..1.25]
            return max(base_w * 0.30, min(base_w * 1.50, base_w * factor))

        dyn_w_ai  = _dyn_base_w(_W_AI,      self._ai_wins,  self._ai_total)
        dyn_w_ta  = _dyn_base_w(_W_TA,      self._ta_wins,  self._ta_total)
        dyn_w_adv = _dyn_base_w(_W_ADVISOR, self._adv_wins, self._adv_total)

        # ── Взвешенный консенсус ──────────────────────────────────────────
        w_ai  = dyn_w_ai  * ai_conf_n   if ai_fresh  else 0.0
        w_ta  = dyn_w_ta  * ta_conf_n   if ta_fresh  else 0.0
        w_adv = dyn_w_adv * adv_conf_n  if adv_fresh else 0.0
        w_sum = w_ai + w_ta + w_adv

        if w_sum < 0.01:
            # Нет данных — нейтральный сигнал
            fs.action = "HOLD"
            fs.consensus_conf = 0.0
            fs.reasoning = "Нет актуальных сигналов"
            return fs

        weighted = (w_ai * ai_num + w_ta * ta_num + w_adv * adv_num) / w_sum
        # weighted: от -1 (сильный SELL) до +1 (сильный BUY)

        if weighted >= 0.15:
            fs.action = "BUY"
        elif weighted <= -0.20:
            fs.action = "SELL"
        else:
            fs.action = "HOLD"

        # Консенсусная уверенность в процентах
        fs.consensus_conf = abs(weighted) * 100.0

        # ── Режим рынка ───────────────────────────────────────────────────
        regime = self._ai.regime if ai_fresh else (
            self._advisor.regime if adv_fresh else "UNKNOWN"
        )
        fs.regime = regime

        # ── Читаем параметры из Config (единственный источник истины) ───────
        try:
            from config import Config as _Cfg
            _scalp_min_conf  = float(_Cfg.SCALP_MIN_AI_CONF)
            _scalp_max_atr   = float(_Cfg.SCALP_MAX_ATR_PCT)
            _scalp_tp        = float(_Cfg.SCALP_TP_PCT)
            _scalp_trail     = float(_Cfg.SCALP_TRAIL_PCT)
            _pump_boost_max  = float(_Cfg.FUSION_PUMP_BOOST_MAX)
            _skip_thresh     = float(_Cfg.FUSION_SKIP_CONFIRM_CONF)
        except Exception:
            _scalp_min_conf  = 55.0
            _scalp_max_atr   = 5.5
            _scalp_tp        = 6.0
            _scalp_trail     = 3.0
            _pump_boost_max  = 1.8
            _skip_thresh     = FUSION_SKIP_CONFIRM_CONF

        # ── Детектор скальп-окна ──────────────────────────────────────────
        # Скальп: RANGING/SQUEEZE режим И AI BUY И уверенность ≥ Config.SCALP_MIN_AI_CONF
        is_scalp = (
            regime in SCALP_REGIMES
            and ai_fresh
            and self._ai.signal == "BUY"
            and self._ai.confidence >= _scalp_min_conf
            and self._ai.atr_pct < _scalp_max_atr   # тихий рынок
        )
        fs.is_scalp_window = is_scalp
        if is_scalp:
            # ATR-адаптивные TP/trail, но не ниже значений Config
            atr = max(1.5, self._ai.atr_pct)
            fs.scalp_tp_pct    = max(_scalp_tp,    atr * 2.2)  # мин = Config.SCALP_TP_PCT
            fs.scalp_trail_pct = max(_scalp_trail, atr * 1.2)  # мин = Config.SCALP_TRAIL_PCT

        # ── Детектор памп-ускорителя ──────────────────────────────────────
        is_pump = (
            regime in PUMP_REGIMES
            or self._ai.momentum in ("EXPLOSIVE", "SURGE")
            or self._ai.breakout in ("BREAKOUT", "RUNAWAY")
            or self._ai.pump_score >= 0.7
        )
        fs.is_pump_window = is_pump and ai_fresh and self._ai.signal == "BUY"
        if fs.is_pump_window:
            # Ускоритель позиции при памп-сигнале — ограничен Config.FUSION_PUMP_BOOST_MAX
            pump_mult = 1.0
            if self._ai.momentum == "EXPLOSIVE":  pump_mult = 1.6
            elif self._ai.momentum == "SURGE":    pump_mult = 1.35
            elif regime == "BREAKOUT":             pump_mult = 1.25
            elif regime == "UPTREND":              pump_mult = 1.15
            fs.position_boost = min(_pump_boost_max, pump_mult)

        # ── Пропуск подтверждения ─────────────────────────────────────────

        # Требуем ПОЛНОГО консенсуса: AI+TA+LLM все согласны
        # (не только AI — чтобы не давать преждевременные BUY)
        ai_agrees  = not ai_fresh  or self._ai.signal == fs.action
        ta_agrees  = not ta_fresh  or self._ta.signal in (fs.action, "HOLD")
        adv_agrees = (not adv_fresh
                      or adv_num * (1 if fs.action == "BUY" else -1 if fs.action == "SELL" else 0) >= 0)
        all_agree = ai_agrees and ta_agrees and adv_agrees

        # Для пропуска нужно: AI свежий + уверенность ≥ порога + все согласны + EV OK
        fs.skip_confirmation = (
            ai_fresh
            and self._ai.confidence >= _skip_thresh
            and all_agree
            and self._ai.ev_ok
            and fs.action != "HOLD"   # не пропускаем для HOLD-сигналов
        )

        # ── Вклад источников (для лога) ───────────────────────────────────
        fs.sources = {
            "ai":      {"signal": self._ai.signal,      "conf": round(self._ai.confidence, 1),   "fresh": ai_fresh},
            "ta":      {"signal": self._ta.signal,      "score": self._ta.entry_score,            "fresh": ta_fresh},
            "advisor": {"signal": self._advisor.verdict,"conf": round(self._advisor.confidence*100,1), "fresh": adv_fresh},
        }

        # ── Рассуждение для лога ──────────────────────────────────────────
        parts = []
        if ai_fresh:  parts.append(f"AI={self._ai.signal}({self._ai.confidence:.0f}%)")
        if ta_fresh:  parts.append(f"TA={self._ta.signal}(Q={self._ta.entry_quality})")
        if adv_fresh: parts.append(f"LLM={self._advisor.verdict}({self._advisor.confidence*100:.0f}%)")
        mode = []
        if is_scalp:     mode.append(f"СКАЛЬП({fs.scalp_tp_pct:.1f}%TP)")
        if is_pump:      mode.append(f"ПАМП(×{fs.position_boost:.2f})")
        if fs.skip_confirmation: mode.append("ПРОПУСК_ОЖИДАНИЯ")
        regime_tag = f"[{regime}]"
        fs.reasoning = f"{regime_tag} {' | '.join(parts)}" + (f" → {'+'.join(mode)}" if mode else "")

        return fs

    # ── Удобные методы ───────────────────────────────────────────────────────

    def is_scalp_window(self) -> bool:
        """True если сейчас хорошее скальп-окно (RANGING/SQUEEZE)."""
        return self.get_fusion_signal().is_scalp_window

    def should_skip_confirmation(self, ai_conf=0.0) -> bool:
        """
        True если все три источника (AI+TA+LLM) согласны выше порога.
        Требует ПОЛНОГО консенсуса — AI-alone недостаточно.
        """
        try:
            ai_conf = float(ai_conf or 0.0)  # защита от None/строк
        except (TypeError, ValueError):
            ai_conf = 0.0
        with self._lock:
            if not self._ai.updated_at:
                return False
            # Всегда вычисляем полный fusion-консенсус (AI+TA+LLM)
            fs = self._compute_fusion()
            return fs.skip_confirmation

    def is_bullish_consensus(self, min_conf: float = 58.0) -> bool:
        """True если большинство источников говорят BUY с уверенностью ≥ порога."""
        with self._lock:
            ai_ok  = self._ai.signal == "BUY" and self._ai.confidence >= min_conf
            adv_ok = self._advisor.verdict in ("ПОКУПАТЬ", "BUY")
            return ai_ok and (adv_ok or
                   (time.time() - self._advisor.updated_at) > ADVISOR_SIGNAL_TTL)

    def get_wallet_analysis(self) -> dict:
        """
        Анализ баланса кошелька:
        - Сколько TON доступно для торговли
        - Процент в позиции vs свободный
        - Рекомендуемый размер следующей сделки
        """
        with self._lock:
            w = self._wallet
            if not w.updated_at:
                return {}

            total = w.total_value_ton
            if total <= 0:
                return {}

            position_pct = (w.grinch_balance * w.grinch_price_ton / total * 100) \
                           if total > 0 else 0.0
            free_pct     = (w.ton_balance / total * 100) if total > 0 else 0.0

            # Рекомендуемый % от свободных TON для следующей сделки
            # Чем меньше позиция — тем агрессивнее новый вход
            if position_pct < 20:
                rec_stake_pct = 80.0   # нет позиции → полный вход
            elif position_pct < 50:
                rec_stake_pct = 60.0   # маленькая позиция → увеличиваем
            elif position_pct < 80:
                rec_stake_pct = 30.0   # большая позиция → докупаем осторожно
            else:
                rec_stake_pct = 10.0   # почти всё в позиции → минимальная докупка

            rec_stake_ton = w.ton_balance * rec_stake_pct / 100.0

            return {
                "ton_balance":    round(w.ton_balance, 3),
                "grinch_balance": round(w.grinch_balance, 0),
                "total_value_ton":round(total, 3),
                "position_pct":   round(position_pct, 1),
                "free_pct":       round(free_pct, 1),
                "open_pnl_pct":   round(w.open_pnl_pct, 2),
                "has_position":   w.has_position,
                "rec_stake_ton":  round(max(0, rec_stake_ton), 3),
                "rec_stake_pct":  rec_stake_pct,
            }

    def get_state(self) -> dict:
        """Полный снимок состояния мозга — для дашборда и LLM-советника."""
        with self._lock:
            fs = self._compute_fusion()
            return {
                "fusion": {
                    "action":           fs.action,
                    "consensus_conf":   round(fs.consensus_conf, 1),
                    "skip_confirm":     fs.skip_confirmation,
                    "scalp_window":     fs.is_scalp_window,
                    "pump_window":      fs.is_pump_window,
                    "position_boost":   round(fs.position_boost, 2),
                    "regime":           fs.regime,
                    "reasoning":        fs.reasoning,
                },
                "ai": {
                    "signal":     self._ai.signal,
                    "confidence": round(self._ai.confidence, 1),
                    "regime":     self._ai.regime,
                    "atr_pct":    round(self._ai.atr_pct, 2),
                    "pump_score": round(self._ai.pump_score, 2),
                    "momentum":   self._ai.momentum,
                    "breakout":   self._ai.breakout,
                    "ev_ok":      self._ai.ev_ok,
                    "age_sec":    round(time.time() - self._ai.updated_at, 0) if self._ai.updated_at else 999,
                },
                "advisor": {
                    "verdict":    self._advisor.verdict,
                    "confidence": round(self._advisor.confidence * 100, 1),
                    "regime":     self._advisor.regime,
                    "age_sec":    round(time.time() - self._advisor.updated_at, 0) if self._advisor.updated_at else 999,
                },
                "ta": {
                    "signal":  self._ta.signal,
                    "quality": self._ta.entry_quality,
                    "score":   self._ta.entry_score,
                    "age_sec": round(time.time() - self._ta.updated_at, 0) if self._ta.updated_at else 999,
                },
                "wallet":  self.get_wallet_analysis(),
                "scalp_streak": self._scalp_streak,
                "scalp_profit": round(self._scalp_profit, 3),
                "fusion_accuracy": (
                    round(self._fusion_wins / self._fusion_total * 100, 1)
                    if self._fusion_total > 0 else None
                ),
            }

    def on_trade_closed(self, pnl_ton: float, was_scalp: bool = False):
        """Обратная связь после закрытой сделки — самообучение мозга.
        Обновляет точность каждого источника (AI/TA/LLM) для динамических весов."""
        with self._lock:
            self._fusion_total += 1
            is_win = pnl_ton > 0
            if is_win:
                self._fusion_wins += 1

            # ── Динамические веса: записываем точность каждого источника ────────
            # Правило: BUY + прибыль = верно; SELL/HOLD + убыток тоже = верно.
            ai_sig  = self._ai.signal
            ta_sig  = self._ta.signal
            adv_sig = self._advisor.verdict

            if ai_sig in ("BUY", "SELL"):
                self._ai_total += 1
                if (ai_sig == "BUY" and is_win) or (ai_sig == "SELL" and not is_win):
                    self._ai_wins += 1

            if ta_sig in ("BUY", "SELL"):
                self._ta_total += 1
                if (ta_sig == "BUY" and is_win) or (ta_sig == "SELL" and not is_win):
                    self._ta_wins += 1

            adv_is_buy = adv_sig in ("ПОКУПАТЬ", "BUY")
            adv_is_sell = adv_sig in ("ПРОДАВАТЬ", "SELL")
            if adv_is_buy or adv_is_sell:
                self._adv_total += 1
                if (adv_is_buy and is_win) or (adv_is_sell and not is_win):
                    self._adv_wins += 1

            if was_scalp:
                if is_win:
                    self._scalp_streak += 1
                    self._scalp_profit += pnl_ton
                else:
                    self._scalp_streak = 0

            log.info(
                f"[BrainFusion] Сделка закрыта: PnL={pnl_ton:+.3f} TON | "
                f"скальп={was_scalp} | серия={self._scalp_streak} | "
                f"fusion={self._fusion_wins}/{self._fusion_total} | "
                f"AI={self._ai_wins}/{self._ai_total} "
                f"TA={self._ta_wins}/{self._ta_total} "
                f"LLM={self._adv_wins}/{self._adv_total}"
            )
            self._save_state()

    def log_decision(self, decision: dict):
        """Сохраняет решение в кольцевой буфер."""
        with self._lock:
            self._decision_log.append({**decision, "t": time.time()})
            if len(self._decision_log) > 50:
                self._decision_log.pop(0)
        self._save_state()

    def get_decision_log(self) -> list:
        with self._lock:
            return list(self._decision_log)


# ─── Глобальный синглтон ─────────────────────────────────────────────────────

brain = BrainFusion()

# ── Удобные функции-прокси (чтобы не импортировать класс) ────────────────────

def update_ai(ai_result: dict):
    """Обновить AI-состояние мозга."""
    brain.update_ai(ai_result)

def update_ta(ta_result: dict):
    """Обновить TA-состояние мозга."""
    brain.update_ta(ta_result)

def update_advisor(verdict: str, confidence: float, regime: str = "UNKNOWN",
                   advice: str = "", next_check_min: int = 10):
    """Обновить состояние LLM-советника."""
    brain.update_advisor(verdict, confidence, regime, advice, next_check_min)

def update_wallet(ton_bal: float, grinch_bal: float,
                  grinch_price_ton: float, open_pnl_pct: float = 0.0):
    """Обновить баланс кошелька."""
    brain.update_wallet(ton_bal, grinch_bal, grinch_price_ton, open_pnl_pct)

def get_fusion_signal() -> FusionSignal:
    """Получить текущий консенсусный сигнал."""
    return brain.get_fusion_signal()

def is_scalp_window() -> bool:
    """True если сейчас режим скальпинга."""
    return brain.is_scalp_window()

def should_skip_confirmation(ai_conf: float = 0.0) -> bool:
    """True если нужно входить немедленно."""
    return brain.should_skip_confirmation(ai_conf)

def is_bullish_consensus(min_conf: float = 58.0) -> bool:
    """True если консенсус бычий."""
    return brain.is_bullish_consensus(min_conf)

def get_wallet_analysis() -> dict:
    """Анализ баланса кошелька."""
    return brain.get_wallet_analysis()

def get_state() -> dict:
    """Полный снимок состояния для дашборда/советника."""
    return brain.get_state()

def on_trade_closed(pnl_ton: float, was_scalp: bool = False):
    """Обратная связь после закрытой сделки."""
    brain.on_trade_closed(pnl_ton, was_scalp)

log.info("[BrainFusion v1] Единый мозг инициализирован ✓")
