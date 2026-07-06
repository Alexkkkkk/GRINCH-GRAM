---
name: wallet-manager
description: Full wallet tracking for TON+GRINCH — architecture, DB table, API endpoints, JS patterns.
---

# WalletManager — Полное отслеживание кошелька

## What it does
- `wallet_manager.py` — background daemon thread (30s poll), reads real balance from `dedust_client`, entry price from `trader.open_trades`, computes P&L with fees+gas, saves to `bot_wallet_snapshots`.
- DB table: `bot_wallet_snapshots` in `db_store.py` — all columns defined, auto-pruned to WALLET_SNAP_KEEP=5000 rows.
- API endpoints in `app.py`: `/api/wallet/full`, `/api/wallet/snapshot`, `/api/wallet/history`, `/api/wallet/analytics`, `/api/wallet/refresh`
- AI advisor `_build_snapshot()` now includes `snap["position"]` with entry_price_ton, pnl_ton, pnl_pct from wallet_manager.

## Critical architecture decisions

**Why:** wallet tracking required full position P&L, DB-backed history, and AI advisor context — not just live balance.

**How to apply:**
- `wallet_manager.start(trader_ref=trader)` must be called at module import time, NOT in `__main__`.
- `_poll_lock` (non-blocking acquire) prevents concurrent background + manual refresh poll races.
- JS dollar signs in HTML templates MUST use unicode escape `\u0024` or the helper `usdStr()` / `usdStr8()` because `$'` sequences get corrupted when edit tools process single-quoted JS strings.
- P&L cost model: `cost = total_stake + buy_gas * n_entries` (n_entries estimated from DCA_STAKE_TON). Use this consistently across `_poll_body()` and `get_full_status()`.
- `fmtUsd()` in the wallet JS uses `\u2248 \u0024` not literal `≈ $` — same corruption protection.

## Invariants that must NOT change
- `ONLY_PROFIT_EXIT` is never touched by wallet_manager or its endpoints.
- All wallet endpoints are read-only (no trade execution).
- `EXTERNAL_DATABASE_URL` priority over `DATABASE_URL` remains intact (not touched).
