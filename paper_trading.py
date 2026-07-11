"""
paper_trading.py — Движок пейпер-трейдинга (виртуальная торговля без денег).

Назначение
----------
Прогоняет ту же техническую стратегию, что и backtest.py/трейдер, на СВЕЖИХ
живых свечах GeckoTerminal, но НЕ исполняет реальные ордера и НЕ трогает
`bot_trades`/`bot_open_trades` (боевые таблицы). Состояние виртуальной позиции
и история виртуальных сделок сохраняются в отдельный JSON-файл —
`paper_trading_state.json` (по аналогии с settings_store.py: файл, не БД,
чтобы не создавать риск для боевой схемы).

Зачем это нужно
---------------
Позволяет обкатывать новую версию стратегии/параметров НА РЕАЛЬНЫХ живых
данных параллельно с боевым ботом, прежде чем доверить ей реальные деньги —
безопасный шаг между backtest (прошлое) и реальной торговлей (будущее).

Использование
-------------
    python3 paper_trading.py --tick --profile standard        # один шаг для одного профиля
    python3 paper_trading.py --tick --all                      # шаг СРАЗУ для всех профилей (общий момент рынка)
    python3 paper_trading.py --status --all                     # статус всех профилей
    python3 paper_trading.py --loop --all --interval 60          # непрерывный цикл для всех сразу
    python3 paper_trading.py --capital 50 --profile standard    # задать сумму 50 TON для профиля (сброс портфеля)
    python3 paper_trading.py --reset --profile aggressive       # сбросить конкретный портфель

Дашборд (app.py) даёт то же самое кнопками: у каждого профиля есть поле
суммы (TON) и кнопка «Тик», плюс общая кнопка «Тикнуть оба одновременно».
"""

from __future__ import annotations

import argparse
import json
import os
import time

from config import Config
import strategy
from exchange import ExchangeClient
from backtest import _trail_pct_for_gain

_STATE_DIR = os.path.dirname(__file__)

# ── Профили торговли ──────────────────────────────────────────────────────
# "standard"   — как боевая стратегия: грейд входа B+, полный тейк-профит
#                 (Config.required_gross_pct()), штатный трейлинг.
# "aggressive" — заходит на более слабых сигналах (грейд C+) и фиксирует
#                 прибыль в 2 раза быстрее (половина обычной цели) — больше
#                 сделок, каждая мельче. Это НЕ влияет на боевые настройки —
#                 отдельный виртуальный портфель с собственным состоянием.
PROFILES = {
    "standard":   {"min_quality": "B", "target_mult": 1.0, "label": "Стандартный"},
    "aggressive": {"min_quality": "C", "target_mult": 0.5, "label": "Агрессивный"},
}


def _state_path(profile: str) -> str:
    return os.path.join(_STATE_DIR, f"paper_trading_state_{profile}.json")


STATE_PATH = _state_path("standard")  # обратная совместимость для прямых импортов

DEFAULT_CAPITAL_TON = 100.0

_DEFAULT_STATE = {
    "equity": 1.0,             # множитель к capital_ton (1.0 = без изменений)
    "peak_equity": 1.0,
    "max_drawdown_pct": 0.0,
    "position": None,   # {"entry_ts","entry_price","peak_gain_pct"}
    "trades": [],        # история закрытых виртуальных сделок
    "last_bar_ts": None,  # защита от повторной обработки одного и того же бара
    "started_at": None,
    "capital_ton": DEFAULT_CAPITAL_TON,   # виртуальная сумма, которой профиль "торгует"
}


def _load_state(profile: str = "standard") -> dict:
    path = _state_path(profile)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return {**_DEFAULT_STATE, **json.load(f)}
        except Exception:
            pass
    state = dict(_DEFAULT_STATE)
    state["started_at"] = time.time()
    return state


def _save_state(state: dict, profile: str = "standard") -> None:
    path = _state_path(profile)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)   # атомарная замена — без частичных записей


def step(state: dict, ohlcv: list, min_quality: str = "B", target_mult: float = 1.0) -> dict:
    """Один шаг симуляции на самой свежей свече из `ohlcv`.
    Идемпотентно: если last_bar_ts не изменился с прошлого вызова — не
    делает ничего (защита от повторного тика на той же свече)."""
    if not ohlcv:
        return state

    bar_ts = ohlcv[-1][0]
    close_price = ohlcv[-1][4]

    if state.get("last_bar_ts") == bar_ts:
        return state  # тот же бар — уже обработан
    state["last_bar_ts"] = bar_ts

    quality_rank = {"A": 3, "B": 2, "C": 1, "": 0}
    min_rank = quality_rank.get(min_quality, 2)
    required_gross = Config.required_gross_pct() / 100.0 * target_mult

    position = state.get("position")

    if position is None:
        try:
            analysis = strategy.analyze(ohlcv)
        except Exception as e:
            print(f"[Paper] strategy.analyze error: {e}")
            return state
        signal = (analysis or {}).get("signal")
        quality = (analysis or {}).get("entry_quality", "")
        if signal == "BUY" and quality_rank.get(quality, 0) >= min_rank:
            state["position"] = {
                "entry_ts": bar_ts,
                "entry_price": close_price,
                "peak_gain_pct": 0.0,
            }
            print(f"[Paper] Виртуальный вход по {close_price} (grade={quality})")
    else:
        gain_pct = (close_price - position["entry_price"]) / position["entry_price"] * 100.0
        position["peak_gain_pct"] = max(position["peak_gain_pct"], gain_pct)
        exit_reason = None

        if gain_pct >= required_gross * 100.0:
            exit_reason = "take_profit"

        trail_pct = _trail_pct_for_gain(position["peak_gain_pct"])
        if exit_reason is None and trail_pct is not None:
            drop_from_peak = position["peak_gain_pct"] - gain_pct
            if drop_from_peak >= trail_pct:
                exit_reason = "trailing_stop"

        if exit_reason is not None:
            net_pct = gain_pct - Config.FEE_ROUND_TRIP
            if net_pct <= 0 and Config.ONLY_PROFIT_EXIT:
                exit_reason = None

        if exit_reason:
            net_pct = gain_pct - Config.FEE_ROUND_TRIP
            state["equity"] *= (1.0 + net_pct / 100.0)
            state["trades"].append({
                "entry_ts": position["entry_ts"],
                "entry_price": position["entry_price"],
                "exit_ts": bar_ts,
                "exit_price": close_price,
                "exit_reason": exit_reason,
                "net_pct": round(net_pct, 3),
            })
            print(f"[Paper] Виртуальный выход по {close_price} ({exit_reason}, net={net_pct:.2f}%)")
            state["position"] = None

    state["peak_equity"] = max(state.get("peak_equity", 1.0), state["equity"])
    dd = (state["peak_equity"] - state["equity"]) / state["peak_equity"] * 100.0 if state["peak_equity"] > 0 else 0.0
    state["max_drawdown_pct"] = max(state.get("max_drawdown_pct", 0.0), dd)
    return state


def status_summary(state: dict, profile: str = "standard") -> dict:
    trades = state.get("trades", [])
    wins = [t["net_pct"] for t in trades if t["net_pct"] > 0]
    capital = float(state.get("capital_ton", DEFAULT_CAPITAL_TON))
    equity_ton = capital * state.get("equity", 1.0)
    return {
        "profile": profile,
        "label": PROFILES.get(profile, {}).get("label", profile),
        "capital_ton": round(capital, 4),
        "equity_ton": round(equity_ton, 4),
        "pnl_ton": round(equity_ton - capital, 4),
        "equity_multiplier": round(state.get("equity", 1.0), 4),
        "total_return_pct": round((state.get("equity", 1.0) - 1.0) * 100, 3),
        "max_drawdown_pct": round(state.get("max_drawdown_pct", 0.0), 3),
        "n_trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "open_position": state.get("position"),
        "last_bar_ts": state.get("last_bar_ts"),
    }


def run_tick(profile: str = "standard", ohlcv: list = None) -> dict:
    """Один тик для указанного профиля — используется и CLI, и Flask API
    (app.py: /api/paper/tick). `ohlcv` можно передать заранее (для
    tick_all, чтобы не дёргать API дважды за один and тот же момент).
    Возвращает summary через status_summary()."""
    cfg = PROFILES.get(profile, PROFILES["standard"])
    state = _load_state(profile)
    if ohlcv is None:
        exchange = ExchangeClient()
        ohlcv = exchange.get_real_ohlcv(limit=150, tf="hour", aggregate=1)
    if not ohlcv:
        return status_summary(state, profile)
    state = step(state, ohlcv, min_quality=cfg["min_quality"], target_mult=cfg["target_mult"])
    _save_state(state, profile)
    return status_summary(state, profile)


def tick_all() -> dict:
    """Тикает ВСЕ профили одновременно на одних и тех же свежих свечах —
    честное сравнение, оба видят один и тот же рыночный момент."""
    exchange = ExchangeClient()
    ohlcv = exchange.get_real_ohlcv(limit=150, tf="hour", aggregate=1)
    return {profile: run_tick(profile, ohlcv=ohlcv) for profile in PROFILES}


def reset_profile(profile: str = "standard") -> None:
    path = _state_path(profile)
    if os.path.exists(path):
        os.remove(path)


def set_capital(profile: str, capital_ton: float) -> dict:
    """Задать сумму, которой торгует профиль. Начинает портфель заново с
    этой суммой (свежий старт — иначе смена суммы задним числом искажала
    бы историю доходности в %)."""
    capital_ton = max(0.01, float(capital_ton))
    reset_profile(profile)
    state = _load_state(profile)
    state["capital_ton"] = capital_ton
    _save_state(state, profile)
    return status_summary(state, profile)


def main():
    parser = argparse.ArgumentParser(description="Пейпер-трейдинг GRINCH/TON (виртуальный, без реальных денег)")
    parser.add_argument("--tick", action="store_true", help="Один шаг: получить свежую свечу и решить")
    parser.add_argument("--status", action="store_true", help="Показать текущее виртуальное состояние")
    parser.add_argument("--loop", action="store_true", help="Непрерывный цикл")
    parser.add_argument("--interval", type=int, default=60, help="Интервал между тиками в секундах (для --loop)")
    parser.add_argument("--profile", type=str, default="standard", choices=list(PROFILES.keys()))
    parser.add_argument("--reset", action="store_true", help="Сбросить виртуальный портфель")
    parser.add_argument("--capital", type=float, default=None, help="Задать сумму (TON), которой торгует профиль (сбрасывает портфель)")
    parser.add_argument("--all", action="store_true", help="Применить --tick/--loop сразу ко ВСЕМ профилям одновременно")
    args = parser.parse_args()

    if args.capital is not None:
        summary = set_capital(args.profile, args.capital)
        print(f"[Paper] Профиль '{args.profile}' теперь торгует суммой {args.capital} TON")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    if args.reset:
        reset_profile(args.profile)
        print(f"[Paper] Состояние профиля '{args.profile}' сброшено")
        return

    if args.status:
        if args.all:
            for p in PROFILES:
                print(json.dumps(status_summary(_load_state(p), p), ensure_ascii=False))
        else:
            state = _load_state(args.profile)
            print(json.dumps(status_summary(state, args.profile), ensure_ascii=False, indent=2))
        return

    if args.loop:
        target = "ВСЕ профили одновременно" if args.all else f"профиль '{args.profile}'"
        print(f"[Paper] Запуск цикла ({target}), интервал={args.interval}с. Ctrl+C для остановки.")
        try:
            while True:
                if args.all:
                    print(json.dumps(tick_all(), ensure_ascii=False))
                else:
                    print(json.dumps(run_tick(args.profile), ensure_ascii=False))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("[Paper] Остановлено пользователем")
    elif args.tick:
        if args.all:
            print(json.dumps(tick_all(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(run_tick(args.profile), ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
