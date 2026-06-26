---
name: Manual per-trade close
description: Why manual single-position close must not emit a global SELL signal, and how double-sell is prevented.
---
Manual close of ONE open platform position (UI "Продать сейчас" button → POST /api/trade/close → trader.close_trade(id)) calls _close_trade directly and intentionally does NOT call _emit_signal("SELL").

**Why:** _emit_signal fans out to signal_callbacks (user_mgr.on_signal), which makes EVERY custodial user with grinch_held>0 do a virtual sell. A manual close is the owner liquidating one platform position only — emitting a global SELL would force-liquidate all users' independent positions. Platform open_trades are separate from users' virtual grinch_held accounting; selling the platform position's GRINCH amount on-chain does not touch user virtual balances.

**How to apply:** Any future "close N positions" / partial-close feature must stay on the _close_trade path without _emit_signal. Only the autonomous trading loop (_tick) emits BUY/SELL for user sync.

Double-sell guard: _close_trade is a thin wrapper holding self._close_lock that re-checks the trade id is still in open_trades before delegating to _close_trade_locked (the real body). This stops the trading loop and a manual close from selling the same position twice. DeDust swaps are also serialized by dedust_client._lock.
