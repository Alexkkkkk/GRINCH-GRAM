const socket = io();
socket.on("connect", () => console.log("Connected"));
socket.on("status_update", updateUI);

function updateUI(data) {
  const analysis = data.analysis || {};
  const stats    = data.stats    || {};
  const ai       = data.ai       || {};

  // Цена
  document.getElementById("price").textContent =
    "$" + (analysis.price || 0).toLocaleString("en-US", {minimumFractionDigits: 2});
  document.getElementById("symbol-label").textContent = data.symbol || "BTC/USDT";

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
async function saveConfig() {
  const cfg = {
    symbol: document.getElementById("cfg-symbol").value,
    trade_amount: document.getElementById("cfg-amount").value,
    stop_loss_pct: document.getElementById("cfg-sl").value,
    take_profit_pct: document.getElementById("cfg-tp").value,
    max_open_trades: document.getElementById("cfg-max").value,
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
  document.getElementById("cfg-max").value     = cfg.max_open_trades;
  document.getElementById("demo-badge").style.display = cfg.demo_mode ? "" : "none";
}
loadConfig();
