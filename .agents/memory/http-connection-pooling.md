---
name: HTTP connection pooling and response caching
description: How external API calls and hot Flask routes were optimized for speed
---

All outbound `requests.get/post` calls to external APIs (DexScreener, GeckoTerminal,
CoinGecko, TonCenter) go through a shared `requests.Session` defined in `http_client.py`
(`from http_client import SESSION as _HTTP`), not raw `requests.get(...)`.

**Why:** raw `requests.get()` opens a fresh TCP+TLS connection every call. A shared
Session with a pooled `HTTPAdapter` reuses keep-alive connections to the same host,
cutting latency significantly on repeated calls (price_feed, coin_info, wallet_tracker,
dedust_client all poll the same few hosts constantly).

**How to apply:** any new module that calls external HTTP APIs repeatedly should import
`_HTTP` from `http_client.py` instead of using the `requests` module directly. Don't
route one-off/rare calls through it if it adds complexity — the win is for hot polling
loops.

Also: `/api/candles` in `app.py` caches the computed `analyze()` payload for 8s (module
cache `_CANDLES_CACHE`) since indicator computation is CPU-heavy but the frontend polls
every 10s while OHLCV itself only refreshes every 60s. Flask-Compress is enabled for
gzip on JSON/JS/CSS responses. DB pool (`db_store.py`) sized minconn=2/maxconn=16.
