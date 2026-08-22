---
    name: Gunicorn preload and code-reload gotcha
    description: SIGHUP to a --preload gunicorn master does not pick up new code; only a real process restart does. self_update/hard_restart endpoints only help post-restart.
    ---
    With gunicorn --preload (app imported once by master, workers forked from that image), sending SIGHUP does NOT make a running process pick up new .py/template files on disk — HUP-triggered worker replacement still shares the master's originally-imported module image, not a disk-fresh reimport. Confirmed on GRINCH-GRAM VPS: 5 repeated self_update+SIGHUP calls left the running process on old code (old UPDATE_FILES list, old template) despite files being correctly overwritten on disk.

    **Why:** self_update endpoint's os.kill(master_pid, SIGHUP) reloads config/workers but not the preloaded app module; only a full OS-level process restart (new PID, fresh import) actually applies new code.

    **How to apply:** after self_update writes files, if you need the CHANGES to take effect (not just be staged on disk), the process needs an actual restart (host panel restart, or a hard_restart endpoint added to code THAT IS ALREADY LIVE in the running process — a newly-added hard_restart endpoint itself needs a restart to become available, so plan restarts ahead of needing them). Also: bot_settings applied via /api/config are NOT guaranteed to survive a restart if not persisted before it — always re-verify and re-apply settings via /api/config after any VPS restart.
    