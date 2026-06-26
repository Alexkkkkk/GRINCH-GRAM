// Мгновенная инициализация AI из серверных данных (без ожидания REST-поллинга)
document.addEventListener("DOMContentLoaded", () => {
  if (window._initAI && window._initAI.ai_signal) {
    updateAIPro(window._initAI);
  }
});

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

// Постоянный polling: REST каждые 2 сек (резерв на случай разрыва сокета)
setInterval(() => {
  fetch("/api/status").then(r => r.json()).then(updateUI).catch(() => {});
}, 2000);

function fmtPrice(p) {
  p = Number(p) || 0;
  const digits = p >= 100 ? 2 : (p >= 1 ? 4 : (p >= 0.01 ? 6 : 8));
  return "$" + p.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// Курс GRINCH в GRAM (бывш. Toncoin)
function fmtGram(p) {
  p = Number(p) || 0;
  const digits = p >= 100 ? 2 : (p >= 1 ? 4 : (p >= 0.01 ? 6 : 8));
  return p.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) + " GRAM";
}

let _lastLivePrice = null;
function updatePrice(d) {
  const el = document.getElementById("price");
  if (!el) return;
  const gram = Number(d.gram) || 0;
  const usd  = Number(d.price) || 0;
  // Hero ВСЕГДА в GRAM: при сбое котировки не подменяем доллар, держим прежнее значение
  if (gram > 0) {
    el.textContent = fmtGram(gram);
    if (_lastLivePrice !== null && gram !== _lastLivePrice) {
      el.classList.remove("price-up", "price-down");
      void el.offsetWidth;
      el.classList.add(gram > _lastLivePrice ? "price-up" : "price-down");
    }
    _lastLivePrice = gram;
  }
  const pit = document.getElementById("price-in-ton");
  if (pit && usd > 0) pit.textContent = "≈ " + fmtPrice(usd);
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

  // Курс GRINCH в GRAM (бывш. Toncoin) — основное число; USD — справочно
  const priceFromAnalysis = Number(analysis.price);
  const gram = Number(data.grinch_ton) || 0;
  const priceEl = document.getElementById("price");
  // Hero ВСЕГДА в GRAM: при отсутствии курса не подменяем доллар
  if (priceEl && gram > 0) priceEl.textContent = fmtGram(gram);
  const pitEl = document.getElementById("price-in-ton");
  if (pitEl && priceFromAnalysis > 0) pitEl.textContent = "≈ " + fmtPrice(priceFromAnalysis);
  const symLabel = document.getElementById("symbol-label");
  if (symLabel) symLabel.textContent = "GRINCH/GRAM";

  // Технический сигнал
  const sig = analysis.signal || "HOLD";
  const sb  = document.getElementById("signal-block");
  sb.className = "signal-block signal-" + sig;
  document.getElementById("signal-text").textContent = sig;
  document.getElementById("signal-strength").textContent = (analysis.strength || 0) + "% Tech";

  // AI сигнал (хедер)
  const aiSig = ai.ai_signal || "HOLD";
  const aiSb  = document.getElementById("ai-signal-block");
  aiSb.className = "signal-block signal-" + aiSig;
  document.getElementById("ai-signal-text").textContent = aiSig;
  document.getElementById("ai-confidence").textContent = "AI " + (ai.confidence || 0) + "%";

  // Совместимость: hidden legacy elements (в левой колонке, display:none)
  const trainedBadge = document.getElementById("ai-trained-badge");
  if (trainedBadge) {
    if (ai.model_trained) {
      trainedBadge.textContent = "✓ Обучена";
      trainedBadge.style.background = "#0d2e22";
      trainedBadge.style.color = "#00d4aa";
    } else {
      trainedBadge.textContent = "обучается…";
    }
  }
  const pU = ai.prob_up   || 0;
  const pH = ai.prob_hold || 0;
  const pD = ai.prob_down || 0;
  const barUp = document.getElementById("bar-up");
  if (barUp) barUp.style.width = pU + "%";
  const barH = document.getElementById("bar-hold");
  if (barH) barH.style.width = pH + "%";
  const barD = document.getElementById("bar-down");
  if (barD) barD.style.width = pD + "%";
  const valUp = document.getElementById("val-up");
  if (valUp) valUp.textContent = pU + "%";
  const valH = document.getElementById("val-hold");
  if (valH) valH.textContent = pH + "%";
  const valD = document.getElementById("val-down");
  if (valD) valD.textContent = pD + "%";
  const regime = ai.regime || {};
  const regimeEl = document.getElementById("regime-badge");
  if (regimeEl) regimeEl.textContent = regime.name || "—";
  const anomaly  = ai.anomaly || {};
  const anomEl   = document.getElementById("anomaly-alert");
  if (anomEl) anomEl.style.display = anomaly.detected ? "" : "none";

  // Прогноз
  const fc = ai.forecast || {};
  const fcT1 = document.getElementById("fc-t1");
  const fcT3 = document.getElementById("fc-t3");
  if (fcT1) {
    fcT1.textContent = fc.t1 ? "$" + fc.t1 : "—";
    if (fc.bull !== undefined) fcT1.className = "fc-val " + (fc.bull ? "pnl-pos" : "pnl-neg");
  }
  if (document.getElementById("fc-t2")) document.getElementById("fc-t2").textContent = fc.t2 ? "$" + fc.t2 : "—";
  if (fcT3) {
    fcT3.textContent = fc.t3 ? "$" + fc.t3 : "—";
    if (fc.bull !== undefined) fcT3.className = "fc-val " + (fc.bull ? "pnl-pos" : "pnl-neg");
  }
  const fcRange = document.getElementById("fc-range");
  if (fcRange && fc.range_up && fc.range_down) {
    fcRange.textContent = `$${fc.range_down} – $${fc.range_up}`;
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
  pnlEl.textContent = (pnl >= 0 ? "+" : "") + pnl.toFixed(4) + " TON";
  pnlEl.className = "stat-value " + (pnl >= 0 ? "pnl-pos" : "pnl-neg");
  document.getElementById("stat-open").textContent = (data.open_trades || []).length;

  // Баланс бота (TON + GRINCH с иконками)
  const bal     = data.balance || {};
  const balList = document.getElementById("balance-list");
  const ASSET_META = {
    TON:   { cls: "balance-ton", icon: "◆", aCls: "balance-asset-ton" },
    GRINCH:{ cls: "balance-grn", icon: "🐸", aCls: "balance-asset-grn" },
  };
  balList.innerHTML = Object.entries(bal).map(([k, v]) => {
    const m = ASSET_META[k] || { cls: "", icon: "", aCls: "" };
    return `<div class="balance-item ${m.cls}">
      <span class="balance-asset ${m.aCls}">${m.icon} ${k}</span>
      <span class="balance-amount">${Number(v).toFixed(4)}</span>
    </div>`;
  }).join("") || '<div class="empty-msg">Нет данных</div>';

  // Хедер GRINCH баланс (из бота)
  const hdrGrn = document.getElementById("hdr-grn-bal");
  if (hdrGrn && bal.GRINCH != null) {
    const grn = Number(bal.GRINCH);
    hdrGrn.textContent = grn >= 1000 ? (grn/1000).toFixed(1) + "K" : grn.toFixed(0);
  }

  // Хедер TON баланс (из бота — реальное время в DeDust-режиме)
  if (bal.TON != null) {
    const hdrTon = document.getElementById("hdr-ton-bal");
    if (hdrTon) hdrTon.textContent = Number(bal.TON).toFixed(2);
    // Кошелёк-карточка TON
    const wbTon    = document.getElementById("wb-ton-bal");
    const wbTonUsd = document.getElementById("wb-ton-usd");
    if (wbTon) {
      const tonAmt = Number(bal.TON);
      wbTon.textContent = tonAmt.toFixed(4);
      if (wbTonUsd && window._tonPriceUsd) {
        wbTonUsd.textContent = "≈ $" + (tonAmt * window._tonPriceUsd).toFixed(2);
      }
    }
  }

  // wallet card GRINCH баланс (бот)
  const wbGrn    = document.getElementById("wb-grn-bal");
  const wbGrnUsd = document.getElementById("wb-grn-usd");
  if (wbGrn && bal.GRINCH != null) {
    const grnAmt = Number(bal.GRINCH);
    wbGrn.textContent = grnAmt.toLocaleString("en-US", {maximumFractionDigits: 0});
    const grnUsd = grnAmt * (Number(data.analysis?.price) || 0);
    if (wbGrnUsd) wbGrnUsd.textContent = "≈ $" + grnUsd.toFixed(4);
  }

  renderOpenTrades(data.open_trades  || [], Number(data.analysis?.price) || 0, Number(data.grinch_ton) || 0);
  renderHistory(data.recent_trades || []);
  renderLogs(data.logs             || []);

  // ═══ AI COMMAND CENTER ═══
  updateAIPro(ai);

  // Шкала обучения (берём из статуса если нет отдельного event)
  if (data.training_progress) renderTrainingProgress(data.training_progress);
}

// ═══════════════════════════════════════════════════════
//  AI COMMAND CENTER — всё что ниже
// ═══════════════════════════════════════════════════════

// Храним историю уверенности (последние 40 точек)
const _sparkData = [];
let   _lastAiSignal = null;

// Цвета по сигналу (GRINCH green для BUY)
const SIG_COLOR = { BUY: "#00ff88", SELL: "#ff4d6d", HOLD: "#ffd166" };

// Цвета режимов
const REGIME_COLOR = {
  green: "#00d4aa", red: "#ff4d6d", yellow: "#ffd166",
  blue: "#4f8ef7", purple: "#a78bfa", grey: "#8892b0",
};

// Иконки режимов
const REGIME_ICON = {
  UPTREND: "🚀", DOWNTREND: "📉", VOLATILE: "⚡", RANGING: "↔️", TRANSITION: "🔄",
};

function updateAIPro(ai) {
  if (!ai) return;

  const signal  = ai.ai_signal  || "HOLD";
  const conf    = Number(ai.confidence)  || 0;
  const probUp  = Number(ai.prob_up)   || 0;
  const probH   = Number(ai.prob_hold) || 0;
  const probDn  = Number(ai.prob_down) || 0;
  const regime  = ai.regime  || {};
  const anomaly = ai.anomaly || {};
  const color   = SIG_COLOR[signal] || SIG_COLOR.HOLD;

  // 1. SVG Gauge
  _drawGauge(conf, color, signal);

  // 2. Большой сигнал + glow
  const sigEl = document.getElementById("ai-decision-signal");
  if (sigEl) {
    const changed = _lastAiSignal && _lastAiSignal !== signal;
    sigEl.textContent = signal;
    sigEl.className = "ai-decision-signal ai-ds-" + signal;
    if (changed) {
      sigEl.style.transform = "scale(1.2)";
      setTimeout(() => { sigEl.style.transform = ""; }, 350);
    }
  }
  _lastAiSignal = signal;

  // 3. Текст причины
  const whyEl = document.getElementById("ai-decision-why");
  if (whyEl) whyEl.textContent = _buildReason(ai);

  // 4. Regime chip (маленький, под thinking dots)
  const chipEl = document.getElementById("ai-regime-chip");
  if (chipEl) {
    const rc = REGIME_COLOR[regime.color] || "#8892b0";
    chipEl.textContent = regime.name || "—";
    chipEl.style.color = rc;
    chipEl.style.borderColor = rc + "80";
    chipEl.style.background  = rc + "18";
  }

  // 5. Вертикальные столбцы вероятностей
  _setVbar("vpb-up",   "vpv-up",   probUp);
  _setVbar("vpb-hold", "vpv-hold", probH);
  _setVbar("vpb-down", "vpv-down", probDn);

  // 6. Regime banner
  _updateRegimeBanner(regime);

  // 7. Sparkline — добавляем точку, перерисовываем
  _sparkData.push(conf);
  if (_sparkData.length > 40) _sparkData.shift();
  _drawSparkline(conf);

  // 8. Anomaly alert
  const anomBanner = document.getElementById("ai-anomaly");
  const anomText   = document.getElementById("ai-anomaly-text");
  if (anomBanner) {
    anomBanner.style.display = anomaly.detected ? "flex" : "none";
    if (anomText && anomaly.detected) {
      anomText.textContent = anomaly.description
        ? `${anomaly.description} (Z-цена=${anomaly.z_price}, Z-объём=${anomaly.z_volume})`
        : "Аномальное движение";
    }
  }

  // 9. Training badge (в command center)
  const tb2 = document.getElementById("ai-trained-badge2");
  const sampEl = document.getElementById("ai-samples");
  if (tb2) {
    if (ai.model_trained) {
      tb2.textContent = "✓ Обучена";
      tb2.style.background = "#0d2e22";
      tb2.style.color = "#00d4aa";
    } else {
      tb2.textContent = "обучается…";
      tb2.style.background = "#3b3228";
      tb2.style.color = "#ffd166";
    }
  }
  if (sampEl) sampEl.textContent = ai.samples_trained || 0;

  // 10. Метка времени последнего обновления
  const updEl = document.getElementById("ai-last-update");
  if (updEl) {
    const now = new Date();
    updEl.textContent = "обновлено " + now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
}

// ─── SVG Gauge ───────────────────────────────────────
function _drawGauge(pct, color, signal) {
  // r=48, cx=60, cy=65 → C = 2π*48 ≈ 301.59
  // 270° arc = 0.75 * C ≈ 226.19
  const ARC = 226.19;
  const filled = ARC * Math.min(100, Math.max(0, pct)) / 100;

  const fill = document.getElementById("gauge-fill");
  if (fill) {
    fill.setAttribute("stroke-dasharray", filled.toFixed(2) + " 302");
    fill.setAttribute("stroke", color);
    fill.style.filter = `drop-shadow(0 0 7px ${color}aa)`;
  }

  const pctTxt = document.getElementById("gauge-pct");
  if (pctTxt) {
    pctTxt.textContent = Math.round(pct) + "%";
    pctTxt.setAttribute("fill", color);
  }

  const sigTxt = document.getElementById("gauge-sig");
  if (sigTxt) {
    sigTxt.textContent = signal;
    sigTxt.setAttribute("fill", color);
  }
}

// ─── Vertical probability bar ────────────────────────
function _setVbar(barId, valId, pct) {
  const bar = document.getElementById(barId);
  const val = document.getElementById(valId);
  if (bar) bar.style.height = Math.max(2, pct) + "%";
  if (val) val.textContent = pct + "%";
}

// ─── Regime banner ───────────────────────────────────
function _updateRegimeBanner(regime) {
  const banner  = document.getElementById("ai-regime-banner");
  const iconEl  = document.getElementById("ai-rb-icon");
  const nameEl  = document.getElementById("ai-rb-name");
  const descEl  = document.getElementById("ai-rb-desc");
  const atrEl   = document.getElementById("ai-rb-atr");
  const volEl   = document.getElementById("ai-rb-vol");

  if (!banner) return;
  const rc   = REGIME_COLOR[regime.color] || "#8892b0";
  const icon = REGIME_ICON[regime.name]   || "📊";

  banner.style.borderLeftColor = rc;
  banner.style.background      = rc + "0d";

  if (iconEl)  iconEl.textContent = icon;
  if (nameEl) { nameEl.textContent = regime.name || "—"; nameEl.style.color = rc; }
  if (descEl)  descEl.textContent = regime.desc  || "—";
  if (atrEl)   atrEl.textContent  = (Number(regime.atr_pct) || 0).toFixed(2) + "%";
  if (volEl)   volEl.textContent  = (Number(regime.vol_ratio) || 1).toFixed(1) + "x";
}

// ─── Текст объяснения сигнала ─────────────────────────
function _buildReason(ai) {
  const sig    = ai.ai_signal  || "HOLD";
  const conf   = Math.round(ai.confidence || 0);
  const regime = (ai.regime || {}).name || "";
  const pats   = (ai.patterns || []).map(p => p.name).slice(0, 2).join(", ");
  const slope  = ((ai.forecast || {}).slope_pct || 0);

  const slopeStr = slope !== 0 ? ` · тренд ${slope > 0 ? "+" : ""}${slope.toFixed(3)}%` : "";
  const patStr   = pats ? ` · ${pats}` : "";

  if (sig === "BUY")
    return `Сильный бычий сигнал (${conf}%) · ${regime}${patStr}${slopeStr}`;
  if (sig === "SELL")
    return `Медвежий разворот (${conf}%) · ${regime}${patStr}${slopeStr}`;
  return `Нейтральная зона (${conf}%) · ${regime}${patStr}${slopeStr}`;
}

// ─── Canvas Sparkline ────────────────────────────────
function _drawSparkline(latest) {
  const canvas = document.getElementById("ai-sparkline");
  if (!canvas) return;

  // Синхронизируем ширину
  const W = canvas.parentElement ? canvas.parentElement.clientWidth : 200;
  canvas.width  = W;
  canvas.height = 40;

  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, W, 40);

  const data = _sparkData;
  if (data.length < 2) return;

  const minV = Math.max(0,   Math.min(...data) - 5);
  const maxV = Math.min(100, Math.max(...data) + 5);
  const range = maxV - minV || 10;

  function xOf(i) { return (i / (data.length - 1)) * W; }
  function yOf(v) { return 36 - ((v - minV) / range) * 32; }

  // Плавная кривая (Catmull-Rom-like via bezier)
  ctx.beginPath();
  for (let i = 0; i < data.length; i++) {
    const x = xOf(i), y = yOf(data[i]);
    if (i === 0) { ctx.moveTo(x, y); continue; }
    const px = xOf(i - 1), py = yOf(data[i - 1]);
    ctx.bezierCurveTo(px + (x - px) / 2, py, x - (x - px) / 2, y, x, y);
  }

  // Градиент заливки
  const grad = ctx.createLinearGradient(0, 0, 0, 40);
  grad.addColorStop(0, "rgba(79,142,247,0.35)");
  grad.addColorStop(1, "rgba(79,142,247,0)");
  ctx.lineTo(W, 40); ctx.lineTo(0, 40); ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Линия
  ctx.beginPath();
  for (let i = 0; i < data.length; i++) {
    const x = xOf(i), y = yOf(data[i]);
    if (i === 0) { ctx.moveTo(x, y); continue; }
    const px = xOf(i - 1), py = yOf(data[i - 1]);
    ctx.bezierCurveTo(px + (x - px) / 2, py, x - (x - px) / 2, y, x, y);
  }
  ctx.strokeStyle = "#4f8ef7";
  ctx.lineWidth   = 1.8;
  ctx.stroke();

  // Пульсирующая точка — последнее значение
  const lx = xOf(data.length - 1);
  const ly = yOf(data[data.length - 1]);
  ctx.beginPath(); ctx.arc(lx, ly, 3.5, 0, Math.PI * 2);
  ctx.fillStyle = "#4f8ef7"; ctx.fill();
  ctx.beginPath(); ctx.arc(lx, ly, 5.5, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(79,142,247,0.4)"; ctx.lineWidth = 1.5; ctx.stroke();

  // Метка текущего значения
  const sparkCur = document.getElementById("ai-spark-last");
  if (sparkCur) sparkCur.textContent = (latest || 0).toFixed(1) + "%";
}

// ════════════════════════════════════════════════════
//  Рендеры (патерны, FI, сделки, логи, S/R)
// ════════════════════════════════════════════════════

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
    const cls  = p.type === "bullish" ? "pat-bull" : p.type === "bearish" ? "pat-bear" : "pat-neut";
    const icon = p.type === "bullish" ? "🟢" : p.type === "bearish" ? "🔴" : "🟡";
    return `<div class="pattern-item ${cls}">${icon} <b>${p.name}</b> — <span>${p.desc}</span></div>`;
  }).join("");
}

function renderFeatureImportance(fi) {
  const el = document.getElementById("fi-list");
  if (!fi.length) { el.innerHTML = '<div class="empty-msg">Нет данных</div>'; return; }
  const max = fi[0].importance;
  el.innerHTML = fi.map((f, idx) => {
    const w = (f.importance / max * 100).toFixed(0);
    // цвет по рангу
    const hue = Math.round(270 - idx * 22);
    const barColor = `hsl(${hue},80%,60%)`;
    return `
    <div class="fi-row">
      <span class="fi-name">${f.feature}</span>
      <div class="fi-bar-wrap">
        <div class="fi-bar" style="width:${w}%;background:linear-gradient(90deg,${barColor},${barColor}88)"></div>
      </div>
      <span class="fi-val" style="color:${barColor}">${f.importance}%</span>
    </div>`;
  }).join("");
}

function renderOpenTrades(trades, curPrice, gramPrice) {
  const el = document.getElementById("open-trades-list");
  if (!trades.length) {
    el.innerHTML = '<div class="empty-msg">Нет позиций, ожидающих продажи</div>';
    return;
  }
  const gram = Number(gramPrice) || 0;
  el.innerHTML = trades.map(t => {
    const amount = Number(t.amount) || 0;
    const valueGram = gram > 0 ? amount * gram : 0;
    const entry = Number(t.entry_price) || 0;
    const tp    = Number(t.take_profit) || 0;
    const sl    = Number(t.stop_loss)   || 0;
    const cur   = curPrice > 0 ? curPrice : entry;

    // Чистый результат «если продать сейчас» — авторитетный расчёт бэкенда
    // (net_pct_now: учитывает обе комиссии 1%+1% и газ). Фолбэк — оценка по
    // цене с круговой комиссией 2%, если бэкенд не прислал значения.
    const hasNet   = (t.net_pct_now !== undefined && t.net_pct_now !== null);
    const grossPct = entry > 0 ? (cur - entry) / entry * 100 : 0;
    const netPct   = hasNet ? Number(t.net_pct_now)
                            : grossPct - (entry > 0 ? (2 + grossPct / 100) : 2);
    const netTon   = (t.net_ton_now !== undefined && t.net_ton_now !== null) ? Number(t.net_ton_now) : null;
    const be       = Number(t.breakeven_price) || 0;
    const inProfit = (t.in_profit !== undefined && t.in_profit !== null) ? !!t.in_profit : netPct >= 0;
    const pnlCls   = inProfit ? "pnl-pos" : "pnl-neg";
    const pnlSign  = netPct >= 0 ? "+" : "";

    // Прогресс от входа к тейк-профиту (0..100%)
    let progress = 0;
    if (tp > entry) progress = Math.min(100, Math.max(0, (cur - entry) / (tp - entry) * 100));
    const barColor = netPct >= 0 ? "linear-gradient(90deg,#ffd166,#00ff88)" : "linear-gradient(90deg,#ff4d6d,#ffd166)";

    // Сколько ещё % до цели продажи
    const toTpPct = (tp > 0 && cur > 0) ? (tp - cur) / cur * 100 : 0;

    // Статус ожидания — главный сигнал: уже в плюсе после ОБЕИХ комиссий?
    let waitLabel, waitColor;
    if (inProfit)                 { waitLabel = "✅ Уже в плюсе — можно закрыть"; waitColor = "#00ff88"; }
    else if (be > 0 && cur > 0)   { waitLabel = `⏳ До прибыли ещё +${((be - cur) / cur * 100).toFixed(1)}%`; waitColor = "#ffd166"; }
    else if (cur >= tp)           { waitLabel = "🎯 Достигнут TP — продаём"; waitColor = "#00ff88"; }
    else if (sl > entry)          { waitLabel = "🔒 Прибыль защищена (трейлинг)"; waitColor = "#00d4aa"; }
    else                          { waitLabel = `⏳ Ждём роста ещё +${toTpPct.toFixed(1)}%`; waitColor = "#ffd166"; }

    return `
    <div class="trade-card buy waiting-sell">
      <div class="trade-row">
        <span class="trade-side buy">⏳ ОЖИДАЕТ ПРОДАЖИ</span>
        <span class="${pnlCls}" style="font-weight:700">${pnlSign}${netPct.toFixed(2)}%</span>
      </div>
      <div class="trade-row" style="font-size:11px;color:#8892b0">
        <span>Вход: <b style="color:#e2e8f0">$${entry}</b></span>
        <span>Сейчас: <b style="color:#e2e8f0">$${cur}</b></span>
      </div>
      <!-- Прогресс-бар к тейк-профиту -->
      <div class="ot-prog-wrap" title="Прогресс к тейк-профиту">
        <div class="ot-prog-bar" style="width:${progress.toFixed(1)}%;background:${barColor}"></div>
      </div>
      <div class="trade-row" style="font-size:10px">
        <span style="color:#ff4d6d">SL $${sl}</span>
        <span style="color:#00d4aa">TP $${tp}</span>
      </div>
      <div class="trade-row">
        <span class="ot-wait" style="color:${waitColor}">${waitLabel}</span>
      </div>
      <div class="trade-row" style="font-size:10px;color:#4a5568">
        <span>Кол-во: <b style="color:#e2e8f0">${amount}</b> GRINCH</span>
        ${t.ai_confidence ? `<span style="color:#a78bfa">AI ${t.ai_confidence}%</span>` : ""}
      </div>
      <div class="trade-row" style="font-size:11px;color:#8892b0">
        <span>Куплено по: <b style="color:#e2e8f0">$${entry}</b></span>
        <span>Стоит сейчас: <b style="color:#00d4aa">${gram > 0 ? fmtGram(valueGram) : "—"}</b></span>
      </div>
      <div class="trade-row" style="font-size:12px;align-items:center">
        <span style="color:#8892b0">Если продать сейчас (−комиссии):</span>
        <b class="${pnlCls}" style="font-weight:800">${netTon !== null ? (netTon >= 0 ? "+" : "−") + fmtGram(Math.abs(netTon)) : "—"}</b>
      </div>
      ${be > 0 ? `<div class="trade-row" style="font-size:10px;color:#4a5568">
        <span>Безубыток (с учётом 2 транзакций): <b style="color:#ffd166">$${be}</b></span>
      </div>` : ""}
      <button onclick='closeTrade(this, ${JSON.stringify(String(t.id))})'
        style="margin-top:8px;width:100%;padding:9px;border:none;border-radius:8px;cursor:pointer;font-weight:800;font-size:12px;color:#fff;background:${inProfit ? "linear-gradient(90deg,#00b894,#00ff88)" : "linear-gradient(90deg,#ff4d6d,#ff7a3d)"}">
        ${inProfit ? "✅ Продать с прибылью" : "✖ Продать сейчас"}
      </button>
    </div>`;
  }).join("");
}

async function closeTrade(btn, id) {
  if (!confirm("Закрыть эту позицию? GRINCH будет продан на DeDust по текущей рыночной цене.")) return;
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Продаю…"; }
  try {
    const r = await fetch("/api/trade/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id })
    });
    const d = await r.json().catch(() => ({ ok: false, error: "ошибка ответа" }));
    if (!d.ok) {
      alert("Не удалось закрыть: " + (d.error || "ошибка"));
      if (btn) { btn.disabled = false; btn.textContent = "✖ Продать сейчас"; }
    }
  } catch (e) {
    alert("Ошибка сети при закрытии позиции");
    if (btn) { btn.disabled = false; btn.textContent = "✖ Продать сейчас"; }
  }
  fetch("/api/status").then(r => r.json()).then(updateUI).catch(() => {});
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
          <span class="${pnlCls}">${pnl >= 0 ? "+" : ""}${pnl.toFixed(4)} TON</span>
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
function escHtml(s)  { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

// ════════════════════════════════════════════════════
//  Управление (кнопки, настройки)
// ════════════════════════════════════════════════════

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
  if (!d.ok) { alert(d.message); loadConfig(); return; }
  _lastLivePrice = null;
  document.getElementById("symbol-label").textContent = symbol;
  document.getElementById("price").textContent = "…";
  document.getElementById("price-change").textContent = "—";
}

async function saveConfig() {
  const cfg = {
    symbol:             document.getElementById("cfg-symbol").value,
    trade_amount:       document.getElementById("cfg-amount").value,
    take_profit_pct:    document.getElementById("cfg-tp").value,
    trailing_stop_pct:  document.getElementById("cfg-trail").value,
    fee_pct:            document.getElementById("cfg-fee").value,
    min_ai_confidence:  document.getElementById("cfg-minconf").value,
    max_open_trades:    document.getElementById("cfg-max").value,
    use_dynamic_targets:document.getElementById("cfg-dyn").checked,
    trend_filter:       document.getElementById("cfg-trend").checked,
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
  // Обновляем wallet card балансы из трекера
  const wbTon = document.getElementById("wb-ton-bal");
  const wbTonUsd = document.getElementById("wb-ton-usd");
  const hdrTon = document.getElementById("hdr-ton-bal");
  if (d.balance != null) {
    const tonBal = Number(d.balance);
    if (wbTon) wbTon.textContent = tonBal.toFixed(4);
    if (hdrTon) hdrTon.textContent = tonBal.toFixed(2);
    if (wbTonUsd && window._tonPriceUsd) {
      wbTonUsd.textContent = "≈ $" + (tonBal * window._tonPriceUsd).toFixed(2);
    }
  }
  const errEl = document.getElementById("ton-error");
  if (d.last_error) { errEl.style.display = ""; errEl.textContent = "⚠ " + d.last_error; }
  else { errEl.style.display = "none"; }
  const box = document.getElementById("ton-deposits");
  if (!d.deposits || d.deposits.length === 0) {
    box.innerHTML = '<div class="ton-empty">Поступлений пока нет</div>';
    return;
  }
  box.innerHTML = d.deposits.slice(0, 15).map(dep => {
    const dt = dep.time ? new Date(dep.time * 1000).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "";
    return `<div class="ton-dep">
      <div class="ton-dep-top">
        <span class="ton-dep-amount">+${dep.amount} TON</span>
        <span class="ton-dep-time">${dt}</span>
      </div>
      <div class="ton-dep-from">от ${escapeHtml(dep.from_short || "")}</div>
    </div>`;
  }).join("");
}

function escapeHtml(s) {
  const d = document.createElement("div"); d.textContent = s; return d.innerHTML;
}

async function loadTon() {
  try { const r = await fetch("/api/ton"); renderTon(await r.json()); } catch (e) {}
}
async function refreshTon() {
  const btn = document.querySelector(".btn-wallet-refresh");
  if (btn) btn.classList.add("spin");
  try { const r = await fetch("/api/ton/refresh", { method: "POST" }); renderTon(await r.json()); } catch (e) {}
  if (btn) setTimeout(() => btn.classList.remove("spin"), 600);
}
// Загружаем цену TON/USDT через серверный прокси (без CORS)
async function loadTonPrice() {
  try {
    const r = await fetch("/api/ton/price");
    const d = await r.json();
    if (d?.price > 0) { window._tonPriceUsd = d.price; return; }
  } catch (_) {}
  window._tonPriceUsd = window._tonPriceUsd || 2.44;
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
    if (d.image) { img.src = d.image; img.style.display = "block"; } else { img.style.display = "none"; }
    document.getElementById("coin-name").textContent = d.name || d.symbol;
    document.getElementById("coin-sym").textContent  = d.symbol || "—";
    document.getElementById("coin-source").textContent = d.source ? "· " + d.source : "";
    document.getElementById("coin-price").textContent  = d.price_usd != null ? fmtPrice(d.price_usd) : "—";
    const ch = document.getElementById("coin-change");
    if (d.change_h24 != null) {
      const up = d.change_h24 >= 0;
      ch.textContent = (up ? "+" : "") + d.change_h24.toFixed(2) + "%";
      ch.className = "cs-val " + (up ? "pos" : "neg");
    } else { ch.textContent = "—"; ch.className = "cs-val"; }
    document.getElementById("coin-vol").textContent  = d.volume_h24 != null ? fmtBig(d.volume_h24) : "—";
    document.getElementById("coin-liq").textContent  = d.liquidity  != null ? fmtBig(d.liquidity)  : "—";
    document.getElementById("coin-mcap").textContent = d.market_cap != null ? fmtBig(d.market_cap) : "—";
    const tx = document.getElementById("coin-txns");
    if (d.buys_h24 != null || d.sells_h24 != null) {
      tx.innerHTML = '<span class="pos">' + (Number(d.buys_h24)||0) + '↑</span> / <span class="neg">' + (Number(d.sells_h24)||0) + '↓</span>';
    } else { tx.textContent = "—"; }
    const link = document.getElementById("coin-link");
    if (d.url) { link.href = d.url; link.style.display = "inline"; } else { link.style.display = "none"; }
  } catch (e) {}
}

async function loadDexTrades() {
  try {
    const r   = await fetch("/api/coin/trades");
    const arr = await r.json();
    const box  = document.getElementById("dex-trades");
    const note = document.getElementById("trades-note");
    if (!Array.isArray(arr) || arr.length === 0) {
      box.innerHTML = '<div class="empty-msg">Лента доступна для GRINCH</div>';
      note.textContent = ""; return;
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
  } catch (e) {}
}

async function loadExchanges() {
  try {
    const r    = await fetch("/api/coin/exchanges");
    const d    = await r.json();
    const list = document.getElementById("exch-list");
    const rows = (d && d.exchanges) || [];
    const cnt  = document.getElementById("exch-count");
    const aiBox = document.getElementById("exch-ai");
    if (rows.length === 0) {
      list.innerHTML = '<div class="empty-msg">Нет данных</div>';
      cnt.textContent = ""; aiBox.style.display = "none"; return;
    }
    cnt.textContent = "· " + rows.length + " бирж";
    const agg = d.agg;
    if (agg) {
      aiBox.style.display = "block";
      const sig = document.getElementById("exch-signal");
      sig.textContent = agg.signal;
      sig.className = "exch-ai-signal sig-" + (agg.signal === "АРБИТРАЖ" ? "arb" : (agg.signal === "РАСХОЖДЕНИЕ" ? "div" : "con"));
      document.getElementById("exch-spread").textContent = "спред " + agg.spread_pct + "%";
      document.getElementById("exch-note").textContent   = agg.note || "";
      document.getElementById("exch-avg").textContent    = fmtPrice(agg.avg_price);
      document.getElementById("exch-buy").textContent  = agg.best_buy  ? escapeHtml(agg.best_buy.name)  + " " + fmtPrice(agg.best_buy.price)  : "—";
      document.getElementById("exch-sell").textContent = agg.best_sell ? escapeHtml(agg.best_sell.name) + " " + fmtPrice(agg.best_sell.price) : "—";
    } else { aiBox.style.display = "none"; }
    list.innerHTML = rows.map(e => {
      const chv = (e.change24h != null && isFinite(Number(e.change24h))) ? Number(e.change24h) : null;
      const ch  = chv != null
        ? '<span class="ex-ch ' + (chv >= 0 ? "pos" : "neg") + '">' + (chv >= 0 ? "+" : "") + chv.toFixed(2) + '%</span>'
        : '<span class="ex-ch"></span>';
      const liqOrVol = e.liquidity != null ? ("Ликв " + fmtBig(e.liquidity)) : (e.volume24h != null ? ("Об " + fmtBig(e.volume24h)) : "");
      return '<div class="ex-row">' +
        '<span class="ex-name">' + escapeHtml(e.name) + '<span class="ex-kind">' + escapeHtml(e.kind || "") + '</span></span>' +
        '<span class="ex-price">' + fmtPrice(e.price) + '</span>' + ch +
        '<span class="ex-liq">' + escapeHtml(liqOrVol) + '</span>' +
        '</div>';
    }).join("");
  } catch (e) {}
}

// Сначала цена TON, затем wallet (чтобы USD значения показались сразу)
loadTonPrice().then(() => loadTon());
loadConfig();
loadCoin();
loadDexTrades();
loadExchanges();
setInterval(() => loadTonPrice().then(() => loadTon()), 60000);
setInterval(loadTon, 15000);
setInterval(loadCoin, 10000);
setInterval(loadDexTrades, 8000);
setInterval(loadExchanges, 15000);

// ═══════════════════════════════════════════════════════════════════════════
//  ШКАЛА ОБУЧЕНИЯ AI
// ═══════════════════════════════════════════════════════════════════════════

const TB_STAGE_ORDER = ["collecting", "features", "rf", "gb", "validate", "ready"];

// Прогресс приходит двумя путями:
// 1) SocketIO event "training_progress" (в реальном времени)
// 2) поле training_progress в updateUI (polling fallback, уже встроен выше)
socket.on("training_progress", renderTrainingProgress);

function renderTrainingProgress(tp) {
  if (!tp) return;
  const banner = document.getElementById("training-banner");
  if (!banner) return;

  const phase   = tp.phase   || "idle";
  const pct     = Math.min(100, Math.max(0, Number(tp.pct) || 0));
  const label   = tp.label   || "";
  const samples = tp.samples || 0;
  const isDone  = phase === "ready" && pct >= 100;

  banner.style.display    = "block";
  banner.style.opacity    = "1";
  banner.style.maxHeight  = "";
  banner.style.transition = "";

  const fill  = document.getElementById("tb-fill");
  const pctEl = document.getElementById("tb-pct");
  const lbl   = document.getElementById("tb-label");
  const samp  = document.getElementById("tb-samples");
  const icon  = document.getElementById("tb-icon");

  if (fill) {
    fill.style.width = pct + "%";
    isDone ? fill.classList.add("ready") : fill.classList.remove("ready");
  }
  if (pctEl) pctEl.textContent = pct + "%";
  if (lbl)   lbl.textContent   = label;
  if (samp && samples > 0) samp.textContent = samples.toLocaleString("ru-RU");

  const ICONS = { idle:"🧠", collecting:"📡", features:"🔬", rf:"🌲", gb:"🚀", validate:"🔎", ready:"✅" };
  if (icon) icon.textContent = ICONS[phase] || "🧠";

  const phaseIdx = TB_STAGE_ORDER.indexOf(phase);
  TB_STAGE_ORDER.forEach((s, i) => {
    const el = document.getElementById("ts-" + s);
    if (!el) return;
    el.className = "tb-stage " + (i < phaseIdx ? "done" : i === phaseIdx ? "active" : "pending");
  });

  // Банер обучения показываем ПОСТОЯННО (не скрываем после завершения).
  // После предобучения он отражает непрерывное самообучение модели.
}

// ══════════════════════════════════════════════════════════════════
//  Авто-ликвидатор GRINCH
// ══════════════════════════════════════════════════════════════════
function fmt8(v) {
  if (v == null) return "—";
  return "$" + Number(v).toFixed(8);
}

function updateLiquidator(d) {
  const bal = d.grinch_balance || 0;

  // Баланс
  const balEl = document.getElementById("liq-bal");
  if (balEl) balEl.textContent = bal > 0 ? bal.toFixed(4) + " GRINCH" : "0 GRINCH";

  // TON для газа + предупреждение
  const tonEl  = document.getElementById("liq-ton");
  const warnEl = document.getElementById("liq-gas-warn");
  if (tonEl) {
    if (d.ton_balance != null) {
      tonEl.textContent = d.ton_balance.toFixed(3) + " TON";
      tonEl.style.color = d.gas_ok === false ? "var(--red)" : "var(--green)";
    } else {
      tonEl.textContent = "—";
      tonEl.style.color = "";
    }
  }
  if (warnEl) warnEl.style.display = (d.gas_ok === false) ? "block" : "none";

  // Цены
  const refEl  = document.getElementById("liq-ref");
  const curEl  = document.getElementById("liq-cur");
  const tgtEl  = document.getElementById("liq-tgt");
  const pctEl  = document.getElementById("liq-pct");
  const barEl  = document.getElementById("liq-bar");
  const msgEl  = document.getElementById("liq-msg");

  if (refEl) refEl.textContent = d.ref_price ? fmt8(d.ref_price) : "—";
  if (curEl) curEl.textContent = d.current_price ? fmt8(d.current_price) : "—";
  if (tgtEl) tgtEl.textContent = d.target_price  ? fmt8(d.target_price) + " (+" + d.sell_rise_pct + "%)" : "—";

  // Изменение цены с опорной
  if (pctEl) {
    if (d.pct_now != null) {
      const sign = d.pct_now >= 0 ? "+" : "";
      pctEl.textContent  = sign + d.pct_now.toFixed(2) + "%";
      pctEl.style.color  = d.pct_now >= d.sell_rise_pct ? "var(--green)" : d.pct_now >= 0 ? "#ffd166" : "var(--red)";
    } else {
      pctEl.textContent = "—";
      pctEl.style.color = "";
    }
  }

  // Прогресс-бар: 0% = нет роста, 100% = достигли цели
  if (barEl && d.sell_rise_pct > 0 && d.pct_now != null) {
    const prog = Math.min(100, Math.max(0, (d.pct_now / d.sell_rise_pct) * 100));
    barEl.style.width = prog.toFixed(1) + "%";
  } else if (barEl) {
    barEl.style.width = "0%";
  }

  // Сообщение
  if (msgEl) {
    if (bal < 0.5) {
      msgEl.textContent = "GRINCH на кошельке не обнаружен";
    } else if (d.last_sell_at) {
      msgEl.textContent = "Последняя продажа: " + d.last_sell_at + " (всего: " + d.sell_count + ")";
    } else if (d.target_price) {
      const pctLeft = d.pct_to_go != null ? d.pct_to_go.toFixed(2) + "% до цели" : "";
      msgEl.textContent = "Жду роста +" + d.sell_rise_pct + "% | " + pctLeft;
    } else {
      msgEl.textContent = "Ожидание данных...";
    }
  }

  // Подсветить карточку если баланс > 0
  const card = document.getElementById("liquidator-card");
  if (card) {
    card.style.borderColor = bal >= 0.5 ? "rgba(0,255,136,0.3)" : "";
  }
}

// Периодически обновляем статус ликвидатора
function pollLiquidator() {
  fetch("/api/liquidator")
    .then(r => r.json())
    .then(d => updateLiquidator(d))
    .catch(() => {});
}
pollLiquidator();
setInterval(pollLiquidator, 20000);

// Ручная продажа
function forceLiqSell() {
  const btn = document.getElementById("liq-sell-btn");
  const st  = document.getElementById("liq-sell-status");
  if (btn) btn.disabled = true;
  if (st)  st.textContent = "Отправляю...";
  fetch("/api/liquidator/sell", { method: "POST" })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        if (st) st.textContent = "✅ Продано " + (d.grinch_sold || 0).toFixed(4) + " GRINCH";
      } else {
        if (st) st.textContent = "⚠️ " + (d.error || "Ошибка");
      }
      if (btn) btn.disabled = false;
      setTimeout(() => { if (st) st.textContent = ""; }, 8000);
      pollLiquidator();
    })
    .catch(() => { if (btn) btn.disabled = false; });
}

// Изменить порог
function setLiqThreshold(val) {
  const pct = parseFloat(val);
  if (isNaN(pct) || pct < 0.5) return;
  fetch("/api/liquidator/threshold", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pct })
  }).then(() => pollLiquidator()).catch(() => {});
}
