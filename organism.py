"""
🧬 QuantumBrain Living Organism v1.0
Семь биологических систем, превращающих AI в живое существо.

1. Настроение / Эмоции  — fear/greed/confidence → порог и размер сделок
2. Голод                — время без сделок → агрессивность входа
3. Энергия / Сон        — волатильность рынка → уровень активности
4. Эволюция             — мутация параметров по результатам сделок
5. Инстинкты            — рефлексы без ML: паника, возбуждение, защита
6. Сны                  — фоновые симуляции на исторических ценах
7. Здоровье             — составная метрика жизнеспособности (дашборд)
"""

import time
import math
import random
import threading
import logging
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger("organism")


class Organism:
    VERSION = "1.0"

    # ── Hunger ─────────────────────────────────────────────────────────────
    HUNGER_SATURATION_SEC   = 7200   # 2 ч без сделок = полный голод (1.0)
    HUNGER_CONF_BONUS_MAX   = 8.0    # макс. снижение порога от голода, пп

    # ── Energy ─────────────────────────────────────────────────────────────
    ENERGY_SLEEP_CONF_PENALTY = 5.0  # повышение порога во время «сна», пп
    ENERGY_AWAKE_CONF_BONUS   = 3.0  # снижение порога при высокой энергии, пп

    # ── Instincts ──────────────────────────────────────────────────────────
    FLASH_CRASH_DROP_PCT    = 8.0    # % падения за 60 с → флэш-крэш
    EXCITEMENT_RISE_PCT     = 5.0    # % роста за 60 с → возбуждение
    PANIC_DURATION_SEC      = 300    # паника длится 5 мин
    DEFENSIVE_CONSEC_LOSSES = 3      # N убытков подряд → защитный режим

    # ── Evolution ──────────────────────────────────────────────────────────
    EVOLVE_EVERY_N        = 10       # мутация каждые N сделок
    EVOLVE_MUTATION_STEP  = 1.5      # макс. мутация порога уверенности, пп

    # ── Dreams ─────────────────────────────────────────────────────────────
    DREAM_ENERGY_THRESHOLD = 0.35    # ниже этой энергии → бот «видит сны»
    DREAM_SIM_COUNT        = 20      # симуляций за один цикл сна

    def __init__(self):
        self._lock = threading.RLock()

        # ── 1. Mood / Emotions ─────────────────────────────────────────────
        self.fear           = 0.20
        self.greed          = 0.30
        self.confidence     = 0.50
        self.mood           = 0.10    # -1..+1
        self.emotion_label  = "нейтральный"

        # ── 2. Hunger ──────────────────────────────────────────────────────
        self.hunger         = 0.0
        self._last_trade_ts = time.time()

        # ── 3. Energy / Sleep ──────────────────────────────────────────────
        self.energy      = 0.70
        self.sleep_phase = "awake"    # awake / drowsy / sleeping
        self._price_hist: List[Tuple[float, float]] = []  # (ts, price)

        # ── 4. Evolution ───────────────────────────────────────────────────
        self.generation          = 0
        self._trade_wins         = 0
        self._trade_total        = 0
        self._trades_since_evolve= 0
        self._evolved_conf_delta = 0.0   # текущая накопленная мутация (пп)
        self.evolution_fitness   = 0.50
        self.evolution_active    = False

        # ── 5. Instincts ───────────────────────────────────────────────────
        self.panic_mode           = False
        self._panic_until         = 0.0
        self._consecutive_losses  = 0
        self.instinct_signal      = None   # None / "SELL_PANIC" / "BUY_EXCITEMENT"
        self.flash_crash_detected = False
        self.defensive_mode       = False

        # ── 6. Dreams ──────────────────────────────────────────────────────
        self.dreaming      = False
        self.dream_quality = 0.50
        self.dreams_total  = 0
        self.dream_wins    = 0
        self.dream_pnl_avg = 0.0
        self._dream_prices: List[float] = []
        self._dream_thread = None
        self._dream_stop   = threading.Event()

        # ── 7. Health ──────────────────────────────────────────────────────
        self.health       = 0.75
        self.health_label = "хорошее"

        # ── Meta ───────────────────────────────────────────────────────────
        self.age         = 0
        self.alive_since = time.time()
        self.last_update = time.time()

        self._start_dreams()
        logger.info("🧬 Organism v1.0 пробудился — все 7 систем активны")

    # ════════════════════════════════════════════════════════════════════════
    # Public API (вызывается из trader.py)
    # ════════════════════════════════════════════════════════════════════════

    def restore(self, trader) -> None:
        """Восстанавливаем состояние из накопленной статистики трейдера."""
        with self._lock:
            try:
                wins  = int(trader.stats.get("winning_trades", 0) or 0)
                total = int(trader.stats.get("total_trades",   0) or 0)
                self.age          = total
                self._trade_wins  = wins
                self._trade_total = total
                if total >= 3:
                    wr = wins / total
                    self.confidence = round(wr, 3)
                    self.fear       = round(max(0.0, 0.35 - wr * 0.35), 3)
                    self.greed      = round(min(1.0, wr  * 0.80), 3)
                    self._update_mood()
                    self.evolution_fitness = round(wr, 3)
                self._update_health()
                logger.info(
                    f"🧬 Organism restored: age={self.age} "
                    f"wr={wins}/{total} mood={self.mood:+.2f} "
                    f"health={self.health:.2f}({self.health_label})"
                )
            except Exception as e:
                logger.warning(f"Organism restore error: {e}")

    def update_tick(self, price: float, ai: Optional[Dict]) -> None:
        """Главный апдейт — вызывается каждый тик торгового цикла."""
        if price <= 0:
            return
        with self._lock:
            now = time.time()
            self.last_update = now

            # Пополняем историю цен
            self._price_hist.append((now, price))
            self._price_hist = [
                (t, p) for (t, p) in self._price_hist if now - t <= 120
            ]
            self._dream_prices.append(price)
            if len(self._dream_prices) > 200:
                self._dream_prices = self._dream_prices[-200:]

            self._update_hunger(now)
            self._update_energy()
            self._update_instincts(now, price)

            if ai:
                self._update_mood_from_ai(ai)
            else:
                self._decay_mood()

            self._update_health()

    def get_conf_modifier(self) -> float:
        """Итоговое смещение порога уверенности AI (процентные пункты).

        Отрицательное = снижаем порог (агрессивнее).
        Положительное = повышаем порог (осторожнее).
        """
        with self._lock:
            delta = 0.0

            # Голод снижает порог: хочет торговать
            if self.hunger > 0.2:
                hunger_norm = (self.hunger - 0.2) / 0.8
                delta -= self.HUNGER_CONF_BONUS_MAX * hunger_norm

            # Энергия: сон → осторожнее, бодрствование → агрессивнее
            if self.energy < 0.30:
                delta += self.ENERGY_SLEEP_CONF_PENALTY
            elif self.energy > 0.70:
                delta -= self.ENERGY_AWAKE_CONF_BONUS * (self.energy - 0.70) / 0.30

            # Настроение: жадность → смелее, страх → осторожнее
            if self.mood > 0.30:
                delta -= 3.0 * (self.mood - 0.30) / 0.70
            elif self.mood < -0.30:
                delta += 5.0 * abs(self.mood + 0.30) / 0.70

            # Защитный режим после серии убытков
            if self.defensive_mode:
                delta += 7.0

            # Паника — вход полностью блокируется
            if self.panic_mode:
                delta += 25.0

            # Эволюционная мутация (может быть + или -)
            delta += self._evolved_conf_delta

            return round(delta, 2)

    def get_size_multiplier(self) -> float:
        """Множитель размера позиции (диапазон 0.4 – 1.8)."""
        with self._lock:
            mult = 1.0

            # Голод незначительно увеличивает размер
            mult += 0.12 * self.hunger

            # Настроение
            mult += 0.20 * self.mood   # жадность +0.2, страх −0.2

            # Энергия: сонный бот торгует меньше
            if self.energy < 0.30:
                mult *= 0.70

            # Паника или защита: минимальный размер
            if self.panic_mode or self.defensive_mode:
                mult *= 0.50

            return round(max(0.40, min(1.80, mult)), 3)

    def get_instinct_override(self) -> Optional[str]:
        """Инстинктивный сигнал (приоритет над ML) или None.

        BUY_EXCITEMENT однократный; SELL_PANIC повторяется до конца паники.
        """
        with self._lock:
            sig = self.instinct_signal
            if sig == "BUY_EXCITEMENT":
                # Возбуждение — одноразовый импульс
                self.instinct_signal = None
            return sig

    def on_trade_opened(self) -> None:
        """Уведомление: сделка открыта — организм насытился (сброс голода)."""
        with self._lock:
            self._last_trade_ts = time.time()
            self.hunger = 0.0

    def on_trade_closed(self, pnl: float, is_win: bool) -> None:
        """Уведомление: сделка закрыта — обновляем эмоции и эволюцию."""
        with self._lock:
            self.age += 1
            self._last_trade_ts = time.time()
            self.hunger = 0.0

            self._trade_total += 1
            if is_win:
                self._trade_wins += 1
                self._consecutive_losses = 0
                self.defensive_mode = False
                self.greed = min(1.0, self.greed + 0.12)
                self.fear  = max(0.0, self.fear  - 0.08)
            else:
                self._consecutive_losses += 1
                self.fear  = min(1.0, self.fear  + 0.18)
                self.greed = max(0.0, self.greed - 0.10)
                if self._consecutive_losses >= self.DEFENSIVE_CONSEC_LOSSES:
                    self.defensive_mode = True
                    logger.warning(
                        f"🧬 ЗАЩИТНЫЙ РЕЖИМ: {self._consecutive_losses} убытков подряд"
                    )

            self._update_mood()
            self._update_health()
            self._evolution_feedback()

            self._trades_since_evolve += 1
            if self._trades_since_evolve >= self.EVOLVE_EVERY_N:
                self._try_evolve()
                self._trades_since_evolve = 0

    def get_state(self) -> Dict[str, Any]:
        """Полный снапшот для /api/organism и дашборда."""
        with self._lock:
            return {
                "version":            self.VERSION,
                "age":                self.age,
                "alive_sec":          int(time.time() - self.alive_since),
                # 1. Mood
                "mood":               round(self.mood,       3),
                "fear":               round(self.fear,       3),
                "greed":              round(self.greed,      3),
                "confidence":         round(self.confidence, 3),
                "emotion_label":      self.emotion_label,
                # 2. Hunger
                "hunger":             round(self.hunger, 3),
                # 3. Energy
                "energy":             round(self.energy, 3),
                "sleep_phase":        self.sleep_phase,
                # 4. Evolution
                "generation":         self.generation,
                "evolution_fitness":  round(self.evolution_fitness, 3),
                "evolution_active":   self.evolution_active,
                "evolved_conf_delta": round(self._evolved_conf_delta, 2),
                # 5. Instincts
                "panic_mode":         self.panic_mode,
                "flash_crash":        self.flash_crash_detected,
                "defensive_mode":     self.defensive_mode,
                "consecutive_losses": self._consecutive_losses,
                "instinct_signal":    self.instinct_signal,
                # 6. Dreams
                "dreaming":           self.dreaming,
                "dream_quality":      round(self.dream_quality, 3),
                "dreams_total":       self.dreams_total,
                "dream_pnl_avg":      round(self.dream_pnl_avg, 2),
                # 7. Health
                "health":             round(self.health, 3),
                "health_label":       self.health_label,
                # Modifiers (для отладки)
                "conf_modifier":      self.get_conf_modifier(),
                "size_multiplier":    self.get_size_multiplier(),
            }

    # ════════════════════════════════════════════════════════════════════════
    # Private — биологические системы
    # ════════════════════════════════════════════════════════════════════════

    # ── 2. Hunger ────────────────────────────────────────────────────────────
    def _update_hunger(self, now: float) -> None:
        elapsed = now - self._last_trade_ts
        self.hunger = round(min(1.0, elapsed / self.HUNGER_SATURATION_SEC), 3)

    # ── 3. Energy / Sleep ────────────────────────────────────────────────────
    def _update_energy(self) -> None:
        if len(self._price_hist) < 2:
            return
        now = time.time()
        recent = [(t, p) for (t, p) in self._price_hist if now - t <= 60]
        if len(recent) < 2:
            return
        prices = [p for _, p in recent]
        p_min, p_max = min(prices), max(prices)
        if p_min <= 0:
            return
        range_pct = (p_max - p_min) / p_min * 100.0
        # 0% vol → 0.10 energy; 8%+ vol → 1.0 energy
        target = min(1.0, 0.10 + range_pct / 8.0 * 0.90)
        # EWMA α=0.2
        self.energy = round(self.energy * 0.80 + target * 0.20, 3)

        if self.energy < 0.30:
            self.sleep_phase = "sleeping"
        elif self.energy < 0.55:
            self.sleep_phase = "drowsy"
        else:
            self.sleep_phase = "awake"

    # ── 5. Instincts ─────────────────────────────────────────────────────────
    def _update_instincts(self, now: float, price: float) -> None:
        # Сбрасываем панику по таймеру
        if self.panic_mode and now > self._panic_until:
            self.panic_mode = False
            self.flash_crash_detected = False
            self.instinct_signal = None
            logger.info("🧬 Паника спала — организм успокоился")

        # Анализ изменения цены за 60 секунд
        hist_60 = [(t, p) for (t, p) in self._price_hist if now - t <= 60]
        if len(hist_60) < 3:
            return
        oldest_price = hist_60[0][1]
        if oldest_price <= 0:
            return
        change_pct = (price - oldest_price) / oldest_price * 100.0

        # Флэш-крэш → паника → SELL_PANIC
        if change_pct <= -self.FLASH_CRASH_DROP_PCT and not self.panic_mode:
            self.panic_mode = True
            self.flash_crash_detected = True
            self._panic_until = now + self.PANIC_DURATION_SEC
            self.instinct_signal = "SELL_PANIC"
            logger.warning(
                f"🧬 ⚡ ИНСТИНКТ ПАНИКА: {change_pct:.1f}% за 60с! "
                f"SELL_PANIC, защита {self.PANIC_DURATION_SEC // 60} мин"
            )
        # Стремительный рост → возбуждение → BUY_EXCITEMENT
        elif change_pct >= self.EXCITEMENT_RISE_PCT and not self.panic_mode:
            if self.instinct_signal != "BUY_EXCITEMENT":
                self.instinct_signal = "BUY_EXCITEMENT"
                logger.info(
                    f"🧬 ⚡ ИНСТИНКТ ВОЗБУЖДЕНИЕ: +{change_pct:.1f}% за 60с → BUY_EXCITEMENT"
                )

    # ── 1. Mood ──────────────────────────────────────────────────────────────
    def _update_mood_from_ai(self, ai: Dict) -> None:
        conf   = float(ai.get("confidence", 50) or 50)
        signal = ai.get("ai_signal", "HOLD")
        conf_n = conf / 100.0

        if signal == "BUY":
            self.greed = min(1.0, self.greed * 0.96 + conf_n * 0.06)
        elif signal == "SELL":
            self.fear  = min(1.0, self.fear  * 0.96 + conf_n * 0.06)

        self._update_mood()

    def _decay_mood(self) -> None:
        """Медленное затухание без AI-данных (DCA тик)."""
        self.fear  = max(0.0, self.fear  * 0.999)
        self.greed = max(0.1, self.greed * 0.999)
        self._update_mood()

    def _update_mood(self) -> None:
        raw = self.greed - self.fear
        self.mood = round(math.tanh(raw * 2), 3)

        if self.mood > 0.50:
            self.emotion_label = "жадность"
        elif self.mood > 0.20:
            self.emotion_label = "оптимизм"
        elif self.mood > -0.20:
            self.emotion_label = "нейтральный"
        elif self.mood > -0.50:
            self.emotion_label = "осторожность"
        else:
            self.emotion_label = "страх"

        # Slow decay to neutral
        self.fear  = max(0.0, self.fear  * 0.9985)
        self.greed = max(0.1, self.greed * 0.9985)

        self.confidence = (
            round(self._trade_wins / self._trade_total, 3)
            if self._trade_total >= 5 else 0.50
        )

    # ── 7. Health ────────────────────────────────────────────────────────────
    def _update_health(self) -> None:
        wr       = (self._trade_wins / self._trade_total
                    if self._trade_total >= 5 else 0.50)
        mood_sc  = (self.mood + 1.0) / 2.0   # -1..+1 → 0..1

        self.health = round(
            0.35 * wr +
            0.20 * self.energy +
            0.15 * mood_sc +
            0.15 * self.evolution_fitness +
            0.15 * self.dream_quality,
            3,
        )

        if self.health > 0.75:
            self.health_label = "отличное"
        elif self.health > 0.55:
            self.health_label = "хорошее"
        elif self.health > 0.35:
            self.health_label = "слабое"
        else:
            self.health_label = "критическое"

    # ── 4. Evolution ─────────────────────────────────────────────────────────
    def _evolution_feedback(self) -> None:
        if self._trade_total > 0:
            self.evolution_fitness = round(
                self._trade_wins / self._trade_total, 3
            )

    def _try_evolve(self) -> None:
        """Hill-climbing мутация порога уверенности."""
        if self._trade_total < 10:
            return

        baseline_wr = self._trade_wins / self._trade_total
        mutation = random.uniform(-self.EVOLVE_MUTATION_STEP, self.EVOLVE_MUTATION_STEP)

        # Направляем мутацию: плохой WR → снижаем порог, хороший → поднимаем
        if baseline_wr < 0.55:
            new_delta = self._evolved_conf_delta - abs(mutation) * 0.6
        elif baseline_wr > 0.72:
            new_delta = self._evolved_conf_delta + abs(mutation) * 0.4
        else:
            new_delta = self._evolved_conf_delta + mutation

        new_delta = round(max(-8.0, min(8.0, new_delta)), 2)
        old_delta = self._evolved_conf_delta
        self._evolved_conf_delta = new_delta
        self.generation += 1
        self.evolution_active = True

        logger.info(
            f"🧬 Эволюция поколение #{self.generation}: "
            f"conf_delta {old_delta:+.1f}%→{new_delta:+.1f}% | WR={baseline_wr:.1%}"
        )

    # ── 6. Dreams ────────────────────────────────────────────────────────────
    def _start_dreams(self) -> None:
        self._dream_thread = threading.Thread(
            target=self._dream_loop, name="organism-dreams", daemon=True
        )
        self._dream_thread.start()

    def _dream_loop(self) -> None:
        """Фоновый поток: симуляции при низкой рыночной активности."""
        while not self._dream_stop.is_set():
            time.sleep(45)
            try:
                with self._lock:
                    energy = self.energy
                    prices = list(self._dream_prices)
                if energy < self.DREAM_ENERGY_THRESHOLD and len(prices) >= 20:
                    self._run_dream(prices)
            except Exception as e:
                logger.debug(f"Dream loop error: {e}")

    def _run_dream(self, prices: List[float]) -> None:
        """Симулирует DREAM_SIM_COUNT «гипотетических» сделок."""
        with self._lock:
            self.dreaming = True
        wins = 0
        total_pnl = 0.0
        n = len(prices)
        try:
            for _ in range(self.DREAM_SIM_COUNT):
                if n < 10:
                    break
                entry_idx = random.randint(0, n // 2)
                exit_idx  = random.randint(
                    entry_idx + 3, min(entry_idx + 30, n - 1)
                )
                ep = prices[entry_idx]
                xp = prices[exit_idx]
                if ep <= 0:
                    continue
                pnl_pct = (xp - ep) / ep * 100.0 - 2.0  # ~2% comission
                if pnl_pct > 0:
                    wins += 1
                total_pnl += pnl_pct

            dream_wr  = wins / self.DREAM_SIM_COUNT
            dream_avg = total_pnl / self.DREAM_SIM_COUNT

            with self._lock:
                self.dreams_total  += self.DREAM_SIM_COUNT
                self.dream_wins    += wins
                self.dream_quality  = round(
                    self.dream_quality * 0.70 + dream_wr * 0.30, 3
                )
                self.dream_pnl_avg  = round(
                    self.dream_pnl_avg * 0.70 + dream_avg * 0.30, 2
                )
                self.dreaming = False

            logger.debug(
                f"🧬 💤 Сон: {wins}/{self.DREAM_SIM_COUNT} побед | "
                f"quality={dream_wr:.0%} avg={dream_avg:+.1f}%"
            )
        except Exception as e:
            with self._lock:
                self.dreaming = False
            logger.debug(f"Dream simulation error: {e}")


# ─── Singleton ────────────────────────────────────────────────────────────────
organism = Organism()
