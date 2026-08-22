---
name: GRINCH chart & API latency
description: Why the candlestick chart needs /api/candles (not /api/status), and the CDN-in-preview-iframe pitfall
---

- The frontend candlestick chart (lightweight-charts) must poll `/api/candles`, NOT `/api/status`.
  **Why:** `/api/status` calls `trader.get_status()` → `exchange.get_balance()`, which makes TON blockchain "get method" calls and takes ~4.5s. Any client fetch on it stays pending long enough that the chart renders blank (refresh never resolves before paint). `/api/candles` only runs `analyze(get_ohlcv())` (DeDust mode uses local `_fake_ohlcv`, no network) and returns in ~0.02s.
  **How to apply:** keep any latency-sensitive polling (charts, sparklines) off `/api/status`; add/extend a lightweight endpoint that avoids `get_balance`.

- Candle timestamps from `analyze()` are strings like `"2026-06-25 13:40:05.423"` (space-separated, ms). Convert to lightweight-charts UTCTimestamp via `replace(" ","T")+"Z"` → `Date.parse` → floor(ms/1000). Data is hourly (~50 pts).

- Real GRINCH candles come from GeckoTerminal pool OHLCV: `GET api.geckoterminal.com/api/v2/networks/ton/pools/{GRINCH_POOL_ADDRESS}/ohlcv/hour?currency=...&token=base`. Returns `ohlcv_list` newest-first, ts in SECONDS (×1000 for ms), so reverse to oldest-first.
  - **GRINCH/GRAM pair** (GRINCH priced in GRAM = renamed Toncoin/TON, ~0.0003) = `currency=token&token=base`. `currency=usd` gives USD (~0.00047). DeDust "GRAM" = native TON.
  - **Must send a browser `User-Agent`** — default urllib UA (`Python-urllib/x`) gets HTTP 403 from GeckoTerminal/Cloudflare. curl works because of its UA, masking the bug in shell tests.
  - Free tier rate-limits hard (HTTP 429). Required pattern: in-memory cache (TTL ~180s, hourly candles), failure backoff that serves STALE data instead of re-hitting, /tmp disk persistence so restarts don't re-storm, and a lock (singleflight) since Flask is multi-threaded. Without negative-caching, a failing fetch never populates cache → every call refetches → self-inflicted 429 storm.
  - Keep the trading engine's `get_ohlcv` on the local sim (`_fake_ohlcv`); only the chart uses real GeckoTerminal data, to avoid doubling API pressure.

- CDN `<script>` tags can silently fail to load inside the Replit preview iframe even when the URL returns 200 from the server shell. A failed script *load* does NOT trigger `window`'s `error` listener unless capture=true, so it looks identical to "library undefined → init loops forever, no error". Symptom: feature blank, no console error, no axes.
  **Why:** preview iframe network/CSP context differs from server-side curl.
  **How to apply:** self-host critical JS libs under `static/js/` instead of relying on a CDN. lightweight-charts standalone global is `window.LightweightCharts` (and `LightweightCharts.CrosshairMode` etc.).
