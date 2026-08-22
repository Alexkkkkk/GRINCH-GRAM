---
name: gunicorn background thread startup
description: Why background threads must start at module import, not in __main__, for this Flask app
---

# Background threads must start at module import (not __main__)

This Flask + flask-socketio app is run two ways:
- Dev: `python app.py` (executes `if __name__ == "__main__"`)
- Production deploy: gunicorn `app:app` (imports the module, NEVER runs `__main__`)

**Rule:** Any background worker (socketio push loop, TON deposit poller, etc.) must be
launched from a module-level `start_background()` call guarded by a one-time flag +
lock, not only inside the `__main__` block.

**Why:** A poller placed only in `__main__` silently never runs in the deployed app,
so live data (e.g. `/api/ton`) stays stale/empty in production while looking fine in dev.

**How to apply:** Add new background threads inside the existing `start_background()`
in `app.py` (idempotent via `_bg_started` flag). Keep the guard so multi-worker
gunicorn setups don't spawn duplicate threads per process.

## 24/7 deployment must be VM, never autoscale
For continuous trading the deployment target MUST be `vm` (Reserved VM), not `autoscale`.
**Why:** autoscale scales to zero when there's no inbound HTTP traffic, which kills the
in-memory trading loop + deposit_monitor + pollers (all daemon threads started at import).
Run command pins a SINGLE worker: `gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:5000 main:app`.
**Why single worker:** >1 gunicorn worker = multiple processes = the custodial trading loop
runs more than once → duplicate buys/sells. Always keep `-w 1`; use `--threads` for concurrency.
