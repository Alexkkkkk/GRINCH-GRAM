import threading
import time
from datetime import datetime
from config import Config
from exchange import ExchangeClient
from strategy import analyze
from ai_engine import AIEngine
from experience_manager import experience_manager


class Trader:
    def __init__(self):
        self.exchange = ExchangeClient()
        self.ai       = AIEngine()
        self.running  = False
        self.training = False
        self.trades      = []
        self.open_trades = []
        self.logs        = []
        self.last_ai     = {}
        self.stats = {
            "total_trades":   0,
            "winning_trades": 0,
            "total_pnl":      0.0,
            "start_balance":  10000.0,
        }
        self._thread = None
        self.signal_callbacks = []
        self.on_training_progress = None
        # Сериализация закрытия позиций: не даём торговому циклу и ручному
        # закрытию продать одну и ту же позицию дважды.
        self._close_lock = threading.Lock()
        # Счётчик подтверждений BUY-сигнала (требуем 2 последовательных)
        self._buy_confirm_count = 0
        # Кеш баланса: не долбим блокчейн при каждом /api/status (TTL 30 сек)
        self._balance_cache     = {}
        self._balance_cache_ts  = 0
        self._balance_cache_ttl = 30  # секунд
        # ── Долговременная память + само-управление ИИ ───────────────────
        self.exp = experience_manager
        self.exp.restore_trader(self)

    def log(self, msg, level="INFO"):
        entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "level": level, "msg": msg}
        self.logs.append(entry)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        print(f"[{entry['time']}] [{level}] {msg}")

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log("Торговый агент запущен", "INFO")

    def stop(self):
        self.running = False
        self.training = False
        self.log("Торговый агент остановлен", "WARN")

    # ──────────────────────────────────────────
    # Главный цикл
    # ──────────────────────────────────────────
    def _loop(self):
        self.training = True
        self.log("🧠 Начинаю предобучение AI модели...", "INFO")
        try:
            ohlcv = self.exchange.get_ohlcv(limit=300)
            self.ai.pretrain(ohlcv, on_progress=self._emit_progress)
        except Exception as e:
            self.log(f"⚠️ Ошибка предобучения: {e}", "WARN")
        self.training = False
        self.log("✅ Предобучение завершено. Запускаю торговый цикл.", "INFO")

        # Возвращаем сохранённый опыт обратно в ИИ (тёплый старт обучения)
        try:
            n = self.exp.restore_ai(self.ai)
            if n:
                self.log(f"🧠 ИИ дообучен на {n} сохранённых сделках из памяти", "INFO")
        except Exception as e:
            self.log(f"Восстановление опыта ИИ: {e}", "WARN")

        while self.running:
            try:
                self._tick()
                self._record_equity()
            except Exception as e:
                self.log(f"Ошибка в цикле: {e}", "ERROR")
            time.sleep(30)

    def _record_equity(self):
        """Снимок капитала кошелька в память (троттлинг внутри менеджера)."""
        try:
            from price_feed import price_feed
            self.exp.record_balance(self._get_balance_cached(),
                                    price_feed.get("GRINCH") or 0.0)
        except Exception:  # noqa: BLE001
            pass

    def _emit_progress(self, progress_dict):
        if self.on_training_progress:
            try:
                self.on_training_progress(progress_dict)
            except Exception:
                pass

    # ──────────────────────────────────────────
    # Торговый тик
    # ──────────────────────────────────────────
    def _tick(self):
        ohlcv  = self.exchange.get_ohlcv(limit=100)
        result = analyze(ohlcv)
        ai     = self.ai.analyze(ohlcv)
        self.last_ai = ai

        signal      = result["signal"]
        ai_signal   = ai.get("ai_signal", "HOLD")
        price       = result["price"]
        conf        = ai.get("confidence", 0)
        rsi         = result.get("rsi", 50)
        regime      = ai.get("regime", {}) or {}
        regime_name = regime.get("name", "?")
        anomaly     = ai.get("anomaly", {}).get("detected", False)

        # Если SL/TP закрыл все позиции и разослал SELL — завершаем тик,
        # чтобы в этом же тике не открыть BUY поверх ещё не сведённого
        # пользовательского состояния (гонка SELL→BUY в одном окне).
        if self._check_stop_loss_take_profit(price):
            return

        # ── Формируем итоговый сигнал ──────────────────────────────────
        final_signal = "HOLD"
        if signal == ai_signal and signal != "HOLD":
            final_signal = signal
        elif ai_signal != "HOLD" and conf >= Config.AI_OVERRIDE_CONFIDENCE:
            final_signal = ai_signal

        if anomaly:
            self.log(f"⚠️ АНОМАЛИЯ! Z-цена={ai['anomaly']['z_price']}", "WARN")

        # ── Счётчик подтверждений (требуем 2 последовательных BUY) ────
        if final_signal == "BUY":
            self._buy_confirm_count += 1
        else:
            self._buy_confirm_count = 0

        # ── Фильтры входа ──────────────────────────────────────────────
        blocked = None
        if final_signal == "BUY":
            hard_override     = conf >= Config.AI_HARD_OVERRIDE_CONFIDENCE
            mean_rev_override = (
                rsi <= Config.RSI_OVERSOLD_REVERSAL and
                conf >= Config.REVERSAL_AI_MIN
            )

            if self.exp.is_paused():
                blocked = "ИИ-пауза: просадка капитала (защита от убытков)"
            elif conf < Config.MIN_AI_CONFIDENCE:
                blocked = f"низкая уверенность AI {conf}%"
            elif mean_rev_override:
                # RSI экстремально низкий + AI уверен → Mean Reversion вход даже в DOWNTREND
                self.log(
                    f"📈 Mean Reversion Override: RSI={rsi:.1f} + AI={conf}% "
                    f"→ вход несмотря на {regime_name}", "INFO"
                )
            elif Config.TREND_FILTER and regime_name == "DOWNTREND":
                blocked = "нисходящий тренд"
            elif regime_name == "VOLATILE" and not hard_override:
                # VOLATILE = хаос, без сильного AI-сигнала не входим
                blocked = f"хаотичный рынок (VOLATILE)"
            elif hard_override:
                if anomaly:
                    self.log(
                        f"🔥 Hard Override: AI={conf}% → вход несмотря на "
                        f"аномалию Z={ai['anomaly']['z_price']}", "INFO"
                    )
            elif rsi >= Config.RSI_OVERBOUGHT:
                blocked = f"перекупленность RSI={rsi:.1f}"
            elif anomaly:
                blocked = f"рыночная аномалия Z={ai['anomaly']['z_price']:.2f}"
            elif self._buy_confirm_count < 2 and not hard_override:
                # Нужно 2 последовательных сигнала для подтверждения
                blocked = f"ожидаем подтверждение ({self._buy_confirm_count}/2)"

        self.log(
            f"📊 RSI={rsi:.1f} | {regime_name} | "
            f"Сигнал={signal} | AI={ai_signal}({conf}%) | "
            f"Итог={'HOLD' if blocked else final_signal}",
            level="INFO"
        )

        if final_signal == "BUY" and blocked:
            self.log(f"⏸️ Вход отменён: {blocked}", "WARN")
        elif final_signal == "BUY" and len(self.open_trades) < Config.MAX_OPEN_TRADES:
            opened = self._open_trade("buy", price, result, ai)
            # Сигнал пользователям шлём ТОЛЬКО если реальный ордер исполнился —
            # иначе у юзеров спишется виртуальный TON и откроется позиция без
            # реальной сделки (рассинхрон балансов, блокировка вывода).
            if opened:
                self._buy_confirm_count = 0   # сбрасываем после входа
                self._emit_signal("BUY", price, ai)
        elif final_signal == "SELL" and self.open_trades:
            closed = self._close_all_trades(price, result)
            # Сигнал SELL юзерам шлём ТОЛЬКО если реальная продажа прошла —
            # иначе у юзеров обнулится grinch_held и разблокируется вывод TON,
            # которого на самом деле нет (реальный GRINCH не продан).
            if closed:
                self._emit_signal("SELL", price, ai)

    def _emit_signal(self, signal, price, ai=None):
        """Шлёт сигнал BUY/SELL всем подписчикам (UserTradingManager и т.п.).
        Вызывать ТОЛЬКО после подтверждённого реального исполнения сделки —
        иначе виртуальное состояние юзеров рассинхронизируется с реальными активами."""
        for cb in self.signal_callbacks:
            try:   cb(signal, price, ai)
            except Exception as e: self.log(f"Signal cb ошибка: {e}", "WARN")

    def _relevant_open(self):
        return [t for t in self.open_trades
                if t.get("symbol", Config.SYMBOL) == Config.SYMBOL]

    # ──────────────────────────────────────────
    # Торговые операции
    # ──────────────────────────────────────────
    def _targets(self, price, ai):
        atr_pct = (ai.get("regime", {}) or {}).get("atr_pct", 0) / 100.0 if ai else 0.0
        if Config.USE_DYNAMIC_TARGETS and atr_pct > 0:
            sl_pct = max(atr_pct * Config.ATR_SL_MULT * 100, Config.STOP_LOSS_PCT)
            # ATR × 8 используется только если ОН ВЫШЕ минимального нетто-таргета
            tp_pct = max(atr_pct * Config.ATR_TP_MULT * 100, Config.TAKE_PROFIT_PCT)
        else:
            sl_pct, tp_pct = Config.STOP_LOSS_PCT, Config.TAKE_PROFIT_PCT

        # Жёсткий минимум TP = gross, дающий ровно +TARGET_NET_PCT нетто после
        # комиссии обеих ног. Никогда не закрываемся дешевле этого уровня.
        min_gross_tp = Config.required_gross_pct()
        tp_pct = max(tp_pct, min_gross_tp)
        return sl_pct, tp_pct

    def _open_trade(self, side, price, analysis, ai=None):
        ai_conf = ai.get("confidence", 0) if ai else 0

        # Ставка пропорциональна уверенности AI: 50%→100% капитала
        conf_factor = 0.5 + min(max((ai_conf - 50) / 50.0, 0.0), 1.0) * 0.5
        stake  = Config.TRADE_AMOUNT * conf_factor

        # ── Резерв на комиссию + опрос баланса перед сделкой ─────────────
        # ВСЕГДА оставляем GAS_RESERVE_TON на газ будущей продажи GRINCH→TON.
        # Покупка не тратит резерв: при нехватке урезаем ставку, а если денег
        # нет даже на резерв + газ покупки — сделку отменяем (fail-closed).
        ton_stake = None
        if self.exchange.mode == "dedust" and side == "buy":
            bal     = self.exchange.get_balance() or {}
            ton_bal = bal.get("TON", 0) or 0
            buy_gas = 0.45                      # газ BUY-свопа + запас на сеть
            reserve = Config.GAS_RESERVE_TON    # неприкосновенный резерв на комиссию продажи
            spendable = ton_bal - buy_gas - reserve
            if bal.get("error") or spendable < Config.MIN_STAKE_TON:
                why = bal.get("error") or (
                    f"на кошельке {ton_bal:.3f} TON: после газа {buy_gas} + резерва "
                    f"{reserve} TON остаётся {spendable:.3f} < мин. ставки "
                    f"{Config.MIN_STAKE_TON} TON"
                )
                self.log(f"⛔ Недостаточно средств для BUY: {why}. Сделка отменена.", "WARN")
                return False
            if stake > spendable:
                self.log(
                    f"✂️ Ставка урезана {stake:.3f} → {spendable:.3f} TON, "
                    f"чтобы всегда осталось на комиссию (резерв {reserve} TON)", "INFO"
                )
                stake = spendable
            ton_stake = stake
            amount = stake / price
        else:
            amount = stake / price

        order = self.exchange.place_order(side, amount, ton_stake=ton_stake)
        if not order:
            self.log("⚠️ BUY ордер не исполнен — пропускаем", "WARN")
            return False

        # В DeDust-режиме используем реальное кол-во GRINCH из подтверждённого свопа,
        # а не расчётное (stake_ton / usd_price), которое даёт неверные единицы измерения.
        if self.exchange.mode == "dedust":
            actual_grinch = (order.get("info") or {}).get("grinch_received", 0)
            if actual_grinch and actual_grinch > 0:
                self.log(
                    f"✅ Реальный GRINCH получен: {actual_grinch:.4f} "
                    f"(расч. по USD-цене было бы {stake / price:.4f})", "INFO"
                )
                amount = actual_grinch

        sl_pct, tp_pct = self._targets(price, ai)
        # Режим «только в плюс»: без убыточного стопа (sl=0, вниз не сработает) —
        # выходим лишь по TP или трейлингу от безубытка.
        sl = 0.0 if Config.ONLY_PROFIT_EXIT else self.exchange._round(price * (1 - sl_pct / 100))
        tp = self.exchange._round(price * (1 + tp_pct / 100))

        trade = {
            "id":            order["id"],
            "symbol":        Config.SYMBOL,
            "side":          side,
            "entry_price":   price,
            "amount":        round(amount, 6),
            "stake_ton":     round(stake, 4),
            "stop_loss":     sl,
            "take_profit":   tp,
            "trail_pct":     Config.TRAILING_STOP_PCT,
            "high_water":    price,
            "opened_at":     datetime.utcnow().isoformat(),
            "pnl":           0.0,
            "status":        "open",
            "ai_confidence": ai_conf,
        }
        self.open_trades.append(trade)
        self.trades.append(dict(trade))
        self.stats["total_trades"] += 1
        # АВТО-СОХРАНЕНИЕ: цена покупки + цель продажи на диск, чтобы после
        # перезапуска бот знал почём купил и не продал дешевле.
        try:
            self.exp.save_open_trades(self.open_trades)
        except Exception as e:  # noqa: BLE001
            self.log(f"Сохранение позиции: {e}", "WARN")
        self.log(
            f"🟢 BUY @ {price} | {stake:.3f} TON | SL={sl}(-{sl_pct:.1f}%) | "
            f"TP={tp}(+{tp_pct:.1f}%) | AI={ai_conf}%", "BUY"
        )
        return True

    def _close_all_trades(self, price, analysis):
        relevant_before = self._relevant_open()
        net_floor_pct = Config.required_gross_pct()
        for trade in list(relevant_before):
            # Режим «только в плюс»: AI-сигнал SELL игнорируется, если позиция
            # ещё не достигла минимальной нетто-прибыли. Продаём только в плюс.
            if Config.ONLY_PROFIT_EXIT:
                entry = trade["entry_price"]
                pnl_pct = (price - entry) / entry * 100 if entry else 0.0
                if pnl_pct < net_floor_pct:
                    self.log(
                        f"⏸️ SELL-сигнал отклонён: прибыль {pnl_pct:+.1f}% < "
                        f"мин. +{net_floor_pct:.0f}% (режим «только в плюс»). Держим.",
                        "INFO"
                    )
                    continue
            self._close_trade(trade, price, "signal")
        # Сигнал SELL юзерам безопасен ТОЛЬКО когда были позиции и ВСЕ они
        # реально закрылись. При частичном закрытии (одна продажа прошла, другая
        # нет) grinch_held обнулять нельзя — часть реального GRINCH ещё не продана.
        return bool(relevant_before) and not self._relevant_open()

    def _check_stop_loss_take_profit(self, price):
        had_relevant = bool(self._relevant_open())
        closed_any   = False
        for trade in list(self.open_trades):
            if trade.get("symbol", Config.SYMBOL) != Config.SYMBOL:
                continue

            entry      = trade["entry_price"]
            profit_pct = (price - entry) / entry * 100

            # Минимальный нетто-пол прибыли (в gross %): нетто-цель + комиссия
            # цикла. Любой выход обязан быть НЕ НИЖЕ этого уровня → гарантируем
            # ≥TARGET_NET_PCT нетто после комиссии обеих ног.
            net_floor_pct = Config.required_gross_pct()
            floor_price   = self.exchange._round(entry * (1 + net_floor_pct / 100))

            if Config.ONLY_PROFIT_EXIT:
                # ── Режим «только в плюс, минимум N% нетто» ──────────────────
                # «Взведённый» стоп = трейлинг уже активирован (стоп ≥ floor_price).
                # До взведения стоп = 0 и вниз не срабатывает (держим позицию,
                # никакого стоп-лосса в убыток не существует).
                armed = trade["stop_loss"] > 0

                # Прибыль достигла пола → взводим/подтягиваем трейлинг. Стоп
                # НИКОГДА не опускается ниже floor_price (гарантия +N% нетто).
                if profit_pct >= net_floor_pct:
                    if price > trade.get("high_water", entry):
                        trade["high_water"] = price
                    high_water = trade.get("high_water", entry)

                    new_sl = self.exchange._round(high_water * (1 - Config.TRAIL_STAGE4_PCT / 100))
                    new_sl = max(new_sl, floor_price)    # пол ≥ +N% нетто

                    if new_sl > trade["stop_loss"]:
                        old_sl = trade["stop_loss"]
                        trade["stop_loss"] = new_sl
                        self.log(
                            f"🔼 Стоп: {old_sl} → {new_sl} | прибыль {profit_pct:+.1f}% | "
                            f"трейл {Config.TRAIL_STAGE4_PCT}% (пол +{net_floor_pct:.0f}% нетто)",
                            "INFO"
                        )
                    armed = True

                # Если стоп взведён — проверяем выход КАЖДЫЙ тик (даже если цена
                # уже просела ниже пола): иначе зафиксированную прибыль можно
                # «забыть» снять. Стоп всегда ≥ floor_price → выход в плюс.
                if armed and price <= trade["stop_loss"]:
                    if self._close_trade(trade, price, "take_profit"):
                        closed_any = True
                continue

            # ── Классический режим (SL/TP, трейлинг с безубытком) ───────────
            if profit_pct >= Config.TRAIL_STAGE4_AT:
                trail_pct = Config.TRAIL_STAGE4_PCT
            elif profit_pct >= Config.TRAIL_STAGE3_AT:
                trail_pct = Config.TRAIL_STAGE3_PCT
            elif profit_pct >= Config.TRAIL_STAGE2_AT:
                trail_pct = Config.TRAIL_STAGE2_PCT
            else:
                trail_pct = Config.TRAILING_STOP_PCT   # начальный (7%)

            if price > trade.get("high_water", entry):
                trade["high_water"] = price
            high_water = trade.get("high_water", entry)

            new_sl = self.exchange._round(high_water * (1 - trail_pct / 100))

            if profit_pct >= Config.TRAIL_BREAKEVEN_AT:
                breakeven_sl = self.exchange._round(entry * (1 + Config.FEE_ROUND_TRIP / 100))
                new_sl = max(new_sl, breakeven_sl)

            if new_sl > trade["stop_loss"]:
                old_sl = trade["stop_loss"]
                trade["stop_loss"] = new_sl
                stage_label = (
                    f"≥{Config.TRAIL_STAGE4_AT:.0f}% (trail {trail_pct}%)" if profit_pct >= Config.TRAIL_STAGE4_AT else
                    f"≥{Config.TRAIL_STAGE3_AT:.0f}% (trail {trail_pct}%)" if profit_pct >= Config.TRAIL_STAGE3_AT else
                    f"≥{Config.TRAIL_STAGE2_AT:.0f}% (trail {trail_pct}%)" if profit_pct >= Config.TRAIL_STAGE2_AT else
                    f"≥{Config.TRAIL_BREAKEVEN_AT:.0f}% → безубыток" if profit_pct >= Config.TRAIL_BREAKEVEN_AT else
                    f"trail {trail_pct}%"
                )
                self.log(
                    f"🔼 Стоп: {old_sl} → {new_sl} | "
                    f"прибыль {profit_pct:+.1f}% | {stage_label}", "INFO"
                )

            if price <= trade["stop_loss"]:
                reason = "trailing_stop" if trade["stop_loss"] > entry else "stop_loss"
                if self._close_trade(trade, price, reason):
                    closed_any = True
            elif price >= trade["take_profit"]:
                if self._close_trade(trade, price, "take_profit"):
                    closed_any = True

        # Если SL/TP реально закрыл позиции и больше открытых нет — сводим
        # виртуальное состояние юзеров (обнуляем grinch_held, разблокируем вывод).
        # Только при полном закрытии: частичное оставляет реальный GRINCH в рынке.
        if closed_any and had_relevant and not self._relevant_open():
            self._emit_signal("SELL", price, self.last_ai)
            return True
        return False

    def close_trade(self, trade_id):
        """Ручное закрытие ОДНОЙ позиции по её id (рыночная продажа сейчас)."""
        trade = next((t for t in self.open_trades
                      if str(t.get("id")) == str(trade_id)), None)
        if not trade:
            return {"ok": False, "error": "Позиция не найдена или уже закрыта"}
        try:
            from price_feed import price_feed
            price = price_feed.get("GRINCH") or trade.get("entry_price")
        except Exception:
            price = trade.get("entry_price")
        # Режим «только в плюс»: даже РУЧНОЕ закрытие не продаёт в минус.
        # Если позиция ещё не достигла минимальной нетто-прибыли — отказываем
        # и держим, пока цена вырастет (та же гарантия, что и для авто-выхода).
        if Config.ONLY_PROFIT_EXIT:
            entry = trade.get("entry_price") or 0
            pnl_pct = (price - entry) / entry * 100 if entry else 0.0
            net_floor_pct = Config.required_gross_pct()
            if pnl_pct < net_floor_pct:
                self.log(
                    f"⏸️ Ручная продажа отклонена: прибыль {pnl_pct:+.1f}% < "
                    f"мин. +{net_floor_pct:.0f}% (режим «только в плюс»). Держим.",
                    "INFO"
                )
                return {"ok": False, "error": (
                    f"Продажа в минус отключена: прибыль {pnl_pct:+.1f}% ниже "
                    f"минимума +{net_floor_pct:.0f}%. Ждём роста цены.")}
        self.log(f"🖐 Ручное закрытие позиции {trade_id} @ {price}", "INFO")
        ok = self._close_trade(trade, price, "manual")
        return {"ok": True} if ok else {
            "ok": False, "error": "Продажа не исполнена — попробуйте ещё раз позже"}

    def _close_trade(self, trade, price, reason):
        """Сериализует закрытие (лок) и защищает от двойной продажи позиции."""
        with self._close_lock:
            if trade.get("id") not in {t.get("id") for t in self.open_trades}:
                return False   # уже закрыта другим потоком
            return self._close_trade_locked(trade, price, reason)

    def _close_trade_locked(self, trade, price, reason):
        """
        Закрывает позицию:
        1. Исполняет реальную продажу GRINCH на блокчейне (DeDust режим)
        2. Рассчитывает виртуальный P&L
        3. Обновляет статистику и AI feedback
        """
        # ── 1. РЕАЛЬНАЯ продажа GRINCH через DeDust ─────────────────────
        if self.exchange.mode == "dedust":
            grinch_amount = trade.get("amount", 0)
            if grinch_amount > 0:
                self.log(
                    f"💸 Продаём {grinch_amount:.6f} GRINCH на DeDust "
                    f"(причина: {reason})...", "INFO"
                )
                sell_ok = False
                try:
                    sell_result = self.exchange.place_order("sell", grinch_amount)
                    if sell_result and not sell_result.get("error"):
                        sell_ok = True
                        self.log(
                            f"✅ Продажа GRINCH → TON исполнена | "
                            f"id={sell_result.get('id', '—')}", "INFO"
                        )
                    else:
                        err = sell_result.get("error", "нет ответа") if sell_result else "нет ответа"
                        self.log(f"⚠️ Продажа не исполнена: {err}", "WARN")
                except Exception as e:
                    self.log(f"⚠️ Ошибка продажи GRINCH: {e}", "WARN")

                if not sell_ok:
                    # Реальная продажа не прошла — НЕ закрываем позицию виртуально.
                    # Оставляем её открытой и повторим продажу на следующем тике.
                    # Так виртуальное состояние юзеров остаётся синхронным с реальными
                    # активами (grinch_held>0 → вывод заблокирован, пока не продадим).
                    self.log("⏳ Позиция остаётся открытой — повтор продажи позже", "WARN")
                    return False

        # ── 2. Виртуальный P&L ───────────────────────────────────────────
        gross = (price - trade["entry_price"]) * trade["amount"]
        # Комиссия обеих ног DeDust: FEE_PCT за вход + FEE_PCT за выход
        fee   = (trade["entry_price"] + price) * trade["amount"] * Config.FEE_PCT / 100
        pnl_raw = gross - fee

        # В DeDust-режиме цены в USD → конвертируем P&L в TON для корректного отображения
        if self.exchange.mode == "dedust":
            from price_feed import price_feed
            ton_usd = price_feed.get("TON") or 2.44
            pnl = round(pnl_raw / ton_usd, 6)
        else:
            pnl = round(pnl_raw, 6)

        trade["pnl"]          = pnl
        trade["fee"]          = round(fee, 6)
        trade["exit_price"]   = price
        trade["closed_at"]    = datetime.utcnow().isoformat()
        trade["close_reason"] = reason
        trade["status"]       = "closed"

        self.stats["total_pnl"] = round(self.stats["total_pnl"] + pnl, 6)
        if pnl > 0:
            self.stats["winning_trades"] += 1

        self.open_trades = [t for t in self.open_trades if t["id"] != trade["id"]]
        for t in self.trades:
            if t["id"] == trade["id"]:
                t.update(trade)
                break

        # ── 3. AI feedback: самообучение ─────────────────────────────────
        try:
            outcome = "win" if pnl > 0 else "loss"
            self.ai.feedback(outcome=outcome, pnl=float(pnl))
            self.log(f"🧠 AI feedback: {outcome} PNL={pnl:+.6f} TON", "INFO")
        except Exception as e:
            self.log(f"AI feedback ошибка: {e}", "WARN")

        # ── 4. Память + само-управление ИИ ───────────────────────────────
        try:
            self.exp.save_open_trades(self.open_trades)   # позиция закрыта → обновляем диск
            self.exp.record_trade(trade, self.stats, self.ai)
            from price_feed import price_feed
            self.exp.record_balance(
                self._get_balance_cached(),
                price_feed.get("GRINCH") or price,
                force=True,
            )
            self.exp.analyze_and_adapt(self, self.ai)
        except Exception as e:
            self.log(f"Память/адаптация: {e}", "WARN")

        emoji = "🟩" if pnl >= 0 else "🟥"
        self.log(
            f"{emoji} Закрыто @ {price} | PNL={pnl:+.6f} TON | {reason}", 
            "SELL" if pnl >= 0 else "ERROR"
        )
        return True

    # ──────────────────────────────────────────
    # Статус
    # ──────────────────────────────────────────
    def _get_balance_cached(self) -> dict:
        """Возвращает кешированный баланс (TTL 30 сек) — не тратим RTT к блокчейну на каждый poll."""
        now = time.time()
        if now - self._balance_cache_ts < self._balance_cache_ttl and self._balance_cache:
            return self._balance_cache
        bal = self.exchange.get_balance()
        if bal and not bal.get("error"):
            self._balance_cache    = bal
            self._balance_cache_ts = now
        return bal

    def _enriched_open_trades(self, grinch_ton):
        """Открытые сделки + расчёт «если продать сейчас» с учётом ОБЕИХ
        транзакций. Комиссия покупки уже сидит в полученном `amount` (пул
        списал 1% при входе), здесь дополнительно вычитаем комиссию продажи
        (1%) и газ — так пользователь видит реальную чистую прибыль в GRAM."""
        out = []
        fee     = Config.FEE_PCT / 100.0
        gas     = Config.SELL_GAS_TON
        cur_ton = grinch_ton or 0
        for t in self.open_trades:
            c = dict(t)
            amount    = t.get("amount", 0) or 0
            stake_ton = t.get("stake_ton", 0) or 0
            entry_usd = t.get("entry_price", 0) or 0
            if cur_ton > 0 and amount > 0 and stake_ton > 0:
                value_now = amount * cur_ton              # текущая стоимость в TON
                proceeds  = value_now * (1 - fee) - gas   # минус комиссия продажи + газ
                net_ton   = proceeds - stake_ton          # против потраченного на покупку
                c["value_ton_now"] = round(value_now, 6)
                c["net_ton_now"]   = round(net_ton, 6)
                c["net_pct_now"]   = round(net_ton / stake_ton * 100, 2)
                c["in_profit"]     = bool(net_ton > 0)
                # Безубыточная цена за GRINCH (где net=0), в USD для карточки
                entry_ton = stake_ton / amount
                if entry_ton > 0 and entry_usd > 0:
                    be_ton = (stake_ton + gas) / (amount * (1 - fee))
                    c["breakeven_price"] = round(entry_usd * be_ton / entry_ton, 8)
            out.append(c)
        return out

    def get_status(self):
        ohlcv    = self.exchange.get_ohlcv(limit=100)
        analysis = analyze(ohlcv)
        # Единый источник «текущей цены» для всего UI: спотовая цена DexScreener
        # (price_feed.get), та же, что использует авто-ликвидатор и карточка монеты.
        # Иначе hero/кошелёк показывают close последней свечи (GeckoTerminal), а
        # ликвидатор — спот, и числа расходятся (~1%). Свечи для графика/индикаторов
        # не трогаем — меняем только отображаемую цену.
        grinch_ton = None
        try:
            from price_feed import price_feed
            spot = price_feed.get("GRINCH")
            if spot and spot > 0:
                analysis["price"] = spot
            # Курс 1 GRINCH в GRAM (TON) — реальный курс пула (priceNative)
            grinch_ton = price_feed.get_grinch_ton_price()
        except Exception:
            pass
        ai       = self.last_ai if self.last_ai else self.ai.analyze(ohlcv)
        balance  = self._get_balance_cached()
        winrate  = 0
        if self.stats["total_trades"] > 0:
            winrate = round(self.stats["winning_trades"] / self.stats["total_trades"] * 100, 1)
        return {
            "running":       self.running,
            "training":      self.training,
            "demo_mode":     self.exchange.demo_mode,
            "symbol":        Config.SYMBOL,
            "grinch_ton":    grinch_ton,
            "balance":       balance,
            "analysis":      analysis,
            "ai":            ai,
            "open_trades":   self._enriched_open_trades(grinch_ton),
            "recent_trades": self.trades[-20:],
            "logs":          self.logs[-50:],
            "stats":         {**self.stats, "winrate": winrate},
            "training_progress": self.ai.training_progress,
        }
