---
name: RAM optimization for AI ensemble
description: Why AI engine model sizes/retrain cadence were reduced, and where the real memory ceiling is
---

The bot is also deployed on an external low-memory host (Bothost, Docker, GitHub-synced) separate from this Replit env, with a much tighter RAM ceiling (~256-512MB). That host was OOM-killing the gunicorn worker seconds after boot, during the initial `pretrain()` model-fitting phase.

**Why:** sklearn/xgboost/lightgbm ensembles hold all fitted estimators in memory permanently, and during (re)fit, old+new model objects plus training arrays can coexist, spiking well above steady-state RSS. `pretrain()` fits 6-7 models sequentially at startup — that spike is what triggers OOM on constrained hosts, not steady-state usage.

**How to apply:** if OOM/RAM issues resurface, first check whether it's a startup-time spike (during pretrain/refit) vs. steady-state growth. For spikes: shrink `n_estimators`/`max_iter`/`max_depth` per model, shrink `REPLAY_SIZE`, raise `RETRAIN_EVERY`, and call `gc.collect()` right after each `slot.fit()` (already done in `ai_engine.py`). Note the Python+numpy+pandas+sklearn+xgboost import baseline alone is ~200-250MB RSS even before any model exists — that floor can't be reduced without dropping a whole library (e.g. XGBoost or LightGBM), which is a bigger tradeoff (fewer models = quicker but slightly less accurate ensemble). If OOM persists after these tunings, the real fix is more container RAM on the external host, not further code shrinking.
