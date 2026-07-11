---
name: New read-only engines (Block 2) added to trading bot
description: Design constraints followed when adding backtest/paper-trading/LLM-agent/RAG/explainability/alerts/multi-agent engines around the existing bot.
---

Seven new engines (`backtest.py`, `paper_trading.py`, `llm_agent.py`, `rag_context.py`,
`explainability.py`, `alert_rules.py`, `multi_agent.py`) were added alongside the existing
bot without touching `trader.py`, `brain_fusion.py`, or `ai_engine.py`.

**Why:** `ai_engine.py` (QuantumBrain) is a stateful singleton — replaying historical data
through its `analyze()`/`_refit_all()` would retrain and mutate the SAME models used for
live trading, corrupting production. `brain_fusion.py` similarly drives real trade decisions.
New experimental/analytical engines must stay read-only and use only `strategy.py`
(stateless indicator functions) as their shared foundation — never the AI singleton or
BrainFusion — unless a future task explicitly designs an isolated, non-singleton model copy
for walk-forward experiments.

**How to apply:** Any future addition to this family (e.g. wiring AI predictions into
backtest/paper-trading) needs its own separate model instance trained only on data before
the simulated point in time, never the live singleton.

## Paper trading dashboard buttons (11.07.2026)

Added a "web" workflow (`python3 main.py`, port 5000) to run the dashboard on Replit —
previously deliberately not run, back when `EXTERNAL_DATABASE_URL` still pointed at the
VPS prod DB. Now safe: DB is separated (see db separation note) and the manual trading
toggle defaults OFF + no `TON_MNEMONIC` on Replit, so the live trade loop can run without
risk of real orders. Paper trading itself uses two isolated per-profile state files
(`paper_trading_state_<profile>.json`, gitignored) — "standard" (grade B+, full TP target)
and "aggressive" (grade C+, half TP target = faster/smaller exits), driven by dashboard
buttons calling `/api/paper/tick|status|reset?profile=`.

