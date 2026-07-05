---
name: Advisor DEX snapshot section
description: How the ai_advisor market snapshot gets deep DeDust/DexScreener order-flow data, and how the LLM is instructed to use it to trade more actively.
---

The advisor's `_build_snapshot()` includes a `dex` section built from `coin_info.py` (DexScreener market stats + GeckoTerminal recent trades), separate from `analytics_buffer` (which only tracks the bot's own tick history).

Fields: `volume_h24_usd`, `liquidity_usd`, `buys_h24`/`sells_h24`, `buy_sell_ratio_h24`, `recent_flow_usd` (net buy-minus-sell USD over the last ~30 GeckoTerminal trades), `change_h1_pct`/`change_h24_pct`.

**Why:** the user wanted the advisor to react to real DeDust order flow (buy/sell pressure) rather than only its own internal tick buffer, so it trades more actively when the market itself shows strong buying pressure.

**How to apply:** the SYSTEM_PROMPT instructs the LLM: `buy_sell_ratio_h24 > 1.3` → lower `min_ai_confidence` by 3-5 points and raise `ai_size_mult` by 0.1-0.2 (trade more actively); `< 0.7` or negative `recent_flow_usd` → stay conservative. Keep this section wrapped in try/except (`snap["dex"] = {"error": ...}` on failure) since it depends on external DexScreener/GeckoTerminal APIs — never let it break the whole snapshot build.
