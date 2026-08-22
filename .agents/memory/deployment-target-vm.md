---
name: Deployment target must be VM, not autoscale
description: Why this trading bot's [deployment] section in .replit uses deploymentTarget="vm" and a threaded gunicorn command, not the default "autoscale".
---

This app keeps critical state in server process memory: the trading loop
(`trader.py`), the AI engine's in-memory model state (`ai_engine.py`), wallet
tracking, deposit monitoring, and the custodial user-balance manager. All of
these run as background threads started at import time.

**Why not autoscale:** autoscale can scale to zero when idle (killing the
trading loop entirely, with no one polling positions/stop-losses) and can run
multiple concurrent instances under load (each instance would run its own
independent copy of the trader loop against the same on-chain wallet/DB —
duplicate or conflicting trades, double-spending custodial funds). Neither
behavior is acceptable for a bot placing real on-chain trades.

**How to apply:** `deploymentTarget` in `.replit`'s `[deployment]` section
must stay `"vm"` (always-on, single instance) for this project. If it's ever
found set to `"autoscale"`, that's a regression — switch it back.

Also: the gunicorn run command must include `--worker-class gthread --threads
N` (not the gunicorn default of 1 sync worker/1 thread), because
Flask-SocketIO here runs with `async_mode="threading"` — a single-threaded
sync worker would serialize every HTTP request and WebSocket long-poll behind
each other, effectively single-tasking the whole dashboard.
