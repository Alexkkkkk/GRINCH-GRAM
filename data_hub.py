"""
data_hub.py — многоисточниковый агрегатор рыночных данных
=============================================================
Собирает бесплатные данные из 6 источников параллельно,
кеширует с индивидуальным TTL и отдаёт единый плоский словарь.

Источники:
  1. alternative.me    — Fear & Greed Index (crypto sentiment)
  2. api.binance.com   — BTC/USDT и TON/USDT: цена, объём, изм. 24ч
  3. api.bybit.com     — funding rate и Open Interest TONUSDT perp
  4. api.llama.fi      — TVL сети TON + STON.fi DEX
  5. api.geckoterminal — трендовые пулы TON (позиция GRINCH)
  6. toncenter.com     — статистика сети: TX/24ч, кол-во аккаунтов

Нет API-ключей — всё бесплатно.
"""

import threading
import time
import logging

log = logging.getLogger("data_hub")

# ─── TTL для каждого источника (секунды) ─────────────────────────────────────
_TTL = {
    "fear_greed":  1800,   # 30 мин — обновляется очень редко
    "binance":       30,   # 30 сек — живая цена CEX
    "bybit":         60,   # 1 мин  — funding rate
    "defillama":    300,   # 5 мин  — TVL меняется медленно
    "geckotrend":   120,   # 2 мин  — позиция в трендах
    "ton_stats":    180,   # 3 мин  — сетевая статистика
}

_cache: dict      = {}    # key → {field: value}
_cache_ts: dict   = {}    # key → unix timestamp последнего обновления
_lock             = threading.RLock()

# ─── Вспомогательный HTTP-запрос ─────────────────────────────────────────────

def _get(url: str, timeout: int = 10, params: dict | None = None):
    try:
        from http_client import SESSION
        r = SESSION.get(url, timeout=timeout, params=params,
                        headers={"Accept": "application/json"})
        if r.status_code == 200:
            return r.json()
        log.debug(f"[Hub] {url} → HTTP {r.status_code}")
    except Exception as e:
        log.debug(f"[Hub] {url}: {e}")
    return None


# ─── Фетчеры ─────────────────────────────────────────────────────────────────

def _fetch_fear_greed() -> dict:
    """alternative.me — Crypto Fear & Greed Index (0 = паника, 100 = жадность)."""
    d = _get("https://api.alternative.me/fng/?limit=2")
    if not d or "data" not in d:
        return {}
    cur  = d["data"][0]
    prev = d["data"][1] if len(d["data"]) > 1 else cur
    val  = int(cur.get("value", 50))
    prev_val = int(prev.get("value", 50))
    label_map = {
        "Extreme Fear":  "💀 крайний страх",
        "Fear":          "😨 страх",
        "Neutral":       "😐 нейтрально",
        "Greed":         "😏 жадность",
        "Extreme Greed": "🤑 крайняя жадность",
    }
    label_ru = label_map.get(cur.get("value_classification", ""), cur.get("value_classification", ""))
    return {
        "fg_value":     val,
        "fg_label":     label_ru,
        "fg_label_en":  cur.get("value_classification", "Neutral"),
        "fg_prev":      prev_val,
        "fg_delta":     val - prev_val,
        "fg_norm":      round((val - 50) / 50.0, 4),   # -1..+1 для ML
    }


def _fetch_kraken() -> dict:
    """Kraken — BTC/USD и TON/USD: цена, объём, изменение 24ч (от open)."""
    result = {}
    d = _get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD,TONUSD")
    if not d or d.get("error"):
        return result
    pairs_map = {"XXBTZUSD": "btc", "TONUSD": "ton_cex", "XTONZUSD": "ton_cex"}
    for pair_key, data in (d.get("result") or {}).items():
        key = None
        for k, v in pairs_map.items():
            if k in pair_key or pair_key.endswith("TONUSD"):
                key = v
                break
        if not key:
            # авто: XBT → btc, TON → ton_cex
            key = "btc" if "XBT" in pair_key or "BTC" in pair_key else "ton_cex"
        price = float((data.get("c") or ["0"])[0])
        open_ = float(data.get("o") or price)
        vol   = float((data.get("v") or ["0", "0"])[1])   # 24h rolling volume
        chg24h = (price - open_) / open_ * 100 if open_ else 0.0
        result[f"{key}_price"]     = round(price, 6)
        result[f"{key}_change24h"] = round(chg24h, 4)
        result[f"{key}_volume24h"] = round(vol, 2)
        result[f"{key}_high24h"]   = float((data.get("h") or ["0", "0"])[1])
        result[f"{key}_low24h"]    = float((data.get("l") or ["0", "0"])[1])
    return result


def _fetch_cg_trending() -> dict:
    """CoinGecko — трендовые монеты (есть ли TON/GRINCH в топ-трендах)."""
    d = _get("https://api.coingecko.com/api/v3/search/trending")
    if not d:
        return {}
    coins = [c.get("item", {}) for c in d.get("coins", [])]
    symbols = [c.get("symbol", "").upper() for c in coins]
    ids     = [c.get("id", "").lower() for c in coins]
    ton_rank   = next((i+1 for i, s in enumerate(symbols) if s in ("TON", "TONCOIN")), 0)
    btc_dom    = next((i+1 for i, s in enumerate(symbols) if s == "BTC"), 0)
    trend_count = len(coins)
    return {
        "cg_ton_trend_rank":  ton_rank,
        "cg_trending_coins":  symbols[:7],
        "cg_trend_count":     trend_count,
        "cg_ton_is_trending": int(ton_rank > 0),
        # Нормализовано для ML: позиция в трендах / 10
        "cg_ton_trend_ml":    round(ton_rank / 10.0, 2) if ton_rank else 0.0,
    }


def _fetch_defillama() -> dict:
    """DeFiLlama — TVL сети TON (тренд) + STON.fi DEX."""
    result = {}
    # TVL сети TON (исторический список)
    d = _get("https://api.llama.fi/v2/historicalChainTvl/TON", timeout=12)
    if d and isinstance(d, list) and len(d) >= 2:
        cur  = float(d[-1].get("tvl", 0) or 0)
        prev = float(d[-2].get("tvl", 0) or 0)
        result["ton_tvl"]        = cur
        result["ton_tvl_change"] = round((cur - prev) / prev * 100, 2) if prev else 0.0
        result["ton_tvl_ml"]     = round(result["ton_tvl_change"] / 5.0, 4)  # нормализовано
    # STON.fi — крупнейший DEX на TON
    d2 = _get("https://api.llama.fi/protocol/ston-fi", timeout=12)
    if d2:
        result["stonfi_tvl"] = float(d2.get("currentChainTvls", {}).get("TON", 0) or 0)
    return result


def _fetch_geckotrend() -> dict:
    """GeckoTerminal — трендовые пулы TON; ищем GRINCH."""
    result = {"grinch_trend_rank": 0, "grinch_trend_vol24h": 0.0, "ton_trending_pools": 0}
    d = _get("https://api.geckoterminal.com/api/v2/networks/ton/trending_pools")
    if not d:
        return result
    pools = d.get("data", [])
    result["ton_trending_pools"] = len(pools)
    # Ищем пул GRINCH по имени или адресу базового токена
    grinch_kw = "GRINCH"
    for i, pool in enumerate(pools):
        attrs = pool.get("attributes", {})
        name  = (attrs.get("name") or "").upper()
        if grinch_kw in name:
            rank = len(pools) - i          # чем выше в списке — тем лучше
            result["grinch_trend_rank"]   = rank
            result["grinch_trend_vol24h"] = float(
                attrs.get("volume_usd", {}).get("h24", 0) or 0
            )
            result["grinch_trending_ml"]  = round(rank / 10.0, 2)  # нормализовано
            break
    if "grinch_trending_ml" not in result:
        result["grinch_trending_ml"] = 0.0
    return result


def _fetch_ton_stats() -> dict:
    """TonCenter — статистика сети TON по последним блокам (TX/блок, seqno)."""
    result = {}
    # Берём 20 последних мастер-блоков для оценки нагрузки сети
    d = _get("https://toncenter.com/api/v3/blocks?limit=20&workchain=-1", timeout=10)
    if d:
        blocks = d.get("blocks", [])
        if blocks:
            tx_total = sum(int(b.get("tx_count", 0) or 0) for b in blocks)
            seqnos   = [int(b.get("seqno", 0) or 0) for b in blocks]
            result["ton_seqno"]       = max(seqnos) if seqnos else 0
            result["ton_tx_per_block"] = round(tx_total / len(blocks), 1)
            result["ton_tx_20blocks"]  = tx_total
    # Резервный вариант: getMasterchainInfo для текущего seqno
    if "ton_seqno" not in result:
        d2 = _get("https://toncenter.com/api/v2/getMasterchainInfo")
        if d2 and d2.get("ok"):
            result["ton_seqno"] = int(
                (d2.get("result") or {}).get("last", {}).get("seqno", 0) or 0
            )
    return result


# ─── Реестр фетчеров ─────────────────────────────────────────────────────────

_FETCHERS = {
    "fear_greed":  _fetch_fear_greed,
    "kraken":      _fetch_kraken,
    "cg_trending": _fetch_cg_trending,
    "defillama":   _fetch_defillama,
    "geckotrend":  _fetch_geckotrend,
    "ton_stats":   _fetch_ton_stats,
}


# ─── Логика обновления ───────────────────────────────────────────────────────

def _refresh_source(key: str) -> bool:
    """Обновляет один источник, если истёк TTL. Возвращает True при успехе."""
    now = time.time()
    with _lock:
        last = _cache_ts.get(key, 0)
    if now - last < _TTL.get(key, 60):
        return True   # ещё свежо, не нужно обновлять
    try:
        data = _FETCHERS[key]()
        if data:
            with _lock:
                _cache[key]    = data
                _cache_ts[key] = now
            log.debug(f"[Hub] ✅ {key}: {list(data.keys())}")
            return True
    except Exception as e:
        log.warning(f"[Hub] ⚠️ {key}: {e}")
    return False


def refresh_all(parallel: bool = True):
    """Обновляет все источники (параллельно по умолчанию)."""
    if parallel:
        threads = []
        for key in _FETCHERS:
            t = threading.Thread(target=_refresh_source, args=(key,), daemon=True, name=f"hub-{key}")
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=15)
    else:
        for key in _FETCHERS:
            _refresh_source(key)


# ─── Публичное API ───────────────────────────────────────────────────────────

def get_snapshot() -> dict:
    """Возвращает плоский словарь всех актуальных данных из всех источников."""
    snap = {}
    with _lock:
        for key, data in _cache.items():
            if isinstance(data, dict):
                snap.update(data)
    return snap


def get_source_status() -> list:
    """Возвращает список источников с возрастом данных и количеством полей."""
    now = time.time()
    rows = []
    with _lock:
        for key in _FETCHERS:
            age_s = int(now - _cache_ts.get(key, 0))
            fields = len(_cache.get(key, {}))
            ttl    = _TTL.get(key, 60)
            rows.append({
                "source":   key,
                "age_s":    age_s,
                "ttl_s":    ttl,
                "fields":   fields,
                "fresh":    age_s < ttl * 1.5,
                "stale":    age_s > ttl * 3,
            })
    return rows


def get_ml_features() -> dict:
    """Возвращает только ML-фичи (нормализованные) с fallback=0."""
    snap = get_snapshot()
    return {
        "fg_norm":          float(snap.get("fg_norm",             0.0)),
        "btc_trend":        float(snap.get("btc_change24h",       0.0)) / 10.0,
        "funding_rate_ml":  float(snap.get("cg_ton_trend_ml",     0.0)),   # CG trending вместо funding
        "ton_tvl_ml":       float(snap.get("ton_tvl_ml",          0.0)),
        "grinch_trending":  float(snap.get("grinch_trending_ml",  0.0)),
    }


# ─── Фоновый поток автообновления ────────────────────────────────────────────

def _background_loop():
    time.sleep(5)           # дать основным модулям подняться
    log.info("[Hub] 🔄 Первоначальная загрузка всех источников...")
    refresh_all(parallel=True)
    ok = sum(1 for k in _FETCHERS if _cache.get(k))
    log.info(f"[Hub] ✅ Загружено {ok}/{len(_FETCHERS)} источников")
    while True:
        time.sleep(30)      # каждые 30 сек проверяем TTL
        try:
            refresh_all(parallel=True)
        except Exception as e:
            log.error(f"[Hub] loop error: {e}")


_bg_thread = threading.Thread(
    target=_background_loop, name="data-hub-bg", daemon=True
)
_bg_thread.start()
log.info("🌐 DataHub v1.0 — 6 источников: F&G · Kraken · CG-Trending · DeFiLlama · GeckoTrend · TONStats")
