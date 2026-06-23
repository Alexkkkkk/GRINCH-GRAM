const socket = io();

socket.on("connect", () => console.log("Connected"));
socket.on("status_update", updateUI);

function updateUI(data) {
  const analysis = data.analysis || {};
  const stats = data.stats || {};

  // Цена
  document.getElementById("price").textContent = "$" + (analysis.price || 0).toLocaleString("en-US", {minimumFractionDigits: 2});
  document.getElementById("symbol-label").textContent = data.symbol || "BTC/USDT";

  // Сигнал
  const signalBlock = document.getElementById("signal-block");
  const sig = analysis.signal || "HOLD";
  signalBlock.className = "signal-block signal-" + sig;
  document.getElementById("signal-text").textContent = sig;
  document.getElementById("signal-strength").textContent = (analysis.strength || 0) + "%";

  // Индикаторы
  document.getElementById("rsi").textContent = analysis.rsi ?? "—";
  document.getElementById("macd").textContent = analysis.macd ?? "—";
  document.getElementById("ema-fast").textContent = analysis.ema_fast ?? "—";
  document.getElementById("ema-slow").textContent = analysis.ema_slow ?? "—";
  document.getElementById("bb-upper").textContent = analysis.bb_upper ?? "—";
  document.getElementById("bb-lower").textContent = analysis.bb_lower ?? "—";

  // Статус
  const running = data.running;
  document.getElementById("btn-start").style.display = running ? "none" : "";
  document.getElementById("btn-stop").style.display = running ? "" : "none";
  const badge = document.getElementById("status-badge");
  badge.textContent = running ? "РАБОТАЕТ" : "ОСТАНОВЛЕН";
  badge.className = "badge " + (running ? "badge-running" : "badge-stopped");

  // Демо
  document.getElementById("demo-badge").style.display = data.demo_mode ? "" : "none";

  // Статистика
  document.getElementById("stat-total").textContent = stats.total_trades || 0;
  document.getElementById("stat-winrate").textContent = (stats.winrate || 0) + "%";
  const pnl = stats.total_pnl || 0;
  const pnlEl = document.getElementById("stat-pnl");
  pnlEl.textContent = (pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(2);
  pnlEl.className = "stat-value " + (pnl >= 0 ? "pnl-pos" : "pnl-neg");
  document.getElementById("stat-open").textContent = (data.open_trades || []).length;

  // Баланс
  const bal = data.balance || {};
  const balList = document.getElementById("balance-list");
  balList.innerHTML = Object.entries(bal).map(([k, v]) =>
    `<div class="balance-item"><span class="balance-asset">${k}</span><span class="balance-amount">${Number(v).toFixed(4)}</span></div>`
  ).join("") || '<div class="empty-msg">Нет данных</div>';

  // Открытые сделки
  renderOpenTrades(data.open_trades || []);

  // История
  renderHistory(data.recent_trades || []);

  // Логи
  renderLogs(data.logs || []);
}

function renderOpenTrades(trades) {
  const el = document.getElementById("open-trades-list");
  if (!trades.length) {
    el.innerHTML = '<div class="empty-msg">Нет открытых сделок</div>';
    return;
  }
  el.innerHTML = trades.map(t => `
    <div class="trade-card buy">
      <div class="trade-row">
        <span class="trade-side buy">BUY</span>
        <span style="color:#8892b0;font-size:11px">${t.opened_at ? t.opened_at.slice(11,19) : ""}</span>
      </div>
      <div class="trade-row">
        <span>Вход: <b>$${t.entry_price}</b></span>
        <span>Кол-во: ${t.amount}</span>
      </div>
      <div class="trade-row">
        <span style="color:#ff4d6d">SL: $${t.stop_loss}</span>
        <span style="color:#00d4aa">TP: $${t.take_profit}</span>
      </div>
    </div>
  `).join("");
}

function renderHistory(trades) {
  const el = document.getElementById("trades-history");
  const closed = trades.filter(t => t.status === "closed").reverse();
  if (!closed.length) {
    el.innerHTML = '<div class="empty-msg">История пуста</div>';
    return;
  }
  el.innerHTML = closed.slice(0, 20).map(t => {
    const pnl = t.pnl || 0;
    const cls = pnl >= 0 ? "closed-win" : "closed-loss";
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
          <span>${t.closed_at ? t.closed_at.slice(11,19) : ""}</span>
        </div>
      </div>
    `;
  }).join("");
}

let allLogs = [];
function renderLogs(logs) {
  allLogs = logs;
  const el = document.getElementById("log-container");
  el.innerHTML = [...logs].reverse().slice(0, 80).map(l =>
    `<div class="log-entry log-${l.level}"><span class="log-time">${l.time}</span>${escHtml(l.msg)}</div>`
  ).join("");
}

function clearLogs() {
  document.getElementById("log-container").innerHTML = "";
}

function escHtml(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

async function startAgent() {
  const r = await fetch("/api/start", {method: "POST"});
  const d = await r.json();
  console.log(d.message);
}

async function stopAgent() {
  const r = await fetch("/api/stop", {method: "POST"});
  const d = await r.json();
  console.log(d.message);
}

async function saveConfig() {
  const cfg = {
    symbol: document.getElementById("cfg-symbol").value,
    trade_amount: document.getElementById("cfg-amount").value,
    stop_loss_pct: document.getElementById("cfg-sl").value,
    take_profit_pct: document.getElementById("cfg-tp").value,
    max_open_trades: document.getElementById("cfg-max").value,
  };
  const r = await fetch("/api/config", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(cfg)
  });
  const d = await r.json();
  alert(d.message);
}

// Загружаем конфиг при старте
async function loadConfig() {
  const r = await fetch("/api/config");
  const cfg = await r.json();
  document.getElementById("cfg-symbol").value = cfg.symbol;
  document.getElementById("cfg-amount").value = cfg.trade_amount;
  document.getElementById("cfg-sl").value = cfg.stop_loss_pct;
  document.getElementById("cfg-tp").value = cfg.take_profit_pct;
  document.getElementById("cfg-max").value = cfg.max_open_trades;
  document.getElementById("demo-badge").style.display = cfg.demo_mode ? "" : "none";
}

loadConfig();
