---
name: GRINCH price display consistency
description: Why the dashboard shows ONE canonical GRINCH USD price and where it comes from
---
The dashboard must show ONE GRINCH "current price" everywhere. Canonical source = `price_feed.get("GRINCH")` (DexScreener spot) — the same value the auto-liquidator uses for real sell decisions and the coin card shows.

**Why:** Two price semantics used to leak into the UI and diverge ~1%:
- `strategy.analyze()` returns `price = last candle close` (GeckoTerminal OHLCV) — fed the hero price + wallet-card USD.
- `price_feed.get("GRINCH")` (DexScreener spot) — fed the liquidator "Текущая цена" + coin card.
A candle close and a spot ticker naturally differ; users see mismatched numbers on one screen.

**How to apply:** `Trader.get_status()` overrides `analysis["price"]` with the spot price for DISPLAY only. Do NOT change `strategy.analyze()` itself — the live trading loop (`_tick`) and the chart (`/api/candles`) rely on candle closes for indicators/rendering. Keep candle data candle-based; override only the displayed price field.
