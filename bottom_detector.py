"""
BottomDetector — умный детектор рыночного дна для ALL-IN покупки.

Набирает очки (0-100) по 7 независимым сигналам:
  1. RSI перепродан          — вес 25
  2. StochRSI у дна          — вес 10
  3. Bollinger Band (нижняя) — вес 15
  4. Объём (паника/выброс)   — вес 15
  5. MACD разворот вверх     — вес 15
  6. Williams %R экстрем     — вес 10
  7. AI подтверждает BUY     — вес 10
  Штраф: памп-риск           — до −20

All-in срабатывает если:
  score >= ALLIN_BOTTOM_CONF  AND  RSI <= ALLIN_RSI_MAX  AND  не в пампе
  AND  кулдаун (4 ч) прошёл  AND  spendable >= ALLIN_MIN_FREE_TON
"""
import logging
import time
from typing import Dict, Any, Optional

logger = logging.getLogger("bottom_detector")

# Кулдаун между all-in триггерами: не чаще раза в 4 часа
_COOLDOWN_SEC = 4 * 3600


class BottomDetector:
    """Детектор рыночного дна. Singleton через модульную переменную bottom_detector."""

    def __init__(self):
        self.last_score: float = 0.0
        self.last_signals: Dict[str, str] = {}
        self.last_all_in: bool = False
        self._last_trigger_ts: float = 0.0

    # ──────────────────────────────────────────────────────────────────────────
    def analyze(
        self,
        rsi: float,
        stoch_rsi: float,        # 0..1  (нормированный RSI стохастик)
        bb_pct: float,           # 0..100 (0 = нижняя BB, 100 = верхняя BB)
        vol_ratio: float,        # текущий объём / MA20
        macd_hist: float,        # текущая гистограмма MACD
        macd_hist_prev: float,   # гистограмма MACD прошлого бара
        willr: float,            # Williams %R: −100 (перепродан) .. 0 (перекуплен)
        ai_signal: str,          # "BUY" / "HOLD" / "SELL"
        ai_conf: float,          # 0..100
        pump_score: float,       # 0..100 (от GRINCHPumpDetector)
    ) -> Dict[str, Any]:
        """
        Возвращает:
          score   — суммарный балл 0-100
          all_in  — True если нужно войти на весь баланс
          signals — словарь активных сигналов {имя: описание}
          reason  — человекочитаемая строка
        """
        try:
            from config import Config
        except ImportError:
            Config = None  # type: ignore

        score = 0.0
        signals: Dict[str, str] = {}

        # 1. RSI — перепроданность (вес 25) ───────────────────────────────────
        if rsi <= 20:
            score += 25
            signals["RSI_экстрем"] = f"RSI={rsi:.1f}≤20"
        elif rsi <= 28:
            score += 15
            signals["RSI_перепродан"] = f"RSI={rsi:.1f}≤28"
        elif rsi <= 35:
            score += 7
            signals["RSI_слабый"] = f"RSI={rsi:.1f}≤35"

        # 2. StochRSI — в зоне дна (вес 10) ──────────────────────────────────
        if stoch_rsi <= 0.08:
            score += 10
            signals["StochRSI_дно"] = f"StRSI={stoch_rsi:.3f}"
        elif stoch_rsi <= 0.20:
            score += 5
            signals["StochRSI_низкий"] = f"StRSI={stoch_rsi:.3f}"

        # 3. Bollinger Band position — у нижней полосы (вес 15) ───────────────
        if bb_pct <= 5:
            score += 15
            signals["BB_нижняя"] = f"bb_pct={bb_pct:.1f}"
        elif bb_pct <= 15:
            score += 8
            signals["BB_низкая"] = f"bb_pct={bb_pct:.1f}"
        elif bb_pct <= 25:
            score += 4
            signals["BB_пониже_середины"] = f"bb_pct={bb_pct:.1f}"

        # 4. Объём — паника / капитуляция (вес 15) ───────────────────────────
        if vol_ratio >= 3.0:
            score += 15
            signals["Паника_x3+"] = f"vol={vol_ratio:.1f}x"
        elif vol_ratio >= 2.0:
            score += 10
            signals["Высокий_объём_x2"] = f"vol={vol_ratio:.1f}x"
        elif vol_ratio >= 1.5:
            score += 5
            signals["Объём_повышен"] = f"vol={vol_ratio:.1f}x"

        # 5. MACD разворачивается вверх из отрицательной зоны (вес 15) ────────
        if macd_hist < 0 and macd_hist > macd_hist_prev:
            score += 15
            signals["MACD_разворот↑"] = f"h={macd_hist:.2e}>prev={macd_hist_prev:.2e}"
        elif macd_hist >= 0 and macd_hist > macd_hist_prev and macd_hist_prev < 0:
            # пересечение нуля снизу вверх
            score += 10
            signals["MACD_пересечение_нуля"] = f"h={macd_hist:.2e}"
        elif macd_hist > 0 and macd_hist > macd_hist_prev:
            score += 4
            signals["MACD_позитив"] = f"h={macd_hist:.2e}"

        # 6. Williams %R — экстремальная перепроданность (вес 10) ────────────
        if willr <= -90:
            score += 10
            signals["WillR_экстрем"] = f"WR={willr:.0f}"
        elif willr <= -80:
            score += 6
            signals["WillR_перепродан"] = f"WR={willr:.0f}"
        elif willr <= -70:
            score += 3
            signals["WillR_низкий"] = f"WR={willr:.0f}"

        # 7. AI подтверждает BUY (вес 10) ─────────────────────────────────────
        if ai_signal == "BUY" and ai_conf >= 65:
            score += 10
            signals["AI_уверен_BUY"] = f"conf={ai_conf:.0f}%"
        elif ai_signal == "BUY" and ai_conf >= 45:
            score += 5
            signals["AI_BUY"] = f"conf={ai_conf:.0f}%"

        # Штраф: памп-риск — никогда не all-in в памп (до −20) ───────────────
        if pump_score >= 65:
            score -= 20
            signals["⚠️_памп_риск"] = f"pump={pump_score:.0f}"
        elif pump_score >= 45:
            score -= 8
            signals["⚠️_памп_умерен"] = f"pump={pump_score:.0f}"

        score = max(0.0, min(100.0, score))

        # ── Проверяем условия all-in ──────────────────────────────────────────
        rsi_max  = float(getattr(Config, "ALLIN_RSI_MAX",      32.0) if Config else 32.0)
        min_conf = float(getattr(Config, "ALLIN_BOTTOM_CONF",  65.0) if Config else 65.0)
        enabled  = bool(getattr(Config, "ALLIN_ON_BOTTOM",     False) if Config else False)
        cooldown_ok = (time.time() - self._last_trigger_ts) >= _COOLDOWN_SEC

        all_in = (
            enabled
            and score >= min_conf
            and rsi <= rsi_max
            and cooldown_ok
            and pump_score < 50  # никогда не all-in в памп
        )

        self.last_score   = score
        self.last_signals = signals
        self.last_all_in  = all_in

        if all_in:
            self._last_trigger_ts = time.time()
            logger.warning(
                "🔥 ДНО ОБНАРУЖЕНО! score=%.0f/100 RSI=%.1f WillR=%.0f "
                "bb_pct=%.1f vol=%.1fx | ALL-IN сигнал | сигналы: %s",
                score, rsi, willr, bb_pct, vol_ratio, list(signals.keys()),
            )
        elif score >= 40:
            logger.debug(
                "📊 BottomScore=%.0f/100 (нужно %.0f) RSI=%.1f | %s",
                score, min_conf, rsi, list(signals.keys()),
            )

        return {
            "score":   round(score, 1),
            "all_in":  all_in,
            "signals": signals,
            "rsi":     round(rsi, 2),
            "reason":  self._build_reason(signals, score, all_in),
            "cooldown_left_sec": max(
                0.0,
                _COOLDOWN_SEC - (time.time() - self._last_trigger_ts)
            ) if not cooldown_ok else 0.0,
        }

    def _build_reason(self, signals: Dict[str, str], score: float, all_in: bool) -> str:
        parts = [f"{k}({v})" for k, v in list(signals.items())[:6]]
        prefix = "🔥 ALL-IN: " if all_in else f"score={score:.0f}/100: "
        return prefix + " | ".join(parts) if parts else prefix.rstrip(": ")

    def status(self) -> Dict[str, Any]:
        """Быстрый снапшот для дашборда."""
        try:
            from config import Config
            enabled = Config.ALLIN_ON_BOTTOM
            conf    = Config.ALLIN_BOTTOM_CONF
            rsi_max = Config.ALLIN_RSI_MAX
        except Exception:
            enabled, conf, rsi_max = False, 65.0, 32.0
        return {
            "enabled":         enabled,
            "last_score":      round(self.last_score, 1),
            "last_all_in":     self.last_all_in,
            "last_signals":    self.last_signals,
            "threshold":       conf,
            "rsi_max":         rsi_max,
            "cooldown_left":   max(
                0.0,
                _COOLDOWN_SEC - (time.time() - self._last_trigger_ts)
            ),
        }


# ── Singleton ────────────────────────────────────────────────────────────────
bottom_detector = BottomDetector()
