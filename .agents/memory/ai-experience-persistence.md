---
name: AI experience persistence & self-management
description: How the bot persists AI learning across restarts and self-tunes trading params; why "AI edits code" means parameter adaptation, not source rewriting.
---

# AI experience persistence & self-management

`experience_manager.py` (singleton `experience_manager`) owns `experience.json`:
trade journal, equity/balance curve, trader stats, AI confirmed-experience export,
and adaptive `control` params. Atomic temp-file write under an RLock.

- The "AI" is a **local sklearn ensemble** (`ai_engine.py`), NOT an LLM. Its learning
  lived only in-memory (`_confirmed_X/y/w`, slot accuracy) and was **wiped on every
  restart** — that was the core gap. Fixed via `ai_engine.export_experience()` /
  `import_experience()` (numpy↔JSON, guarded by feature-dim match; refits on load).
  `import_experience` must run AFTER `pretrain` (needs `_feature_names`).

**"Программа правит код для управления" = adaptive parameter tuning, NOT literal
source rewriting.** `analyze_and_adapt()` reads loss-streak / recent net PnL /
drawdown-from-peak and mutates `Config.MIN_AI_CONFIDENCE` (stricter after losses),
`Config.TRADE_AMOUNT` (smaller on drawdown), and a drawdown **pause** flag with
hysteresis (pause ≥30% DD, resume ≤15%). Trader's BUY gate checks `exp.is_paused()`.
**Why:** letting an AI rewrite its own trading source is dangerous; param adaptation
gives the requested "self-management" safely.

**Two invariants to keep:**
- Manual config changes (`/api/config`) must call `experience_manager.set_baseline()`
  for any changed `min_ai_confidence`/`trade_amount`, or adaptation drags the value
  back toward a stale baseline.
- `record_balance()` must **skip** the sample when GRINCH is held but the TON USD
  quote (or GRINCH price) is unavailable — otherwise equity collapses to TON-only,
  fabricating a huge drawdown and a false trading pause.

Read-only state exposed at `GET /api/experience`.
