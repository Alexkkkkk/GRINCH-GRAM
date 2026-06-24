// Немедленно загружаем данные при старте страницы (не ждём SocketIO)
fetch("/api/status").then(r => r.json()).then(updateUI).catch(() => {});

const socket = io({
  path: "/socket.io",
  transports: ["polling", "websocket"],
});
socket.on("connect", () => {
  console.log("Connected");
  fetch("/api/status").then(r => r.json()).then(updateUI).catch(() => {});
});
socket.on("status_update", updateUI);
socket.on("price_update", updatePrice);

// Постоянный polling: REST каждые 3 сек — гарантирует актуальный статус/цену
setInterval(() => {
  fetch("/api/status").then(r => r.json()).then(updateUI).catch(() => {});
}, 3000);

function fmtPrice(p) {
  p = Number(p) || 0;
  const digits = p >= 100 ? 2 : (p >= 1 ? 4 : (p >= 0.01 ? 6 : 8));
  return "$" + p.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

let _lastLivePrice = null;
function updatePrice(d) {
  const el = document.getElementById("price");
  if (!el) return;
  const price = Number(d.price) || 0;
  el.textContent = fmtPrice(price);

  // Подсветка движения цены
  if (_lastLivePrice !== null && price !== _lastLivePrice) {
    el.classList.remove("price-up", "price-down");
    void el.offsetWidth;
    el.classList.add(price > _lastLivePrice ? "price-up" : "price-down");
  }
  _lastLivePrice = price;

  const ch = document.getElementById("price-change");
  if (ch) {
    const c = Number(d.change) || 0;
    ch.textContent = (c >= 0 ? "▲ +" : "▼ ") + c.toFixed(3) + "%";
    ch.className = "price-change " + (c >= 0 ? "chg-up" : "chg-down");
  }
}

function updateUI(data) {
  const analysis = data.analysis || {};
  const stats    = data.stats    || {};
  const ai       = data.ai       || {};

  // Цена из анализа — всегда обновляем
  const priceFromAnalysis = Number(analysis.price);
  if (priceFromAnalysis > 0) {
    const priceEl = document.getElementById("price");
    if (priceEl) {
      priceEl.textContent = fmtPrice(priceFromAnalysis);
    }
  }
  document.getElementById("symbol-label").textContent = data.symbol || "GRINCH/USDT";

  // Технический сигнал
  const sig = analysis.signal || "HOLD";
  const sb  = document.getElementById("signal-block");
  sb.className = "signal-block signal-" + sig;
  document.getElementById("signal-text").textContent = sig;
  document.getElementById("signal-strength").textContent = (analysis.strength || 0) + "% Tech";

  // AI сигнал
  const aiSig = ai.ai_signal || "HOLD";
  const aiSb  = document.getElementById("ai-signal-block");
  aiSb.className = "signal-block signal-" + aiSig;
  document.getElementById("ai-signal-text").textContent = aiSig;
  document.getElementById("ai-confidence").textContent = "AI " + (ai.confidence || 0) + "%";

  // AI обучение
  const trainedBadge = document.getElementById("ai-trained-badge");
  if (ai.model_trained) {
    trainedBadge.textContent = `✓ Обучена (${ai.samples_trained} баров)`;
    trainedBadge.style.background = "#0d2e22";
    trainedBadge.style.color = "#00d4aa";
  } else {
    trainedBadge.textContent = "обучается…";
    trainedBadge.style.background = "#3b3228";
    trainedBadge.style.color = "#ffd166";
  }

  // Вероятности
  const pU = ai.prob_up   || 0;
  const pH = ai.prob_hold || 0;
  const pD = ai.prob_down || 0;
  document.getElementById("bar-up").style.width   = pU + "%";
  document.getElementById("bar-hold").style.width = pH + "%";
  document.getElementById("bar-down").style.width = pD + "%";
  document.getElementById("val-up").textContent   = pU + "%";
  document.getElementById("val-hold").textContent = pH + "%";
  document.getElementById("val-down").textContent = pD + "%";

  // Режим рынка
  const regime = ai.regime || {};
  const regimeEl = document.getElementById("regime-badge");
  const regimeColors = { green:"#00d4aa", red:"#ff4d6d", yellow:"#ffd166", blue:"#4f8ef7", purple:"#a78bfa", grey:"#8892b0" };
  const rc = regimeColors[regime.color] || "#8892b0";
  regimeEl.textContent = `${regime.name || "—"} — ${regime.desc || ""}`;
  regimeEl.style.borderColor = rc;
  regimeEl.style.color = rc;

  // Аномалия
  const anomaly = ai.anomaly || {};
  const anomEl  = document.getElementById("anomaly-alert");
  if (anomaly.detected) {
    anomEl.style.display = "";
    document.getElementById("anomaly-desc").textContent =
      `${anomaly.description} | Z-цена=${anomaly.z_price} | Z-объём=${anomaly.z_volume}`;
  } else {
    anomEl.style.display = "none";
  }

  // Прогноз
  const fc = ai.forecast || {};
  document.getElementById("fc-t1").textContent = fc.t1 ? "$" + fc.t1 : "—";
  document.getElementById("fc-t2").textContent = fc.t2 ? "$" + fc.t2 : "—";
  document.getElementById("fc-t3").textContent = fc.t3 ? "$" + fc.t3 : "—";
  const fcT1El = document.getElementById("fc-t1");
  const fcT3El = document.getElementById("fc-t3");
  if (fc.bull !== undefined) {
    const fc3cls = fc.bull ? "pnl-pos" : "pnl-neg";
    fcT1El.className = "fc-val " + fc3cls;
    fcT3El.className = "fc-val " + fc3cls;
  }
  if (fc.range_up && fc.range_down) {
    document.getElementById("fc-range").textContent =
      `Диапазон: $${fc.range_down} – $${fc.range_up} (ATR)`;
  }

  // Уровни S/R
  renderSR(ai.support_resistance || {});

  // Паттерны
  renderPatterns(ai.patterns || []);

  // Важность признаков
  renderFeatureImportance(ai.feature_importance || []);

  // Индикаторы
  document.getElementById("rsi").textContent      = analysis.rsi ?? "—";
  document.getElementById("macd").textContent     = analysis.macd ?? "—";
  document.getElementById("ema-fast").textContent = analysis.ema_fast ?? "—";
  document.getElementById("ema-slow").textContent = analysis.ema_slow ?? "—";
  document.getElementById("bb-upper").textContent = analysis.bb_upper ?? "—";
  document.getElementById("bb-lower").textContent = analysis.bb_lower ?? "—";

  // Статус кнопок
  const running = data.running;
  document.getElementById("btn-start").style.display = running ? "none" : "";
  document.getElementById("btn-stop").style.display  = running ? "" : "none";
  const badge = document.getElementById("status-badge");
  badge.textContent = running ? "РАБОТАЕТ" : "ОСТАНОВЛЕН";
  badge.className = "badge " + (running ? "badge-running" : "badge-stopped");
  document.getElementById("demo-badge").style.display = data.demo_mode ? "" : "none";

  // Статистика
  document.getElementById("stat-total").textContent   = stats.total_trades || 0;
  document.getElementById("stat-winrate").textContent = (stats.winrate || 0) + "%";
  const pnl   = stats.total_pnl || 0;
  const pnlEl = document.getElementById("stat-pnl");
  pnlEl.textContent = (pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(2);
  pnlEl.className = "stat-value " + (pnl >= 0 ? "pnl-pos" : "pnl-neg");
  document.getElementById("stat-open").textContent = (data.open_trades || []).length;

  // Баланс
  const bal     = data.balance || {};
  const balList = document.getElementById("balance-list");
  balList.innerHTML = Object.entries(bal).map(([k, v]) =>
    `<div class="balance-item"><span class="balance-asset">${k}</span>
     <span class="balance-amount">${Number(v).toFixed(4)}</span></div>`
  ).join("") || '<div class="empty-msg">Нет данных</div>';

  renderOpenTrades(data.open_trades  || []);
  renderHistory(data.recent_trades || []);
  renderLogs(data.logs             || []);
}

function renderSR(sr) {
  const res = sr.resistance || [];
  const sup = sr.support    || [];
  document.getElementById("sr-res").innerHTML = res.length
    ? res.reverse().map(v => `<div class="sr-val sr-res">$${v}</div>`).join("")
    : '<div class="empty-msg">—</div>';
  document.getElementById("sr-sup").innerHTML = sup.length
    ? sup.map(v => `<div class="sr-val sr-sup">$${v}</div>`).join("")
    : '<div class="empty-msg">—</div>';
}

function renderPatterns(patterns) {
  const el = document.getElementById("patterns-list");
  if (!patterns.length) {
    el.innerHTML = '<div class="empty-msg">Паттерны не обнаружены</div>';
    return;
  }
  el.innerHTML = patterns.map(p => {
    const cls = p.type === "bullish" ? "pat-bull" : p.type === "bearish" ? "pat-bear" : "pat-neut";
    const icon = p.type === "bullish" ? "🟢" : p.type === "bearish" ? "🔴" : "🟡";
    return `<div class="pattern-item ${cls}">${icon} <b>${p.name}</b> — <span>${p.desc}</span></div>`;
  }).join("");
}

function renderFeatureImportance(fi) {
  const el = document.getElementById("fi-list");
  if (!fi.length) { el.innerHTML = '<div class="empty-msg">Нет данных</div>'; return; }
  const max = fi[0].importance;
  el.innerHTML = fi.map(f => `
    <div class="fi-row">
      <span class="fi-name">${f.feature}</span>
      <div class="fi-bar-wrap"><div class="fi-bar" style="width:${(f.importance/max*100).toFixed(0)}%"></div></div>
      <span class="fi-val">${f.importance}%</span>
    </div>`
  ).join("");
}

function renderOpenTrades(trades) {
  const el = document.getElementById("open-trades-list");
  if (!trades.length) { el.innerHTML = '<div class="empty-msg">Нет открытых сделок</div>'; return; }
  el.innerHTML = trades.map(t => `
    <div class="trade-card buy">
      <div class="trade-row">
        <span class="trade-side buy">BUY</span>
        <span style="color:#8892b0;font-size:11px">${t.opened_at?.slice(11,19) || ""}</span>
      </div>
      <div class="trade-row">
        <span>Вход: <b>$${t.entry_price}</b></span>
        <span>Кол-во: ${t.amount}</span>
      </div>
      <div class="trade-row">
        <span style="color:#ff4d6d">SL: $${t.stop_loss}</span>
        <span style="color:#00d4aa">TP: $${t.take_profit}</span>
      </div>
      ${t.ai_confidence ? `<div class="trade-row"><span style="color:#a78bfa;font-size:10px">AI уверенность: ${t.ai_confidence}%</span></div>` : ""}
    </div>`
  ).join("");
}

function renderHistory(trades) {
  const el     = document.getElementById("trades-history");
  const closed = trades.filter(t => t.status === "closed").reverse();
  if (!closed.length) { el.innerHTML = '<div class="empty-msg">История пуста</div>'; return; }
  el.innerHTML = closed.slice(0, 20).map(t => {
    const pnl    = t.pnl || 0;
    const cls    = pnl >= 0 ? "closed-win" : "closed-loss";
    const pnlCls = pnl >= 0 ? "pnl-pos" : "pnl-neg";
    return `
      <div class="trade-card ${cls}">
        <div class="trade-row">
          <span class="trade-side ${t.side}">${t.side?.toUpperCase()}</span>
          <span class="${pnlCls}">${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}</span>
        </div>
        <div class="trade-row">
          <span style="color:#8892b0">Вход: $${t.entry_price}</span>
          <span style="color:#8892b0">Выход: $${t.exit_price || "—"}</span>
        </div>
        <div class="trade-row" style="color:#4a5568;font-size:10px">
          <span>${t.close_reason || ""}</span>
          <span>${t.closed_at?.slice(11,19) || ""}</span>
        </div>
      </div>`;
  }).join("");
}

function renderLogs(logs) {
  const el = document.getElementById("log-container");
  el.innerHTML = [...logs].reverse().slice(0, 80).map(l =>
    `<div class="log-entry log-${l.level}"><span class="log-time">${l.time}</span>${escHtml(l.msg)}</div>`
  ).join("");
}

function clearLogs() { document.getElementById("log-container").innerHTML = ""; }
function escHtml(s)   { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

async function startAgent() {
  await fetch("/api/start", {method:"POST"});
}
async function stopAgent() {
  await fetch("/api/stop", {method:"POST"});
}
async function switchPair(symbol) {
  const r = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol }),
  });
  const d = await r.json();
  if (!d.ok) {
    alert(d.message);
    loadConfig();           // вернуть прежнюю пару в списке
    return;
  }
  // Сброс живой цены, чтобы изменение % не переносилось со старой монеты
  _lastLivePrice = null;
  document.getElementById("symbol-label").textContent = symbol;
  document.getElementById("price").textContent = "…";
  document.getElementById("price-change").textContent = "—";
}

async function saveConfig() {
  const cfg = {
    symbol: document.getElementById("cfg-symbol").value,
    trade_amount: document.getElementById("cfg-amount").value,
    stop_loss_pct: document.getElementById("cfg-sl").value,
    take_profit_pct: document.getElementById("cfg-tp").value,
    trailing_stop_pct: document.getElementById("cfg-trail").value,
    fee_pct: document.getElementById("cfg-fee").value,
    min_ai_confidence: document.getElementById("cfg-minconf").value,
    max_open_trades: document.getElementById("cfg-max").value,
    use_dynamic_targets: document.getElementById("cfg-dyn").checked,
    trend_filter: document.getElementById("cfg-trend").checked,
  };
  const r = await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(cfg)});
  const d = await r.json();
  alert(d.message);
}
async function loadConfig() {
  const r   = await fetch("/api/config");
  const cfg = await r.json();
  document.getElementById("cfg-symbol").value  = cfg.symbol;
  document.getElementById("cfg-amount").value  = cfg.trade_amount;
  document.getElementById("cfg-sl").value      = cfg.stop_loss_pct;
  document.getElementById("cfg-tp").value      = cfg.take_profit_pct;
  document.getElementById("cfg-trail").value   = cfg.trailing_stop_pct;
  document.getElementById("cfg-fee").value     = cfg.fee_pct;
  document.getElementById("cfg-minconf").value = cfg.min_ai_confidence;
  document.getElementById("cfg-max").value     = cfg.max_open_trades;
  document.getElementById("cfg-dyn").checked   = !!cfg.use_dynamic_targets;
  document.getElementById("cfg-trend").checked = !!cfg.trend_filter;
  document.getElementById("demo-badge").style.display = cfg.demo_mode ? "" : "none";
  if (cfg.ton_wallet) {
    window._tonWallet = cfg.ton_wallet;
    document.getElementById("ton-addr").textContent = cfg.ton_wallet;
  }
}

async function copyTon() {
  const addr = window._tonWallet || document.getElementById("ton-addr").textContent;
  try {
    await navigator.clipboard.writeText(addr);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = addr; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); document.body.removeChild(ta);
  }
  const c = document.getElementById("ton-copied");
  c.style.display = "";
  setTimeout(() => { c.style.display = "none"; }, 1800);
}

function renderTon(d) {
  document.getElementById("ton-balance").textContent  = (d.balance ?? 0).toFixed(4) + " TON";
  document.getElementById("ton-received").textContent = (d.total_received ?? 0).toFixed(4) + " TON";

  const errEl = document.getElementById("ton-error");
  if (d.last_error) {
    errEl.style.display = "";
    errEl.textContent = "⚠ " + d.last_error;
  } else {
    errEl.style.display = "none";
  }

  const box = document.getElementById("ton-deposits");
  if (!d.deposits || d.deposits.length === 0) {
    box.innerHTML = '<div class="ton-empty">Поступлений пока нет</div>';
    return;
  }
  box.innerHTML = d.deposits.map(dep => {
    const dt = dep.time ? new Date(dep.time * 1000).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "";
    const cm = dep.comment ? `<div class="ton-dep-comment">💬 ${escapeHtml(dep.comment)}</div>` : "";
    return `<div class="ton-dep">
      <div class="ton-dep-top">
        <span class="ton-dep-amount">+${dep.amount} TON</span>
        <span class="ton-dep-time">${dt}</span>
      </div>
      <div class="ton-dep-from">от ${escapeHtml(dep.from_short || "")}</div>
      ${cm}
    </div>`;
  }).join("");
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

async function loadTon() {
  try {
    const r = await fetch("/api/ton");
    renderTon(await r.json());
  } catch (e) { /* silent */ }
}

async function refreshTon() {
  const btn = document.querySelector(".btn-ton-refresh");
  if (btn) btn.classList.add("spin");
  try {
    const r = await fetch("/api/ton/refresh", { method: "POST" });
    renderTon(await r.json());
  } catch (e) { /* silent */ }
  if (btn) setTimeout(() => btn.classList.remove("spin"), 600);
}

function fmtBig(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return "$" + (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
  return "$" + n.toFixed(2);
}
function fmtAmt(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
}
function timeAgo(ts) {
  if (!ts) return "";
  const sec = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (sec < 60) return Math.floor(sec) + "с";
  if (sec < 3600) return Math.floor(sec / 60) + "м";
  if (sec < 86400) return Math.floor(sec / 3600) + "ч";
  return Math.floor(sec / 86400) + "д";
}

async function loadCoin() {
  try {
    const r = await fetch("/api/coin");
    const d = await r.json();
    if (!d || !d.symbol) return;
    const img = document.getElementById("coin-img");
    if (d.image) { img.src = d.image; img.style.display = "block"; }
    else { img.style.display = "none"; }
    document.getElementById("coin-name").textContent = d.name || d.symbol;
    document.getElementById("coin-sym").textContent = d.symbol || "—";
    document.getElementById("coin-source").textContent = d.source ? "· " + d.source : "";
    document.getElementById("coin-price").textContent = d.price_usd != null ? fmtPrice(d.price_usd) : "—";

    const ch = document.getElementById("coin-change");
    if (d.change_h24 != null) {
      const up = d.change_h24 >= 0;
      ch.textContent = (up ? "+" : "") + d.change_h24.toFixed(2) + "%";
      ch.className = "cs-val " + (up ? "pos" : "neg");
    } else { ch.textContent = "—"; ch.className = "cs-val"; }

    document.getElementById("coin-vol").textContent = d.volume_h24 != null ? fmtBig(d.volume_h24) : "—";
    document.getElementById("coin-liq").textContent = d.liquidity != null ? fmtBig(d.liquidity) : "—";
    document.getElementById("coin-mcap").textContent = d.market_cap != null ? fmtBig(d.market_cap) : "—";

    const tx = document.getElementById("coin-txns");
    if (d.buys_h24 != null || d.sells_h24 != null) {
      tx.innerHTML = '<span class="pos">' + (Number(d.buys_h24) || 0) + '↑</span> / <span class="neg">' + (Number(d.sells_h24) || 0) + '↓</span>';
    } else { tx.textContent = "—"; }

    const link = document.getElementById("coin-link");
    if (d.url) { link.href = d.url; link.style.display = "inline"; }
    else { link.style.display = "none"; }
  } catch (e) { /* silent */ }
}

async function loadDexTrades() {
  try {
    const r = await fetch("/api/coin/trades");
    const arr = await r.json();
    const box = document.getElementById("dex-trades");
    const note = document.getElementById("trades-note");
    if (!Array.isArray(arr) || arr.length === 0) {
      box.innerHTML = '<div class="empty-msg">Лента доступна для GRINCH</div>';
      note.textContent = "";
      return;
    }
    note.textContent = "· DEX";
    box.innerHTML = arr.map(t => {
      const buy = t.kind === "buy";
      const sym = (document.getElementById("coin-sym").textContent || "").replace("—", "");
      return '<div class="dex-trade">' +
        '<span class="dt-side ' + (buy ? "pos" : "neg") + '">' + (buy ? "Покупка" : "Продажа") + '</span>' +
        '<span class="dt-amt">' + fmtAmt(t.token_amount) + ' ' + escapeHtml(sym) + '</span>' +
        '<span class="dt-usd">' + (t.amount_usd != null ? fmtBig(t.amount_usd) : "—") + '</span>' +
        '<span class="dt-time">' + timeAgo(t.ts) + '</span>' +
        '</div>';
    }).join("");
  } catch (e) { /* silent */ }
}

async function loadExchanges() {
  try {
    const r = await fetch("/api/coin/exchanges");
    const d = await r.json();
    const list = document.getElementById("exch-list");
    const rows = (d && d.exchanges) || [];
    const cnt = document.getElementById("exch-count");
    const aiBox = document.getElementById("exch-ai");

    if (rows.length === 0) {
      list.innerHTML = '<div class="empty-msg">Нет данных</div>';
      cnt.textContent = "";
      aiBox.style.display = "none";
      return;
    }
    cnt.textContent = "· " + rows.length + " бирж";

    const agg = d.agg;
    if (agg) {
      aiBox.style.display = "block";
      const sig = document.getElementById("exch-signal");
      sig.textContent = agg.signal;
      sig.className = "exch-ai-signal sig-" + (agg.signal === "АРБИТРАЖ" ? "arb" : (agg.signal === "РАСХОЖДЕНИЕ" ? "div" : "con"));
      document.getElementById("exch-spread").textContent = "спред " + agg.spread_pct + "%";
      document.getElementById("exch-note").textContent = agg.note || "";
      document.getElementById("exch-avg").textContent = fmtPrice(agg.avg_price);
      document.getElementById("exch-buy").textContent =
        (agg.best_buy ? escapeHtml(agg.best_buy.name) + " " + fmtPrice(agg.best_buy.price) : "—");
      document.getElementById("exch-sell").textContent =
        (agg.best_sell ? escapeHtml(agg.best_sell.name) + " " + fmtPrice(agg.best_sell.price) : "—");
    } else {
      aiBox.style.display = "none";
    }

    list.innerHTML = rows.map(e => {
      const chv = (e.change24h != null && isFinite(Number(e.change24h))) ? Number(e.change24h) : null;
      const ch = chv != null
        ? '<span class="ex-ch ' + (chv >= 0 ? "pos" : "neg") + '">' +
            (chv >= 0 ? "+" : "") + chv.toFixed(2) + '%</span>'
        : '<span class="ex-ch"></span>';
      const liqOrVol = e.liquidity != null ? ("Ликв " + fmtBig(e.liquidity))
                      : (e.volume24h != null ? ("Об " + fmtBig(e.volume24h)) : "");
      return '<div class="ex-row">' +
        '<span class="ex-name">' + escapeHtml(e.name) +
          '<span class="ex-kind">' + escapeHtml(e.kind || "") + '</span></span>' +
        '<span class="ex-price">' + fmtPrice(e.price) + '</span>' +
        ch +
        '<span class="ex-liq">' + escapeHtml(liqOrVol) + '</span>' +
        '</div>';
    }).join("");
  } catch (e) { /* silent */ }
}

loadConfig();
loadTon();
loadCoin();
loadDexTrades();
loadExchanges();
setInterval(loadTon, 30000);
setInterval(loadCoin, 20000);
setInterval(loadDexTrades, 15000);
setInterval(loadExchanges, 20000);
