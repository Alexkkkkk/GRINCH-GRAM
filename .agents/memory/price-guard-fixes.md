---
name: Price zero-division guards
description: Where price/entry_usd guards were added to prevent ZeroDivisionError crashes in trading functions.
---

## Rule
Any function that divides by `price` or `entry_usd` must guard against zero/None BEFORE the division.

## Locations patched (August 2026)
- `trader.py` `_open_trade`: guard before `amount = stake / price` → `if not price or price <= 0: return False`
- `trader.py` `_check_short_positions`: `entry_usd = trade.get("entry_price", 0)` + `if not entry_usd: continue`
- `trader.py` `_close_short_trade`: guard block before DeDust/demo branches → falls back to `entry_price`, returns False if both zero

## Why
Short-position logic had three unchecked `/price` divisions (lines ~3821, 3833, 3837) and `_open_trade` had two (lines ~3474, 3476). If price feed returns 0 (API glitch) the bot would crash the trading tick silently.

## How to apply
Before any new function that divides by price: add `if not price or price <= 0: return False/log+return` at the top.
