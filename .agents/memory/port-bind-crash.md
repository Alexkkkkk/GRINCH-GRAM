---
name: Port 5000 bind crash on restart
description: Why the dev workflow crashed with "Address already in use" and how app.py self-heals it
---

# "Address already in use" on workflow restart

The `Start application` dev workflow (`.pythonlibs/bin/python app.py`) intermittently
crashed at startup with `Address already in use` on port 5000. The bot logic loaded
fine every time — only the socket bind failed.

**Root cause:** a previous `app.py` instance was still holding port 5000 when the new
one started (restart race / lingering process). It was NOT a non-daemon-thread problem —
all background threads (trader, ton tracker, deposit_monitor, push_*) are already
`daemon=True`.

**Fix (in app.py `__main__`):** `_free_port(5000)` runs before `socketio.run`, plus a
bind-retry loop.
- `_free_port` scans `/proc/net/tcp` + `/proc/net/tcp6` for a LISTEN socket (state `0A`)
  on the port, maps the socket inode → PID via `/proc/[pid]/fd/*`, and SIGTERM→SIGKILL
  the holder.
- **Safety invariant:** only kills a PID whose `/proc/<pid>/cmdline` contains `app.py`
  AND is not our own PID — never touch an unrelated service on 5000.
- Retry loop catches `OSError` **only** when `errno == EADDRINUSE`; any other OSError
  is re-raised immediately (don't mask real startup failures).

**Why:** restarts must be reliable and self-healing; without this the workflow needed a
manual second restart to clear the stale listener.

**How to apply:** keep the cmdline guard + EADDRINUSE-only retry if editing startup.
This guard lives in `if __name__ == "__main__"`, so it protects the `python app.py`
dev workflow. A WSGI/gunicorn deploy entrypoint would NOT run it (different bind path).
