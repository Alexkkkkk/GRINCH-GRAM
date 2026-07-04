// ═══ Toast-уведомления (заменяет alert(), который блокируется в iframe) ═══
function showToast(msg, type) {
  // type: "ok" | "err" | "info"  (default: "info")
  const container = document.getElementById("toast-container");
  if (!container) { console.log("[toast]", msg); return; }
  const t = document.createElement("div");
  t.className = "toast toast-" + (type || "info");
  t.textContent = msg;
  container.appendChild(t);
  requestAnimationFrame(() => requestAnimationFrame(() => t.classList.add("show")));
  setTimeout(() => {
    t.classList.remove("show");
    setTimeout(() => t.remove(), 300);
  }, type === "err" ? 5000 : 3000);
}

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

// Буфер последних данных на клиенте: если пришёл пустой/битый ответ или
// оборвалась связь — НЕ затираем экран, показываем последнее известное значение.
let _lastGoodStatus = null;
function updateUI(data) {
  const valid = data && typeof data === "object" &&
                (data.analysis || data.stats || data.ai || data.open_trades);
  if (!valid) {
    if (!_lastGoodStatus) return;   // нечего показывать — оставляем как есть
    data = _lastGoodStatus;
  } else {
    _lastGoodStatus = data;
  }
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

  // Индикаторы (legacy hidden spans + полный обзор рынка)
  document.getElementById("rsi").textContent      = analysis.rsi ?? "—";
  document.getElementById("macd").textContent     = analysis.macd ?? "—";
  document.getElementById("ema-fast").textContent = analysis.ema_fast ?? "—";
  document.getElementById("ema-slow").textContent = analysis.ema_slow ?? "—";
  document.getElementById("bb-upper").textContent = analysis.bb_upper ?? "—";
  document.getElementById("bb-lower").textContent = analysis.bb_lower ?? "—";
  renderMarketOverview(analysis);
  renderAIAnalytics(analysis);
  updateTicker(data);
  renderDecisionLog(data.decision_log || []);
  updateDBSync(data.db_synced_secs != null ? data.db_synced_secs : null);
  updateMomentum(ai.momentum || null, data.ai_full_rights_active);
  updateBreakout(ai.breakout || null);

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

  // Portfolio total + ROI tracker
  _updatePortfolioTracker(bal, data.analysis, stats);

  renderOpenTrades(data.open_trades  || [], Number(data.analysis?.price) || 0, Number(data.grinch_ton) || 0);
  renderOpenShortTrades(data.open_short_trades || [], Number(data.analysis?.price) || 0, Number(data.grinch_ton) || 0);
  renderHistory(data.recent_trades || []);
  renderLogs(data.logs             || []);

  // ═══ AI COMMAND CENTER ═══
  updateAIPro(ai);

  // Шкала обучения (берём из статуса если нет отдельного event)
  if (data.training_progress) renderTrainingProgress(data.training_progress);

  // Smart BUY: индикатор ожидания откатного входа
  renderSmartBuy(data.pending_buy || null);

  // Качество точки входа (A/B/C-грейд + факторы)
  renderEntryQuality(data.entry_quality || null, data.analysis?.signal || "HOLD");

  // Умные деньги + AI-управление (защита капитала, просадка)
  renderSmartMoneyBar(data.smart_money || null);
  renderAIManagement(data.ai_management || null);

  // DCA стратегия — статус текущего цикла
  if (data.dca_mode) renderDcaState(data.dca_state || null, data.dca_mode);
}

function renderDcaState(st, active) {
  const panel = document.getElementById("dca-status-panel");
  if (!panel) return;
  panel.style.display = active ? "" : "none";
  if (!active || !st) return;

  const g = id => document.getElementById(id);
  // Фаза: поля из get_status() — wait_pullback, entries_count
  const phaseMap = {
    "idle":    "⏳ Ожидание входа",
    "buying":  "🟢 Набор позиции",
    "waiting": "📉 Ожидание отката",
  };
  const phase = st.wait_pullback ? "waiting" : (st.entries_count > 0 ? "buying" : "idle");
  if (g("dca-phase"))   g("dca-phase").textContent  = phaseMap[phase] || phase;
  if (g("dca-entries")) g("dca-entries").textContent = (st.entries_count ?? "—") + " / " + (st.max_entries ?? "—");
  if (g("dca-stake"))   g("dca-stake").textContent   = st.total_stake != null ? Number(st.total_stake).toFixed(2) + " TON" : "—";

  // Прибыль портфеля (поле portfolio_pct из get_status)
  const profit = st.portfolio_pct;
  const target = st.target_pct ?? 20;
  if (g("dca-profit"))       g("dca-profit").textContent      = profit != null ? (profit >= 0 ? "+" : "") + Number(profit).toFixed(2) + "%" : "—";
  if (g("dca-target-label")) g("dca-target-label").textContent = "+" + target + "%";

  // Прогресс-бар
  const bar = document.getElementById("dca-progress-bar");
  if (bar && profit != null) {
    const pct = Math.max(0, Math.min(100, (Number(profit) / target) * 100));
    bar.style.width = pct + "%";
    bar.style.background = pct >= 100 ? "linear-gradient(90deg,#00ff88,#ffd166)" : "linear-gradient(90deg,#00ff88,#00d4ff)";
  }

  // Цены (поля last_buy_price, peak_price из get_status)
  if (g("dca-last-buy")) g("dca-last-buy").textContent = st.last_buy_price > 0
    ? fmtGram(st.last_buy_price) : "—";
  if (g("dca-peak"))     g("dca-peak").textContent     = st.peak_price > 0
    ? fmtGram(st.peak_price) : "—";
}

function renderSmartBuy(pb) {
  let el = document.getElementById("smart-buy-banner");
  if (!el) {
    el = document.createElement("div");
    el.id = "smart-buy-banner";
    el.style.cssText = [
      "display:none", "margin:8px 0", "padding:10px 14px",
      "border-radius:10px", "border:1px solid rgba(167,139,250,0.4)",
      "background:rgba(167,139,250,0.08)", "font-size:12px", "color:#a78bfa",
    ].join(";");
    const openTradesSection = document.querySelector(".section-trades") ||
                              document.getElementById("open-trades") ||
                              document.querySelector(".trades-section");
    if (openTradesSection) openTradesSection.before(el);
    else (document.querySelector("main") || document.body).appendChild(el);
  }
  if (!pb) { el.style.display = "none"; return; }
  const gradeInfo = { A: ["🏆", "#ffd700", "Элитный"], B: ["⭐", "#a78bfa", "Стандарт"], C: ["🔸", "#f59e0b", "Слабый"] };
  const [gIcon, gCol, gLabel] = gradeInfo[pb.entry_quality] || gradeInfo.B;
  const savings = pb.signal_price > 0
    ? ((pb.signal_price - pb.target) / pb.signal_price * 100).toFixed(2)
    : "0.00";
  el.style.display = "";
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
      <span>🎯 <b>Smart BUY</b>
        <span style="color:${gCol};font-weight:700;margin-left:6px">${gIcon} ${gLabel} ${pb.entry_quality || "B"}</span>
        · откат до <b style="color:#e2e8f0">$${Number(pb.target).toFixed(8)}</b>
        <span style="color:#00d18f;font-size:10px;margin-left:4px">(-${pb.pullback_pct || 0.8}% · экономия ${savings}%)</span>
      </span>
      <span style="color:#8892b0">Сигнал: $${Number(pb.signal_price).toFixed(8)} | AI ${pb.ai_conf}% | осталось ${pb.ticks_left} тика</span>
    </div>
  `;
}

// ── Качество точки входа: A/B/C-грейд + факторы confluence ────────────────
function renderEntryQuality(eq, signal) {
  let el = document.getElementById("entry-quality-bar");
  if (!el) {
    el = document.createElement("div");
    el.id = "entry-quality-bar";
    // Вставляем сразу после ai-signal-block (в hero-карточке)
    const ref = document.getElementById("ai-signal-block");
    if (ref && ref.parentElement) ref.parentElement.after(el);
    else {
      const hero = document.querySelector(".grinch-hero") || document.querySelector(".col-left");
      if (hero) hero.insertAdjacentElement("afterbegin", el);
    }
  }
  if (!eq || !eq.quality) { el.style.display = "none"; return; }

  const quality  = eq.quality;
  const score    = eq.score || 0;
  const reasons  = eq.reasons || [];
  const volRatio = Number(eq.vol_ratio || 1).toFixed(1);
  const stochRsi = Math.round((eq.stoch_rsi || 0.5) * 100);

  // Цвета и иконки грейда
  const cfg = {
    A: { col: "#ffd700", bg: "rgba(255,215,0,0.08)",  brd: "rgba(255,215,0,0.3)",  icon: "🏆", lbl: "ЭЛИТНЫЙ ВХОД" },
    B: { col: "#a78bfa", bg: "rgba(167,139,250,0.06)", brd: "rgba(167,139,250,0.3)", icon: "⭐", lbl: "СТАНДАРТ" },
    C: { col: "#f59e0b", bg: "rgba(245,158,11,0.05)",  brd: "rgba(245,158,11,0.2)",  icon: "🔸", lbl: "СЛАБЫЙ" },
  }[quality] || { col: "#6b82a8", bg: "transparent", brd: "rgba(107,130,168,0.2)", icon: "○", lbl: "—" };

  // Только показываем при BUY-сигнале (в HOLD/SELL — скрываем)
  if (signal !== "BUY") { el.style.display = "none"; return; }

  el.style.cssText = `
    margin:6px 0 2px;padding:8px 12px;border-radius:9px;
    border:1px solid ${cfg.brd};background:${cfg.bg};
    font-size:11px;display:block;
  `;

  const reasonsHtml = reasons.length
    ? `<div style="margin-top:5px;display:flex;flex-wrap:wrap;gap:4px">
        ${reasons.map(r => `<span style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);
          border-radius:5px;padding:2px 7px;color:#c8d6e8;font-size:10px">${r}</span>`).join("")}
       </div>`
    : "";

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span style="color:${cfg.col};font-weight:800;font-size:13px">${cfg.icon} Вход ${quality} · ${cfg.lbl}</span>
      <span style="color:#6b82a8;font-size:10px">score ${score}пт</span>
      <span style="margin-left:auto;color:#8892b0;font-size:10px">vol ${volRatio}x · Stoch ${stochRsi}%</span>
    </div>
    ${reasonsHtml}
  `;
}

// ── Полоса умных денег (появляется под Smart BUY баннером) ─────────────────
function renderSmartMoneyBar(sm) {
  let el = document.getElementById("sm-global-bar");
  if (!el) {
    el = document.createElement("div");
    el.id = "sm-global-bar";
    el.style.cssText = "display:none;margin:4px 0;padding:8px 12px;border-radius:8px;font-size:11px;border:1px solid transparent;";
    const ref = document.getElementById("smart-buy-banner");
    if (ref) ref.after(el); else (document.querySelector("main")||document.body).appendChild(el);
  }
  if (!sm || sm.basis === "idle") { el.style.display = "none"; return; }
  const score = Number(sm.score || 0);
  const isPos = score >= 0;
  const col   = isPos ? "#00ff88" : "#ff4d6d";
  const bg    = isPos ? "rgba(0,255,136,0.06)" : "rgba(255,77,109,0.06)";
  const arrow = isPos ? "▲" : "▼";
  el.style.display = "";
  el.style.borderColor = col + "55";
  el.style.background  = bg;
  el.innerHTML = `
    <span style="color:${col};font-weight:700">🐋 ${arrow} ${sm.label || "умные деньги"}</span>
    <span style="color:#8892b0;margin-left:8px">score ${score > 0 ? "+" : ""}${score.toFixed(2)}</span>
    ${sm.buys_1h  != null ? `<span style="color:#00ff88;margin-left:8px">↑ ${sm.buys_1h.toFixed(1)} TON/ч</span>` : ""}
    ${sm.sells_1h != null ? `<span style="color:#ff4d6d;margin-left:6px">↓ ${sm.sells_1h.toFixed(1)} TON/ч</span>` : ""}
    ${sm.early_buy ? `<span style="color:#ffd166;margin-left:8px">⚡ ранний вход</span>` : ""}
  `;
}

// ── Панель AI-управления (просадка, пауза, адаптированный порог) ───────────
function renderAIManagement(mgmt) {
  let el = document.getElementById("ai-mgmt-bar");
  if (!el) {
    el = document.createElement("div");
    el.id = "ai-mgmt-bar";
    el.style.cssText = "display:none;margin:4px 0;padding:8px 12px;border-radius:8px;font-size:11px;border:1px solid rgba(167,139,250,0.3);background:rgba(167,139,250,0.05);display:flex;flex-wrap:wrap;gap:12px;";
    const ref = document.getElementById("sm-global-bar");
    if (ref) ref.after(el); else (document.querySelector("main")||document.body).appendChild(el);
  }
  if (!mgmt || mgmt.trades_count == null) { el.style.display = "none"; return; }
  const ctrl     = mgmt.control || {};
  const paused   = ctrl.paused;
  const dd       = Number(ctrl.drawdown_pct || 0);
  const minConf  = Number(ctrl.min_conf || 0);
  const tradeAmt = Number(ctrl.trade_amount || 0);
  const streak   = Number(ctrl.loss_streak || 0);
  el.style.display = "";
  el.innerHTML = `
    <span style="color:#a78bfa;font-weight:700">🤖 AI-управление</span>
    <span title="Текущий адаптированный порог уверенности" style="color:#e2e8f0">⚡ порог ${minConf.toFixed(0)}%</span>
    <span title="Текущая адаптированная ставка" style="color:#e2e8f0">💰 ставка ${tradeAmt.toFixed(2)} TON</span>
    <span title="Просадка от пика" style="${dd > 10 ? "color:#ff4d6d" : "color:#00ff88"}">📉 DD ${dd.toFixed(1)}%</span>
    ${streak > 0 ? `<span style="color:#ffd166">⚠️ убытков подряд: ${streak}</span>` : ""}
    ${paused ? `<span style="color:#ff4d6d;font-weight:700">⏸️ ПАУЗА (защита)</span>` : `<span style="color:#00ff88">▶️ активен</span>`}
    <span style="color:#8892b0">W/L ${mgmt.wins}/${mgmt.losses} (${mgmt.win_rate}%)</span>
  `;
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

function _updateKellyPanel(kelly) {
  if (!kelly) return;
  const frac   = Number(kelly.fraction) || 0.5;
  const wr     = Number(kelly.win_rate) || 0;
  const rr     = Number(kelly.rr_ratio) || 1;
  const ev     = Number(kelly.ev) || 0;
  const trades = Number(kelly.trades) || 0;

  // Donut arc: circumference = 2π×30 = 188.5
  const arc = document.getElementById("kelly-arc");
  if (arc) {
    const fill = Math.min(frac / 2.0, 1.0) * 188.5;
    const goodKelly = wr >= 55 && trades >= 5;
    arc.setAttribute("stroke-dasharray", fill.toFixed(1) + " 188.5");
    arc.setAttribute("stroke", goodKelly ? "#00ff88" : wr >= 50 ? "#ffd166" : "#ff4d6d");
  }
  const fracText = document.getElementById("kelly-frac-text");
  if (fracText) fracText.textContent = (frac * 100).toFixed(0) + "%";

  const wrEl = document.getElementById("kelly-winrate");
  if (wrEl) {
    wrEl.textContent = wr.toFixed(1) + "%";
    wrEl.style.color = wr >= 60 ? "#00ff88" : wr >= 50 ? "#ffd166" : "#ff4d6d";
  }
  const rrEl = document.getElementById("kelly-rr");
  if (rrEl) {
    rrEl.textContent = rr.toFixed(2);
    rrEl.style.color = rr >= 2 ? "#00ff88" : rr >= 1 ? "#ffd166" : "#ff4d6d";
  }
  const evEl = document.getElementById("kelly-ev");
  if (evEl) {
    evEl.textContent = (ev >= 0 ? "+" : "") + ev.toFixed(3) + " TON";
    evEl.style.color = ev >= 0 ? "#00ff88" : "#ff4d6d";
  }
  const trEl = document.getElementById("kelly-trades");
  if (trEl) trEl.textContent = trades;

  const descEl = document.getElementById("kelly-desc");
  if (descEl) {
    if (trades < 5) {
      descEl.textContent = `Накапливаем статистику: ${trades}/5 сделок…`;
      descEl.style.color = "#8892b0";
    } else if (wr >= 60 && rr >= 1.5) {
      descEl.textContent = `🔥 Отличная статистика! Kelly рекомендует ${(frac*100).toFixed(0)}% ставку`;
      descEl.style.color = "#00ff88";
    } else if (wr >= 50) {
      descEl.textContent = `✅ Позитивное мат.ожидание — Kelly: ${(frac*100).toFixed(0)}% от капитала`;
      descEl.style.color = "#ffd166";
    } else {
      descEl.textContent = `⚠️ Win rate ${wr.toFixed(0)}% — AI снижает ставку до ${(frac*100).toFixed(0)}%`;
      descEl.style.color = "#ff4d6d";
    }
  }
}

function _updateQuantumModels(modelInfo) {
  const grid = document.getElementById("qb-models-grid");
  if (!grid || !modelInfo) return;
  const MODEL_ICONS = {RF:"🌲",ET:"⚡",GB:"🚀",HGB:"💥",XGB:"🔥",MLP:"🧠"};
  const MODEL_DESC  = {RF:"Random Forest",ET:"Extra Trees",GB:"Gradient Boost",HGB:"Hist GB",XGB:"XGBoost",MLP:"Neural Net"};
  grid.innerHTML = modelInfo.map(m => {
    const pct = Math.round(m.accuracy || 0);
    const col = pct >= 65 ? "#00ff88" : pct >= 50 ? "#ffd166" : "#ff4d6d";
    const wPct = Math.min(m.weight / 2.0 * 100, 100).toFixed(0);
    return `<div class="qb-model-card">
      <div class="qb-model-icon">${MODEL_ICONS[m.name]||"🤖"}</div>
      <div class="qb-model-body">
        <div class="qb-model-name">${m.name} <span class="qb-model-desc">${MODEL_DESC[m.name]||""}</span></div>
        <div class="qb-model-bar-wrap">
          <div class="qb-model-bar" style="width:${pct}%;background:${col}"></div>
        </div>
        <div class="qb-model-stats">
          <span style="color:${col}">${pct}% acc</span>
          <span style="color:#8892b0">wt: ${m.weight.toFixed(2)}</span>
        </div>
      </div>
    </div>`;
  }).join("");
}

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

  // 11. Kelly Criterion panel
  if (ai.kelly) _updateKellyPanel(ai.kelly);

  // 12. QuantumBrain 6-model grid
  if (ai.model_info && ai.model_info.length) _updateQuantumModels(ai.model_info);
}

// ═══════════════════════════════════════════════════════════════════
//  💎 PORTFOLIO TOTAL + ROI TRACKER
// ═══════════════════════════════════════════════════════════════════
let _portfolioBaseline = null;

function _updatePortfolioTracker(bal, analysis, stats) {
  const tonAmt  = Number(bal?.TON)    || 0;
  const grnAmt  = Number(bal?.GRINCH) || 0;
  const grnUsd  = Number(analysis?.price) || 0;
  const tonUsd  = window._tonPriceUsd || 0;

  if (tonUsd <= 0 || grnUsd <= 0) return;

  const totalUsd = tonAmt * tonUsd + grnAmt * grnUsd;

  // Baseline = first non-zero snapshot (used for day-change %)
  if (!_portfolioBaseline && totalUsd > 0) {
    _portfolioBaseline = totalUsd;
  }

  const ptVal = document.getElementById("pt-total");
  if (ptVal) ptVal.textContent = "$" + totalUsd.toFixed(2);

  const ptChange = document.getElementById("pt-change");
  if (ptChange && _portfolioBaseline && _portfolioBaseline > 0) {
    const chg = ((totalUsd - _portfolioBaseline) / _portfolioBaseline) * 100;
    const sign = chg >= 0 ? "▲ +" : "▼ ";
    ptChange.textContent = sign + Math.abs(chg).toFixed(2) + "% сессия";
    ptChange.className = "pt-change " + (chg >= 0 ? "pos" : "neg");
  }

  // ROI ring toward +20% target (uses P&L / baseline to compute progress)
  const pnlTon = Number(stats?.total_pnl) || 0;
  // compute ROI% from P&L vs current portfolio in TON
  const portTon = tonAmt + grnAmt * (grnUsd / tonUsd);
  let roiPct = 0;
  if (portTon > 0 && pnlTon !== 0) {
    roiPct = (pnlTon / (portTon - pnlTon)) * 100;
  }
  // Also check if there are open trades to display progress toward 20%
  const goalPct = 20;
  const progress = Math.min(Math.max(roiPct, 0), goalPct);
  const progressRatio = progress / goalPct; // 0–1

  // Update ring (circumference = 2π*28 ≈ 175.9)
  const arc = document.getElementById("roi-ring-arc");
  if (arc) {
    const fill = (progressRatio * 175.9).toFixed(1);
    arc.setAttribute("stroke-dasharray", fill + " 175.9");
    const ringColor = roiPct >= goalPct ? "#00ff88" : roiPct >= goalPct * 0.5 ? "#ffd166" : "#0098ea";
    arc.setAttribute("stroke", ringColor);
  }
  const ringPct = document.getElementById("roi-ring-pct");
  if (ringPct) {
    ringPct.textContent = roiPct.toFixed(1) + "%";
    const rc = roiPct >= goalPct ? "#00ff88" : roiPct >= 5 ? "#ffd166" : "#e8eef8";
    ringPct.setAttribute("fill", rc);
  }

  // Goal progress bar
  const barEl = document.getElementById("roi-goal-bar");
  if (barEl) barEl.style.width = (progressRatio * 100).toFixed(1) + "%";

  const goalPctEl = document.getElementById("roi-goal-pct");
  if (goalPctEl) {
    goalPctEl.textContent = roiPct.toFixed(2) + "%";
    goalPctEl.style.color = roiPct >= goalPct ? "#00ff88" : roiPct >= 5 ? "#ffd166" : "#8892b0";
  }

  const subEl = document.getElementById("roi-goal-sub");
  if (subEl) {
    if (pnlTon === 0) {
      subEl.textContent = "Накапливаем историю прибыли…";
    } else if (roiPct >= goalPct) {
      subEl.textContent = `🎯 Цель достигнута! +${roiPct.toFixed(2)}% доходность`;
    } else {
      const need = goalPct - roiPct;
      subEl.textContent = `До цели +20%: ещё +${need.toFixed(2)}% · P&L ${pnlTon >= 0 ? "+" : ""}${pnlTon.toFixed(4)} TON`;
    }
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
    const smartTp = !!t.smart_tp_active;
    let waitLabel, waitColor;
    if (smartTp && inProfit)      { waitLabel = `🧠 ИИ держит — ищем больше прибыли`; waitColor = "#a78bfa"; }
    else if (inProfit)            { waitLabel = "✅ Уже в плюсе — можно закрыть"; waitColor = "#00ff88"; }
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
        <span style="color:#00d4aa">TP $${tp}${smartTp ? ' <span style="background:#a78bfa22;color:#a78bfa;border:1px solid #a78bfa55;border-radius:4px;padding:0 4px;font-size:9px;margin-left:3px">🧠 Smart</span>' : ''}</span>
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
      <div style="margin:6px 0;padding:8px 10px;border-radius:8px;background:${inProfit ? 'rgba(0,255,136,0.08)' : 'rgba(255,77,109,0.08)'};border:1px solid ${inProfit ? 'rgba(0,255,136,0.25)' : 'rgba(255,77,109,0.25)'}">
        <div class="trade-row" style="font-size:11px;color:#8892b0;margin-bottom:3px">
          <span>Если продать сейчас (−газ покупки −газ продажи −1% DEX):</span>
        </div>
        <div class="trade-row" style="align-items:center">
          <b style="font-size:18px;font-weight:900;color:${inProfit ? '#00ff88' : '#ff4d6d'};letter-spacing:0.5px">${netTon !== null ? (netTon >= 0 ? '+' : '−') + fmtGram(Math.abs(netTon)) : '—'}</b>
          <span style="font-size:13px;font-weight:700;color:${inProfit ? '#00ff88' : '#ff4d6d'}">${pnlSign}${netPct.toFixed(2)}%</span>
        </div>
      </div>
      ${be > 0 ? `<div class="trade-row" style="font-size:10px;color:#4a5568">
        <span>Безубыток (газ обоих свопов + 2% DEX): <b style="color:#ffd166">$${be}</b></span>
        ${t.min_gross_pct ? `<span style="color:#718096">нужен рост <b style="color:#ffd166">+${t.min_gross_pct}%</b></span>` : ""}
      </div>` : ""}
      <div style="display:flex;gap:6px;margin-top:8px">
        <button onclick='closeTrade(this, ${JSON.stringify(String(t.id))})'
          style="flex:1;padding:9px;border:none;border-radius:8px;cursor:pointer;font-weight:800;font-size:12px;color:#fff;background:${inProfit ? "linear-gradient(90deg,#00b894,#00ff88)" : "linear-gradient(90deg,#ff4d6d,#ff7a3d)"}">
          ${inProfit ? "✅ Продать с прибылью" : "✖ Продать сейчас"}
        </button>
        <button onclick='deleteTrade(this, ${JSON.stringify(String(t.id))})'
          title="Удалить позицию без продажи"
          style="padding:9px 12px;border:1px solid rgba(255,255,255,0.15);border-radius:8px;cursor:pointer;font-size:14px;color:#8892b0;background:rgba(255,255,255,0.05);flex-shrink:0"
          onmouseover="this.style.background='rgba(255,77,109,0.15)';this.style.color='#ff4d6d';this.style.borderColor='rgba(255,77,109,0.4)'"
          onmouseout="this.style.background='rgba(255,255,255,0.05)';this.style.color='#8892b0';this.style.borderColor='rgba(255,255,255,0.15)'">
          🗑
        </button>
      </div>
    </div>`;
  }).join("");
}

async function deleteTrade(btn, id) {
  if (!confirm("Удалить позицию из списка БЕЗ продажи на DeDust?\n\nGRINCH останется на кошельке — позиция просто исчезнет из трекера.")) return;
  if (btn) { btn.disabled = true; btn.textContent = "⏳"; }
  try {
    const r = await fetch("/api/trade/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id })
    });
    const d = await r.json().catch(() => ({ ok: false, error: "ошибка ответа" }));
    if (!d.ok) {
      showToast("❌ Не удалось удалить: " + (d.error || "ошибка"), "err");
      if (btn) { btn.disabled = false; btn.textContent = "🗑"; }
    } else {
      showToast("🗑 Позиция удалена из трекера", "ok");
    }
  } catch (e) {
    showToast("❌ Ошибка сети при удалении позиции", "err");
    if (btn) { btn.disabled = false; btn.textContent = "🗑"; }
  }
  fetch("/api/status").then(r => r.json()).then(updateUI).catch(() => {});
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
      showToast("❌ Не удалось закрыть: " + (d.error || "ошибка"), "err");
      if (btn) { btn.disabled = false; btn.textContent = "✖ Продать сейчас"; }
    }
  } catch (e) {
    showToast("❌ Ошибка сети при закрытии позиции", "err");
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

// ── Mobile Brain toggle ──────────────────────────────────────────
function toggleMobileBrain() {
  const col = document.querySelector(".ai-brain-col");
  const btn = document.getElementById("mobile-brain-toggle");
  if (!col) return;
  const isOpen = col.classList.toggle("mobile-open");
  if (btn) btn.classList.toggle("active", isOpen);
  if (btn) btn.textContent = isOpen ? "✕ ЗАКРЫТЬ" : "🧠 AI";
}

// Показывать кнопку тогла только на мобильных (<768px)
function _initMobileUI() {
  const btn = document.getElementById("mobile-brain-toggle");
  if (!btn) return;
  const mq = window.matchMedia("(max-width: 768px)");
  const update = (e) => { btn.style.display = e.matches ? "flex" : "none"; };
  mq.addEventListener("change", update);
  update(mq);
}
document.addEventListener("DOMContentLoaded", _initMobileUI);
async function switchPair(symbol) {
  const r = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol }),
  });
  const d = await r.json();
  if (!d.ok) { showToast("❌ " + (d.message || "Ошибка смены пары"), "err"); loadConfig(); return; }
  _lastLivePrice = null;
  document.getElementById("symbol-label").textContent = symbol;
  document.getElementById("price").textContent = "…";
  document.getElementById("price-change").textContent = "—";
}

// ── DCA toggle visibility ────────────────────────────────────────────────────
function onDcaModeChange() {
  const checked = document.getElementById("cfg-dca-mode")?.checked;
  const params  = document.getElementById("dca-params");
  const panel   = document.getElementById("dca-status-panel");
  if (params) params.style.display  = checked ? "" : "none";
  if (panel)  panel.style.display   = checked ? "" : "none";
}
// Wire up toggle after DOM ready
document.addEventListener("DOMContentLoaded", () => {
  const cb = document.getElementById("cfg-dca-mode");
  if (cb) cb.addEventListener("change", onDcaModeChange);
});

async function saveDcaConfig() {
  const g   = id => document.getElementById(id);
  const btn = document.getElementById("btn-save-dca");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Сохраняю…"; }
  const cfg = {
    dca_mode:             g("cfg-dca-mode")?.checked ?? false,
    dca_stake_ton:        parseFloat(g("cfg-dca-stake")?.value  || 100),
    dca_target_profit_pct: parseFloat(g("cfg-dca-target")?.value || 20),
    dca_drop_trigger_pct: parseFloat(g("cfg-dca-drop")?.value   || 25),
    dca_pullback_wait_pct: parseFloat(g("cfg-dca-pullback")?.value || 25),
    dca_max_entries:      parseInt(g("cfg-dca-max-entries")?.value || 10, 10),
  };
  try {
    const r = await fetch("/api/config", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify(cfg)
    });
    const d = await r.json();
    if (d.ok) {
      showToast("✅ DCA настройки сохранены", "ok");
      if (btn) { btn.textContent = "✅ Сохранено"; setTimeout(() => { btn.disabled = false; btn.textContent = "💾 Сохранить DCA настройки"; }, 2000); }
    } else {
      showToast("❌ " + (d.message || "Ошибка"), "err");
      if (btn) { btn.disabled = false; btn.textContent = "💾 Сохранить DCA настройки"; }
    }
  } catch (e) {
    showToast("❌ Ошибка сети: " + e.message, "err");
    if (btn) { btn.disabled = false; btn.textContent = "💾 Сохранить DCA настройки"; }
  }
}

// ─── AI Советник ────────────────────────────────────────────────────────────
// Опрос статуса, запуск анализа и переключение автономии реализованы в
// templates/index.html (advLoadStatus/advRun/advToggleAuto) — единственный
// источник правды, чтобы не дублировать поллинг и рендер.

async function advSaveKey() {
  const inp = document.getElementById("adv-apikey-inp");
  const key = inp ? inp.value.trim() : "";
  if (!key) { showToast("❌ Введите ключ", "err"); return; }
  const r = await fetch("/api/advisor/apikey", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({key})
  });
  const d = await r.json();
  if (d.ok) {
    showToast("✅ Groq ключ сохранён — AI советник активирован", "ok");
    if (inp) inp.value = "";
    if (typeof advLoadKey === "function") advLoadKey();
    if (typeof advLoadStatus === "function") advLoadStatus();
  } else {
    showToast("❌ " + (d.error || "Ошибка"), "err");
  }
}


async function saveProfitProtectConfig() {
  const g = id => document.getElementById(id);
  const cfg = {
    profit_protect_enabled:  g("cfg-pp-enabled")?.checked ?? true,
    profit_protect_ton:      parseFloat(g("cfg-pp-ton")?.value  || 2.0),
    profit_protect_drop_pct: parseFloat(g("cfg-pp-drop")?.value || 1.5),
    profit_protect_ai_sell:  g("cfg-pp-ai-sell")?.checked ?? true,
  };
  try {
    const r = await fetch("/api/config", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify(cfg)
    });
    const d = await r.json();
    if (d.ok) {
      showToast("✅ Защита прибыли сохранена", "ok");
    } else {
      showToast("❌ " + (d.message || "Ошибка"), "err");
    }
  } catch (e) {
    showToast("❌ Ошибка сети: " + e.message, "err");
  }
}

async function saveLargeSellConfig() {
  const g = id => document.getElementById(id);
  const cfg = {
    large_sell_dca_enabled:  g("cfg-lsd-enabled")?.checked ?? true,
    large_sell_dca_ton:      parseFloat(g("cfg-lsd-buy-ton")?.value  || 100),
    large_sell_min_ton:      parseFloat(g("cfg-lsd-min-ton")?.value  || 500),
    large_sell_cooldown_sec: parseInt(g("cfg-lsd-cooldown")?.value   || 300, 10),
  };
  try {
    const r = await fetch("/api/config", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify(cfg)
    });
    const d = await r.json();
    if (d.ok) {
      showToast("✅ Детектор крупных продаж сохранён", "ok");
    } else {
      showToast("❌ " + (d.message || "Ошибка"), "err");
    }
  } catch (e) {
    showToast("❌ Ошибка сети: " + e.message, "err");
  }
}

async function saveConfig() {
  const g = id => document.getElementById(id);
  const btn = document.getElementById("btn-save-cfg");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Сохраняю…"; }

  const cfg = {
    symbol:             g("cfg-symbol").value,
    trade_amount:       parseFloat(g("cfg-amount").value),
    take_profit_pct:    parseFloat(g("cfg-tp").value),
    trailing_stop_pct:  parseFloat(g("cfg-trail").value),
    fee_pct:            parseFloat(g("cfg-fee").value),
    min_ai_confidence:  parseFloat(g("cfg-minconf").value),
    max_open_trades:    parseInt(g("cfg-max").value, 10),
    use_dynamic_targets:g("cfg-dyn").checked,
    trend_filter:       g("cfg-trend").checked,
    // Smart BUY
    smart_buy_enabled:        g("cfg-smart-buy").checked,
    smart_buy_pullback_pct:   parseFloat(g("cfg-sb-pullback").value),
    smart_buy_max_wait_ticks: parseInt(g("cfg-sb-wait").value, 10),
    smart_buy_skip_conf:      parseFloat(g("cfg-sb-skip").value),
    // Smart TP
    smart_tp_enabled:         g("cfg-smart-tp").checked,
    smart_tp_min_conf:        parseFloat(g("cfg-stp-conf").value),
    smart_tp_tight_trail_pct: parseFloat(g("cfg-stp-trail").value),
    // Авто-TP от ИИ
    min_profit_ton:       parseFloat(g("cfg-min-profit-ton")?.value || 5),
    ai_tp_adapt_min_trades: parseInt(g("cfg-ai-tp-min-trades")?.value || 5, 10),
    ai_tp_cap_pct:        parseFloat(g("cfg-ai-tp-cap")?.value || 80),
  };

  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg)
    });
    const d = await r.json();
    if (d.ok) {
      showToast("✅ " + (d.message || "Настройки сохранены"), "ok");
      if (btn) { btn.textContent = "✅ Сохранено"; setTimeout(() => { btn.disabled = false; btn.textContent = "💾 Сохранить настройки"; }, 2000); }
      await loadConfig();
    } else {
      showToast("❌ " + (d.message || d.error || "Ошибка сохранения"), "err");
      if (btn) { btn.disabled = false; btn.textContent = "💾 Сохранить настройки"; }
    }
  } catch (e) {
    showToast("❌ Ошибка сети: " + e.message, "err");
    if (btn) { btn.disabled = false; btn.textContent = "💾 Сохранить настройки"; }
  }
}

async function loadConfig() {
  const r   = await fetch("/api/config");
  const cfg = await r.json();
  const g = id => document.getElementById(id);
  g("cfg-symbol").value  = cfg.symbol;
  g("cfg-amount").value  = cfg.trade_amount;
  g("cfg-tp").value      = cfg.take_profit_pct;
  g("cfg-trail").value   = cfg.trailing_stop_pct;
  g("cfg-fee").value     = cfg.fee_pct;
  g("cfg-minconf").value = cfg.min_ai_confidence;
  g("cfg-max").value     = cfg.max_open_trades;
  g("cfg-dyn").checked   = !!cfg.use_dynamic_targets;
  g("cfg-trend").checked = !!cfg.trend_filter;
  // Smart BUY
  if (g("cfg-smart-buy"))     g("cfg-smart-buy").checked     = !!cfg.smart_buy_enabled;
  if (g("cfg-sb-pullback"))   g("cfg-sb-pullback").value     = cfg.smart_buy_pullback_pct   ?? 0.8;
  if (g("cfg-sb-wait"))       g("cfg-sb-wait").value         = cfg.smart_buy_max_wait_ticks ?? 3;
  if (g("cfg-sb-skip"))       g("cfg-sb-skip").value         = cfg.smart_buy_skip_conf      ?? 90;
  // Smart TP
  if (g("cfg-smart-tp"))      g("cfg-smart-tp").checked      = !!cfg.smart_tp_enabled;
  if (g("cfg-stp-conf"))      g("cfg-stp-conf").value        = cfg.smart_tp_min_conf        ?? 75;
  if (g("cfg-stp-trail"))     g("cfg-stp-trail").value       = cfg.smart_tp_tight_trail_pct ?? 1.5;
  // Авто-TP от ИИ
  if (g("cfg-min-profit-ton"))   g("cfg-min-profit-ton").value   = cfg.min_profit_ton          ?? 5;
  if (g("cfg-ai-tp-min-trades")) g("cfg-ai-tp-min-trades").value = cfg.ai_tp_adapt_min_trades  ?? 5;
  if (g("cfg-ai-tp-cap"))        g("cfg-ai-tp-cap").value        = cfg.ai_tp_cap_pct           ?? 80;
  // Обновляем статус-панель авто-TP
  const tpr = cfg.ai_tp_report || {};
  const modeEl = g("ai-tp-mode-label");
  if (modeEl) modeEl.textContent = tpr.adapted ? "🎯 ИИ-адаптивный" : "📌 Ручной (обучается…)";
  if (g("ai-tp-current")) g("ai-tp-current").textContent = tpr.take_profit_pct ? tpr.take_profit_pct.toFixed(1) + "%" : "—";
  if (g("ai-tp-floor"))   g("ai-tp-floor").textContent   = tpr.floor_pct ? tpr.floor_pct.toFixed(1) + "%" : "—";
  if (g("ai-tp-avg-win")) g("ai-tp-avg-win").textContent = tpr.avg_win_pct ? "+" + tpr.avg_win_pct.toFixed(1) + "%" : "—";
  if (g("ai-tp-trades"))  g("ai-tp-trades").textContent  = tpr.trades_used != null ? tpr.trades_used + " / " + (cfg.ai_tp_adapt_min_trades ?? 5) + " мин." : "—";

  // DCA стратегия
  if (g("cfg-dca-mode")) {
    g("cfg-dca-mode").checked = !!cfg.dca_mode;
    onDcaModeChange();
  }
  if (g("cfg-dca-stake"))       g("cfg-dca-stake").value       = cfg.dca_stake_ton          ?? 100;
  if (g("cfg-dca-target"))      g("cfg-dca-target").value      = cfg.dca_target_profit_pct  ?? 20;
  if (g("cfg-dca-drop"))        g("cfg-dca-drop").value        = cfg.dca_drop_trigger_pct   ?? 25;
  if (g("cfg-dca-pullback"))    g("cfg-dca-pullback").value    = cfg.dca_pullback_wait_pct  ?? 25;
  if (g("cfg-dca-max-entries")) g("cfg-dca-max-entries").value = cfg.dca_max_entries        ?? 10;

  // Детектор крупных продаж
  if (g("cfg-lsd-enabled"))  g("cfg-lsd-enabled").checked   = cfg.large_sell_dca_enabled  ?? true;
  if (g("cfg-lsd-buy-ton"))  g("cfg-lsd-buy-ton").value     = cfg.large_sell_dca_ton       ?? 100;
  if (g("cfg-lsd-min-ton"))  g("cfg-lsd-min-ton").value     = cfg.large_sell_min_ton       ?? 500;
  if (g("cfg-lsd-cooldown")) g("cfg-lsd-cooldown").value    = cfg.large_sell_cooldown_sec  ?? 300;

  // Защита прибыли
  if (g("cfg-pp-enabled"))  g("cfg-pp-enabled").checked   = cfg.profit_protect_enabled  ?? true;
  if (g("cfg-pp-ton"))      g("cfg-pp-ton").value         = cfg.profit_protect_ton       ?? 2.0;
  if (g("cfg-pp-drop"))     g("cfg-pp-drop").value        = cfg.profit_protect_drop_pct  ?? 1.5;
  if (g("cfg-pp-ai-sell"))  g("cfg-pp-ai-sell").checked   = cfg.profit_protect_ai_sell   ?? true;

  g("demo-badge").style.display = cfg.demo_mode ? "" : "none";
  if (cfg.ton_wallet) {
    window._tonWallet = cfg.ton_wallet;
    g("ton-addr").textContent = cfg.ton_wallet;
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
    return `<div class="ton-dep-item">
      <span class="dep-amt">+${dep.amount} TON</span>
      <span class="dep-memo">от ${escapeHtml(dep.from_short || "")}</span>
      <span class="dep-time">${dt}</span>
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
  // Числовой ts — это epoch в секундах (а Date ждёт миллисекунды) → ×1000
  let ms;
  if (typeof ts === "number") {
    ms = ts < 1e12 ? ts * 1000 : ts;
  } else {
    ms = new Date(ts).getTime();
  }
  const sec = Math.max(0, (Date.now() - ms) / 1000);
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
      let addrHtml = "";
      if (t.addr) {
        const isSmart = _smartAddrs.has(t.addr);
        const short = t.addr.slice(0, 6) + "…" + t.addr.slice(-4);
        addrHtml = '<a class="dt-addr' + (isSmart ? " smart" : "") + '" target="_blank" rel="noopener" ' +
          'href="https://tonviewer.com/' + encodeURIComponent(t.addr) + '">' +
          (isSmart ? "⭐ " : "") + escapeHtml(short) + '</a>';
      }
      return '<div class="dex-trade">' +
        '<span class="dt-side ' + (buy ? "pos" : "neg") + '">' + (buy ? "Покупка" : "Продажа") + '</span>' +
        '<span class="dt-amt">' + fmtAmt(t.token_amount) + ' ' + escapeHtml(sym) + '</span>' +
        '<span class="dt-usd">' + (t.amount_usd != null ? fmtBig(t.amount_usd) : "—") + '</span>' +
        '<span class="dt-time">' + timeAgo(t.ts) + '</span>' +
        addrHtml +
        '</div>';
    }).join("");
  } catch (e) {}
}

let _walletTab = "profit";
let _walletData = null;
let _smartAddrs = new Set();
function switchWalletTab(tab) {
  _walletTab = tab;
  document.getElementById("wl-tab-profit").classList.toggle("active", tab === "profit");
  document.getElementById("wl-tab-volume").classList.toggle("active", tab === "volume");
  renderWalletList();
}
function renderWalletList() {
  const list = document.getElementById("wl-list");
  if (!list || !_walletData) return;
  const rows = (_walletTab === "profit" ? _walletData.top_profit : _walletData.top_volume) || [];
  if (!rows.length) {
    list.innerHTML = '<div class="empty-msg">Накапливаю статистику по кошелькам…</div>';
    return;
  }
  list.innerHTML = rows.map(w => {
    const pnl = Number(w.pnl_ton) || 0;
    const pnlCls = pnl > 0 ? "pos" : (pnl < 0 ? "neg" : "");
    const pnlTxt = (pnl > 0 ? "+" : "") + pnl.toFixed(2) + " TON";
    const lastBuy = w.last_kind === "buy";
    const usdVol = Number(w.usd_volume) || 0;
    const usdIn  = Number(w.usd_in)  || 0;
    const usdOut = Number(w.usd_out) || 0;
    const volTxt = _walletTab === "volume"
      ? '<span class="wl-usd">' + fmtBig(usdVol) + '</span>'
      : '<span class="wl-pnl ' + pnlCls + '">' + pnlTxt + '</span>';
    const detailTxt = _walletTab === "volume"
      ? '<span class="wl-vol-detail"><span class="pos">↑' + fmtBig(usdIn) + '</span> <span class="neg">↓' + fmtBig(usdOut) + '</span></span>'
      : '<span class="wl-vol-detail">' + fmtBig(Number(w.grinch_bought)||0, true) + ' GRINCH</span>';
    return '<div class="wl-row">' +
      '<span class="wl-addr">' + (w.smart ? "⭐ " : "") + escapeHtml(w.short) + '</span>' +
      '<span class="wl-bs"><span class="pos">' + w.buys + '↑</span>/<span class="neg">' + w.sells + '↓</span></span>' +
      '<span class="wl-side ' + (lastBuy ? "pos" : "neg") + '">' + (lastBuy ? "купил" : "продал") + '</span>' +
      volTxt +
      detailTxt +
      '<span class="wl-time">' + timeAgo(w.last_ts) + '</span>' +
      '</div>';
  }).join("");
}

function renderWalletEvents() {
  const box = document.getElementById("wl-events");
  if (!box || !_walletData) return;
  const evts = _walletData.recent_events || [];
  if (!evts.length) { box.innerHTML = ""; return; }
  box.innerHTML = evts.map(e => {
    const buy = e.kind === "buy";
    const usd = Number(e.usd) || 0;
    const grinch = Number(e.grinch) || 0;
    return '<div class="dex-trade">' +
      '<span class="dt-side ' + (buy ? "pos" : "neg") + '">' + (buy ? "Покупка" : "Продажа") + '</span>' +
      '<span class="dt-amt">' + fmtAmt(grinch) + ' GRINCH</span>' +
      '<span class="dt-usd">' + (usd > 0 ? fmtBig(usd) : "—") + '</span>' +
      '<span class="dt-time">' + timeAgo(e.ts) + '</span>' +
      '<span class="dt-addr' + (e.smart ? " smart" : "") + '">' + (e.smart ? "⭐ " : "") + escapeHtml(e.short) + '</span>' +
      '</div>';
  }).join("");
}
async function loadWallets() {
  try {
    const r = await fetch("/api/wallets");
    const d = await r.json();
    if (!d) return;
    _walletData = d;
    _smartAddrs = new Set(d.smart_addrs || []);
    const sig = d.signal || {};
    const score = Number(sig.score) || 0;
    const scoreEl = document.getElementById("sm-score");
    scoreEl.textContent = (score > 0 ? "+" : "") + score.toFixed(2) + " · " + (sig.label || "—");
    scoreEl.className = "sm-score " + (score >= 0.4 ? "pos" : (score <= -0.4 ? "neg" : ""));
    const bar = document.getElementById("sm-bar");
    bar.style.width = Math.min(100, Math.abs(score) * 100) + "%";
    bar.style.left = score >= 0 ? "50%" : (50 - Math.min(50, Math.abs(score) * 50)) + "%";
    bar.style.background = score >= 0 ? "var(--grinch, #00ff88)" : "#ff4d6d";
    const buyUsd  = Number(d.recent_buy_usd)  || 0;
    const sellUsd = Number(d.recent_sell_usd) || 0;
    const buyTon  = Number(sig.buy_ton)  || 0;
    const sellTon = Number(sig.sell_ton) || 0;
    document.getElementById("sm-buy").textContent  = buyTon.toFixed(1) + " TON" + (buyUsd  > 0 ? " · " + fmtBig(buyUsd)  : "");
    document.getElementById("sm-sell").textContent = sellTon.toFixed(1) + " TON" + (sellUsd > 0 ? " · " + fmtBig(sellUsd) : "");
    document.getElementById("wl-total").textContent = d.total_wallets || 0;
    document.getElementById("wl-smart").textContent = d.smart_wallets || 0;
    document.getElementById("wl-seen").textContent  = d.total_trades_seen || 0;
    const src = document.getElementById("wallets-src");
    if (src) src.textContent = "· за 24ч: " + (d.active_24h || 0);
    renderWalletList();
    renderWalletEvents();
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
loadWallets();
setInterval(loadWallets, 20000);

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
  if (tgtEl) tgtEl.textContent = d.target_price  ? fmt8(d.target_price) + " (+" + (+d.sell_rise_pct).toFixed(2) + "%)" : "—";

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
      msgEl.textContent = "Жду роста +" + (+d.sell_rise_pct).toFixed(2) + "% | " + pctLeft;
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

// ══════════════════════════════════════════════════════════════════
//  Мониторинг ликвидности GRINCH (LiquidityGuard)
// ══════════════════════════════════════════════════════════════════
function fmtUsd(v) {
  if (v == null) return "—";
  return "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function updateLiquidityGuard(d) {
  const curEl    = document.getElementById("lg-current");
  const peakEl   = document.getElementById("lg-peak");
  const dropEl   = document.getElementById("lg-drop");
  const statusEl = document.getElementById("lg-status");
  const barEl    = document.getElementById("lg-bar");
  const warnEl   = document.getElementById("lg-warn");
  const card     = document.getElementById("liqguard-card");

  if (curEl)  curEl.textContent  = fmtUsd(d.current_liq);
  if (peakEl) peakEl.textContent = fmtUsd(d.peak_liq);

  if (dropEl) {
    const drop = d.drop_pct || 0;
    dropEl.textContent = drop.toFixed(1) + "%";
    dropEl.style.color = drop >= (d.pause_threshold_pct || 30) ? "var(--red)" : drop >= 15 ? "#ffd166" : "var(--green)";
  }

  if (statusEl) {
    if (d.buys_paused) {
      statusEl.textContent = "⛔ ПАУЗА";
      statusEl.style.color = "var(--red)";
    } else {
      statusEl.textContent = "✅ АКТИВНЫ";
      statusEl.style.color = "var(--green)";
    }
  }

  if (barEl) {
    const drop = Math.min(100, Math.max(0, d.drop_pct || 0));
    barEl.style.width = drop.toFixed(1) + "%";
    barEl.style.background = d.buys_paused ? "var(--red)" : "";
  }

  if (warnEl) {
    if (d.buys_paused && d.pause_reason) {
      warnEl.style.display = "block";
      warnEl.textContent = "⚠️ " + d.pause_reason;
    } else {
      warnEl.style.display = "none";
    }
  }

  if (card) {
    card.style.borderColor = d.buys_paused ? "rgba(255,71,87,0.4)" : "";
  }
}

function pollLiquidityGuard() {
  fetch("/api/liquidity_guard")
    .then(r => r.json())
    .then(d => updateLiquidityGuard(d))
    .catch(() => {});
}
pollLiquidityGuard();
setInterval(pollLiquidityGuard, 15000);

// ── График истории баланса кошелька (equity curve) ───────────────────────────
(function initEquityChart() {
  const canvas  = document.getElementById("eq-chart");
  const emptyEl = document.getElementById("eq-empty");
  if (!canvas) return;

  function drawEquity(pts) {
    if (!pts || pts.length < 2) {
      canvas.style.display = "none";
      if (emptyEl) emptyEl.style.display = "";
      return;
    }
    canvas.style.display = "block";
    if (emptyEl) emptyEl.style.display = "none";

    const dpr = window.devicePixelRatio || 1;
    const W   = canvas.offsetWidth  || 320;
    const H   = canvas.offsetHeight || 90;
    canvas.width  = W * dpr;
    canvas.height = H * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const vals = pts.map(p => p.equity_ton);
    const times = pts.map(p => new Date(p.t).getTime());
    const minV  = Math.min(...vals);
    const maxV  = Math.max(...vals);
    const range = maxV - minV || 0.0001;
    const minT  = times[0];
    const maxT  = times[times.length - 1];
    const timeRange = maxT - minT || 1;

    const PAD_L = 2, PAD_R = 4, PAD_T = 8, PAD_B = 14;
    const cW = W - PAD_L - PAD_R;
    const cH = H - PAD_T - PAD_B;

    const toX = t => PAD_L + ((t - minT) / timeRange) * cW;
    const toY = v => PAD_T + (1 - (v - minV) / range) * cH;

    // фон
    ctx.clearRect(0, 0, W, H);

    // область под линией
    const grad = ctx.createLinearGradient(0, PAD_T, 0, H - PAD_B);
    const isUp = vals[vals.length - 1] >= vals[0];
    grad.addColorStop(0, isUp ? "rgba(0,209,143,0.25)" : "rgba(255,90,90,0.22)");
    grad.addColorStop(1, "rgba(0,0,0,0)");
    ctx.beginPath();
    ctx.moveTo(toX(times[0]), toY(vals[0]));
    for (let i = 1; i < pts.length; i++) ctx.lineTo(toX(times[i]), toY(vals[i]));
    ctx.lineTo(toX(times[times.length - 1]), H - PAD_B);
    ctx.lineTo(toX(times[0]), H - PAD_B);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // линия
    ctx.beginPath();
    ctx.moveTo(toX(times[0]), toY(vals[0]));
    for (let i = 1; i < pts.length; i++) ctx.lineTo(toX(times[i]), toY(vals[i]));
    ctx.strokeStyle = isUp ? "#00d18f" : "#ff5a5a";
    ctx.lineWidth   = 1.5;
    ctx.lineJoin    = "round";
    ctx.stroke();

    // метки мин/макс
    ctx.font      = "9px monospace";
    ctx.fillStyle = "#8892b0";
    ctx.textAlign = "right";
    ctx.fillText(maxV.toFixed(4) + " TON", W - PAD_R, PAD_T + 8);
    ctx.fillText(minV.toFixed(4) + " TON", W - PAD_R, H - PAD_B - 2);

    // диапазон времени
    const rangeLbl = document.getElementById("eq-range-lbl");
    if (rangeLbl && pts.length > 0) {
      const first = new Date(pts[0].t);
      const last  = new Date(pts[pts.length - 1].t);
      const fmt   = d => d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      const fmtD  = d => d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
      const sameDay = first.toDateString() === last.toDateString();
      rangeLbl.textContent = sameDay
        ? `${fmtD(first)} ${fmt(first)} – ${fmt(last)}`
        : `${fmtD(first)} – ${fmtD(last)}`;
    }
  }

  function fetchAndDraw() {
    fetch("/api/equity")
      .then(r => r.json())
      .then(d => drawEquity(d.points || []))
      .catch(() => {});
  }

  fetchAndDraw();
  setInterval(fetchAndDraw, 30000);
  window.addEventListener("resize", fetchAndDraw);
})();

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

// ═══════════════════════════════════════════════════════════════════
//  🌍 ПОЛНЫЙ ОБЗОР РЫНКА
// ═══════════════════════════════════════════════════════════════════

function _g(id) { return document.getElementById(id); }

function _setBar(barId, pct, color) {
  const el = _g(barId);
  if (!el) return;
  el.style.width = Math.min(100, Math.max(0, pct)) + "%";
  el.style.background = color || "var(--ton)";
}

function _setMarker(markerId, pct) {
  const el = _g(markerId);
  if (!el) return;
  el.style.left = Math.min(100, Math.max(0, pct)) + "%";
}

function _zone(el, cls) {
  if (!el) return;
  el.className = "mo-cell-zone " + cls;
}

function _trendColor(dir) {
  return dir === "UP" ? "#00ff88" : dir === "DOWN" ? "#ff4d6d" : "#8892b0";
}
function _trendArrow(dir) {
  return dir === "UP" ? "▲ UP" : dir === "DOWN" ? "▼ DOWN" : "→ FLAT";
}

function renderMarketOverview(analysis) {
  if (!analysis) return;
  const g = _g;

  // ── Режим рынка ────────────────────────────────────────────────
  const regime = analysis.regime || "RANGING";
  const regimeColor = analysis.regime_color || "#8892b0";
  const regimeBadge = g("mo-regime-badge");
  if (regimeBadge) {
    const icons = { UPTREND:"🚀", DOWNTREND:"📉", BREAKOUT:"💥", VOLATILE:"⚡", RANGING:"↔️" };
    regimeBadge.textContent = (icons[regime] || "") + " " + regime;
    regimeBadge.style.cssText = `color:${regimeColor};border-color:${regimeColor};background:${regimeColor}18`;
  }

  // ── Поддержка / Сопротивление ──────────────────────────────────
  const fmtLvl = v => v != null ? fmtPrice(v) : "—";
  if (g("mo-support"))    g("mo-support").textContent    = fmtLvl(analysis.support_pa);
  if (g("mo-resistance")) g("mo-resistance").textContent = fmtLvl(analysis.resistance_pa);

  // EMA50 расстояние
  const ema50d = Number(analysis.price_vs_ema50 || 0);
  if (g("mo-ema50-dist")) {
    g("mo-ema50-dist").textContent = (ema50d >= 0 ? "+" : "") + ema50d.toFixed(2) + "%";
    g("mo-ema50-dist").style.color = ema50d >= 0 ? "#00ff88" : "#ff4d6d";
  }

  // ── RSI ────────────────────────────────────────────────────────
  const rsi = Number(analysis.rsi || 50);
  const rsiZone = analysis.rsi_zone || "NEUTRAL";
  if (g("mo-rsi-val")) g("mo-rsi-val").textContent = rsi.toFixed(1);
  const rsiColors = { OVERSOLD:"#00ff88", LOW:"#5cc8ff", NEUTRAL:"#8892b0", HIGH:"#ffd166", OVERBOUGHT:"#ff4d6d" };
  const rsiLabels = { OVERSOLD:"перепродан", LOW:"низкий", NEUTRAL:"норма", HIGH:"высокий", OVERBOUGHT:"перекуплен" };
  const rsiCls    = { OVERSOLD:"zone-up", LOW:"zone-up", NEUTRAL:"zone-neutral", HIGH:"zone-warn", OVERBOUGHT:"zone-down" };
  if (g("mo-rsi-zone")) { g("mo-rsi-zone").textContent = rsiLabels[rsiZone] || rsiZone; _zone(g("mo-rsi-zone"), rsiCls[rsiZone] || "zone-neutral"); }
  _setBar("mo-rsi-bar", rsi, rsiColors[rsiZone] || "#8892b0");
  _setMarker("mo-rsi-marker", rsi);

  // ── ADX ────────────────────────────────────────────────────────
  const adx = Number(analysis.adx || 0);
  const diP  = Number(analysis.di_plus  || 0);
  const diM  = Number(analysis.di_minus || 0);
  if (g("mo-adx-val"))   g("mo-adx-val").textContent   = adx.toFixed(1);
  if (g("mo-di-plus"))   g("mo-di-plus").textContent    = diP.toFixed(1);
  if (g("mo-di-minus"))  g("mo-di-minus").textContent   = diM.toFixed(1);
  const adxLabel = adx > 40 ? "сильный тренд" : adx > 25 ? "тренд" : adx > 15 ? "слабый" : "флэт";
  const adxCls   = adx > 25 ? "zone-up" : adx > 15 ? "zone-warn" : "zone-neutral";
  const adxColor = diP > diM ? "#00ff88" : "#ff4d6d";
  if (g("mo-adx-zone")) { g("mo-adx-zone").textContent = adxLabel; _zone(g("mo-adx-zone"), adxCls); }
  _setBar("mo-adx-bar", Math.min(adx * 2, 100), adxColor);

  // ── Volume ─────────────────────────────────────────────────────
  const volR = Number(analysis.vol_ratio || 1);
  if (g("mo-vol-val")) g("mo-vol-val").textContent = volR.toFixed(2) + "x";
  const volLabel = volR >= 2 ? "кит 🔥" : volR >= 1.5 ? "высокий" : volR >= 1.1 ? "норма+" : volR < 0.7 ? "низкий" : "норма";
  const volCls   = volR >= 1.5 ? "zone-up" : volR < 0.7 ? "zone-down" : "zone-neutral";
  const volColor = volR >= 1.5 ? "#00ff88" : volR < 0.7 ? "#ff4d6d" : "#ffd166";
  if (g("mo-vol-zone")) { g("mo-vol-zone").textContent = volLabel; _zone(g("mo-vol-zone"), volCls); }
  _setBar("mo-vol-bar", Math.min(volR * 40, 100), volColor);

  // ── MACD ───────────────────────────────────────────────────────
  const macdDir = analysis.macd_dir || "FLAT";
  const macdH   = Number(analysis.macd_hist || 0);
  if (g("mo-macd-val")) { g("mo-macd-val").textContent = macdH > 0 ? "▲+" : "▼"; g("mo-macd-val").style.color = macdH >= 0 ? "#00ff88" : "#ff4d6d"; }
  const macdCls = macdDir === "UP" ? "zone-up" : macdDir === "DOWN" ? "zone-down" : "zone-neutral";
  if (g("mo-macd-zone")) { g("mo-macd-zone").textContent = macdDir === "UP" ? "рост" : macdDir === "DOWN" ? "падение" : "флэт"; _zone(g("mo-macd-zone"), macdCls); }
  _setBar("mo-macd-bar", macdDir === "UP" ? 75 : macdDir === "DOWN" ? 25 : 50, macdDir === "UP" ? "#00ff88" : "#ff4d6d");

  // ── Stoch RSI ──────────────────────────────────────────────────
  const stoch = Number(analysis.stoch_rsi || 0.5);
  if (g("mo-stoch-val")) g("mo-stoch-val").textContent = (stoch * 100).toFixed(0) + "%";
  const stochLabel = stoch < 0.2 ? "перепродан" : stoch > 0.8 ? "перекуплен" : "норма";
  const stochCls   = stoch < 0.2 ? "zone-up" : stoch > 0.8 ? "zone-down" : "zone-neutral";
  if (g("mo-stoch-zone")) { g("mo-stoch-zone").textContent = stochLabel; _zone(g("mo-stoch-zone"), stochCls); }
  _setBar("mo-stoch-bar", stoch * 100, stoch < 0.2 ? "#00ff88" : stoch > 0.8 ? "#ff4d6d" : "#5cc8ff");

  // ── ATR% ───────────────────────────────────────────────────────
  const atr = Number(analysis.atr_pct || 0);
  if (g("mo-atr-val")) g("mo-atr-val").textContent = atr.toFixed(2) + "%";
  const atrLabel = atr > 5 ? "высок. волат." : atr > 2 ? "средн." : "низкая";
  const atrCls   = atr > 5 ? "zone-warn" : atr > 2 ? "zone-neutral" : "zone-neutral";
  if (g("mo-atr-zone")) { g("mo-atr-zone").textContent = atrLabel; _zone(g("mo-atr-zone"), atrCls); }
  _setBar("mo-atr-bar", Math.min(atr * 10, 100), atr > 3 ? "#ffd166" : "#5cc8ff");

  // ── BB позиция ─────────────────────────────────────────────────
  const bbPct = Number(analysis.bb_pct || 50);
  if (g("mo-bb-val")) g("mo-bb-val").textContent = bbPct.toFixed(0) + "%";
  const bbLabel = bbPct < 20 ? "у нижней" : bbPct > 80 ? "у верхней" : "середина";
  const bbCls   = bbPct < 20 ? "zone-up" : bbPct > 80 ? "zone-down" : "zone-neutral";
  if (g("mo-bb-zone")) { g("mo-bb-zone").textContent = bbLabel; _zone(g("mo-bb-zone"), bbCls); }
  _setMarker("mo-bb-marker", bbPct);

  // ── EMA выравнивание ───────────────────────────────────────────
  const emaAlign = Number(analysis.ema_alignment || 0);
  const emaLabels = ["нет тренда", "EMA 9>21", "9>21>50", "цена>9>21>50"];
  const emaCls    = emaAlign >= 3 ? "zone-up" : emaAlign >= 2 ? "zone-warn" : "zone-neutral";
  if (g("mo-ema-val")) g("mo-ema-val").textContent = emaAlign + "/3";
  if (g("mo-ema-zone")) { g("mo-ema-zone").textContent = emaLabels[emaAlign] || "—"; _zone(g("mo-ema-zone"), emaCls); }
  const emaDotsEl = g("mo-ema-dots");
  if (emaDotsEl) {
    const dotColors = ["#ff4d6d", "#ffd166", "#5cc8ff", "#00ff88"];
    emaDotsEl.innerHTML = ["EMA9", "EMA21", "EMA50", "Цена"].map((label, i) => {
      const active = i < emaAlign || (emaAlign === 3 && i === 3);
      const color = active ? dotColors[emaAlign] : "rgba(255,255,255,.1)";
      return `<div class="mo-ema-dot" style="background:${color}" title="${label}"></div>`;
    }).join("") + `<span style="font-size:10px;color:var(--text2);margin-left:4px">${emaLabels[emaAlign]||""}</span>`;
  }

  // ── OBV / Vol тренд / BB сжатие ────────────────────────────────
  const obvDir   = analysis.obv_dir   || "FLAT";
  const volTrd   = analysis.vol_trend || "FLAT";
  const bbwPct   = Number(analysis.bb_width_pct || 100);

  if (g("mo-obv")) { g("mo-obv").textContent = _trendArrow(obvDir); g("mo-obv").style.color = _trendColor(obvDir); }
  if (g("mo-voltrd")) { g("mo-voltrd").textContent = _trendArrow(volTrd); g("mo-voltrd").style.color = _trendColor(volTrd); }
  if (g("mo-bbw")) {
    const squeezed = bbwPct < 80;
    g("mo-bbw").textContent = squeezed ? "🔴 сжато " + bbwPct.toFixed(0) + "%" : "🟢 норма";
    g("mo-bbw").style.color = squeezed ? "#ffd166" : "#8892b0";
  }

  // ── 10 факторов входа ──────────────────────────────────────────
  const eq       = analysis.entry_quality || "C";
  const score    = analysis.entry_score   || 0;
  const factors  = analysis.factors_detail || [];
  const gradeColors = { A: "#00ff88", B: "#ffd166", C: "#ff4d6d" };
  const gradeEl = g("mo-eq-grade");
  if (gradeEl) {
    const gc = gradeColors[eq] || "#8892b0";
    gradeEl.innerHTML = `<span style="color:${gc};font-weight:800;font-size:13px">${eq}</span>`;
  }
  if (g("mo-eq-score")) g("mo-eq-score").textContent = score + " очков";

  const factorNames = [
    "Объём", "BB сжатие→разрыв", "Дивергенция RSI",
    "Моментум свечи", "Stoch RSI разворот", "Отскок от поддержки",
    "OBV подтверждение", "EMA выравнивание", "MACD ускорение", "RSI перепродан"
  ];
  const listEl = g("mo-factors-list");
  if (listEl && factors.length) {
    listEl.innerHTML = factors.map((f, i) => {
      const active = f.pts > 0;
      const name   = f.reason || factorNames[i] || "Фактор " + (i+1);
      const pts    = f.pts;
      return `<div class="mo-factor ${active ? "active" : "inactive"}">
        <span>${active ? "✅" : "⬜"}</span>
        <span style="flex:1">${name}</span>
        ${pts > 0 ? `<span class="mo-factor-pts">+${pts}</span>` : ""}
      </div>`;
    }).join("");
  }
}


// ═══════════════════════════════════════════════════════════════════
//  🤖 AI ТОРГОВАЯ АНАЛИТИКА
// ═══════════════════════════════════════════════════════════════════

function renderAIAnalytics(a) {
  if (!a) return;

  // ── Opportunity Score ─────────────────────────────────────────
  const score = Number(a.opportunity_score || 0);

  // SVG gauge: r=62, circumference=~389.6, 270°=~292.2
  const circum  = 2 * Math.PI * 62;          // 389.56
  const arcFull = circum * 270 / 360;        // 292.17
  const arcVal  = arcFull * (score / 100);
  const gaugeArc = document.getElementById("ai-gauge-arc");
  if (gaugeArc) {
    gaugeArc.setAttribute("stroke-dasharray", `${arcVal.toFixed(1)} ${circum.toFixed(1)}`);
  }
  const gaugeNum = document.getElementById("ai-gauge-num");
  if (gaugeNum) gaugeNum.textContent = score;

  // Уровень словами
  let word, wordCls;
  if      (score >= 80) { word = "🔥 Отличный вход";    wordCls = "score-elite";  }
  else if (score >= 65) { word = "✅ Хороший сигнал";    wordCls = "score-strong"; }
  else if (score >= 45) { word = "⚡ Слабый сигнал";     wordCls = "score-ok";    }
  else                  { word = "⏳ Ждём момента";       wordCls = "score-weak";  }
  const wordEl = document.getElementById("ai-score-word");
  if (wordEl) { wordEl.textContent = word; wordEl.className = "ai-score-word " + wordCls; }

  // ── Мультитаймфрейм ───────────────────────────────────────────
  const mtf    = a.signals_mtf || [];
  const mtfEl  = document.getElementById("ai-mtf-list");
  if (mtfEl && mtf.length) {
    mtfEl.innerHTML = mtf.map(m => {
      const conf = Number(m.conf || 50);
      const col  = m.color || "#8892b0";
      return `<div class="ai-mtf-row" style="border-left:3px solid ${col}20">
        <span class="ai-mtf-tf">${m.tf}</span>
        <div style="flex:1">
          <div style="display:flex;justify-content:space-between;margin-bottom:3px">
            <span class="ai-mtf-sig" style="color:${col}">${m.signal}</span>
            <span class="ai-mtf-pct">${conf}%</span>
          </div>
          <div class="ai-comp-bar-track" style="height:3px">
            <div class="ai-comp-bar-fill" style="width:${conf}%;background:${col};box-shadow:none"></div>
          </div>
        </div>
      </div>`;
    }).join("");
  }

  // ── AI Компоненты ─────────────────────────────────────────────
  const comps   = a.ai_components || [];
  const compsEl = document.getElementById("ai-components-list");
  if (compsEl && comps.length) {
    compsEl.innerHTML = comps.map(c => {
      const pct = Number(c.pct || 0);
      const col = c.color || "#5cc8ff";
      return `<div class="ai-comp-row">
        <span class="ai-comp-icon">${c.icon}</span>
        <span class="ai-comp-name">${c.name}</span>
        <div class="ai-comp-bar-track">
          <div class="ai-comp-bar-fill" style="width:${pct}%;background:${col};color:${col}"></div>
        </div>
        <span class="ai-comp-pts" style="color:${col}">${c.val}/${c.max}</span>
      </div>`;
    }).join("");
  }

  // ── Предсказание цены ─────────────────────────────────────────
  const pStop = a.price_stop;
  const pNow  = a.price;
  const pTgt  = a.price_target;
  const rr    = Number(a.rr_ratio || 1);
  const prob  = Number(a.prob_win || 50);

  if (document.getElementById("ai-pred-stop"))
    document.getElementById("ai-pred-stop").textContent = pStop != null ? fmtPrice(pStop) : "—";
  if (document.getElementById("ai-pred-now"))
    document.getElementById("ai-pred-now").textContent  = pNow  != null ? fmtPrice(pNow)  : "—";
  if (document.getElementById("ai-pred-tgt"))
    document.getElementById("ai-pred-tgt").textContent  = pTgt  != null ? fmtPrice(pTgt)  : "—";
  if (document.getElementById("ai-rr-val"))
    document.getElementById("ai-rr-val").textContent    = rr.toFixed(2);
  if (document.getElementById("ai-win-prob"))
    document.getElementById("ai-win-prob").textContent  = prob;

  // R:R визуальная полоса
  const totalRange = pStop != null && pTgt != null && pNow != null
    ? Math.abs(pTgt - pStop) : 1;
  const stopWidth = pStop != null && pNow != null
    ? Math.abs(pNow - pStop) / totalRange * 100 : 33;
  const gainWidth = pTgt  != null && pNow != null
    ? Math.abs(pTgt - pNow) / totalRange * 100 : 55;

  const rrStop = document.getElementById("ai-rr-stop-fill");
  const rrGain = document.getElementById("ai-rr-gain-fill");
  if (rrStop) rrStop.style.width = stopWidth.toFixed(1) + "%";
  if (rrGain) rrGain.style.width = gainWidth.toFixed(1) + "%";
}

// ═══════════════════════════════════════════════════════════════════
//  🔴 AI STATUS TICKER — живая строка состояния
// ═══════════════════════════════════════════════════════════════════

// Часы в тикере
(function startClock() {
  function tick() {
    const el = document.getElementById("at-clock");
    if (el) el.textContent = new Date().toLocaleTimeString("ru-RU", {hour12:false});
  }
  tick();
  setInterval(tick, 1000);
})();

function updateTicker(data) {
  if (!data) return;
  const a = data.analysis || {};
  const ai = data.ai || {};
  const stats = data.stats || {};

  // Режим
  const running = data.running;
  const modeDot = document.getElementById("at-mode-dot");
  const modeTxt = document.getElementById("at-mode-txt");
  if (modeDot) modeDot.style.background = running ? "#00ff88" : "#ff4d6d";
  if (modeTxt) modeTxt.textContent = running ? "AUTO ON" : "СТОП";

  // RSI + Regime
  const rsiEl = document.getElementById("at-rsi");
  if (rsiEl) rsiEl.textContent = "RSI " + (a.rsi != null ? Number(a.rsi).toFixed(1) : "—");
  const regEl = document.getElementById("at-regime");
  if (regEl) {
    regEl.textContent = a.regime || "—";
    regEl.style.color = a.regime_color || "#8892b0";
  }

  // AI сигнал
  const aiSig  = document.getElementById("at-ai-sig");
  const aiConf = document.getElementById("at-ai-conf");
  const sigColors = {BUY:"#00ff88", SELL:"#ff4d6d", HOLD:"#8892b0"};
  if (aiSig) {
    const sig = ai.ai_signal || "HOLD";
    aiSig.textContent = sig;
    aiSig.style.color = sigColors[sig] || "#8892b0";
  }
  if (aiConf) aiConf.textContent = (ai.confidence || 0) + "%";

  // P&L
  const pnl = Number(stats.total_pnl || 0);
  const pnlEl = document.getElementById("at-pnl");
  if (pnlEl) {
    pnlEl.textContent = (pnl >= 0 ? "+" : "") + pnl.toFixed(4) + " TON";
    pnlEl.style.color = pnl >= 0 ? "#00ff88" : "#ff4d6d";
  }

  // Kelly fraction in ticker
  const kellyFrac = ai.kelly ? Number(ai.kelly.fraction || 0.5) : null;
  const kEl = document.getElementById("at-kelly-frac");
  if (kEl && kellyFrac !== null) {
    const kPct = (kellyFrac * 100).toFixed(0) + "%";
    kEl.textContent = kPct;
    kEl.style.color = kellyFrac >= 0.8 ? "#00ff88" : kellyFrac >= 0.5 ? "#ffd166" : "#ff4d6d";
  }
}

// ═══════════════════════════════════════════════════════════════════
//  📋 AI ЛОГ РЕШЕНИЙ
// ═══════════════════════════════════════════════════════════════════

function renderDecisionLog(log) {
  const el = document.getElementById("ai-declog-list");
  if (!el || !log) return;
  if (!log.length) {
    el.innerHTML = '<div class="empty-msg">Ждём первые тики…</div>';
    return;
  }
  const gradeColors = { A:"#00ff88", B:"#ffd166", C:"#ff8585" };
  el.innerHTML = log.slice(0, 12).map(d => {
    const result = d.result || "HOLD";
    const cls = result === "BUY" ? "dec-buy" : result === "SELL" ? "dec-sell" : "dec-hold";
    const icon = result === "BUY" ? "▲ BUY" : result === "SELL" ? "▼ SELL" : "— HOLD";
    const gc = gradeColors[d.quality] || "#8892b0";
    const src = (d.source || "").replace("AI🤖+ТА✅","🤖+✅").replace("AI🤖","🤖").replace("HOLD","—");
    const regShort = (d.regime || "").replace("RANGING","RANG").replace("DOWNTREND","DOWN")
      .replace("UPTREND","UP").replace("BREAKOUT","BRK").replace("VOLATILE","VOLT")
      .replace("TRANSITION","TRANS");
    const reasonTip = d.reason ? ` title="${d.reason}"` : "";
    return `<div class="ai-dec-row ${cls}"${reasonTip}>
      <span class="ai-dec-time">${d.t || "—"}</span>
      <span class="ai-dec-result">${icon}</span>
      <span class="ai-dec-conf" style="color:${result==='BUY'?'#00ff88':result==='SELL'?'#ff4d6d':'#8892b0'}">${d.conf || 0}%</span>
      <span class="ai-dec-rsi">RSI ${d.rsi != null ? d.rsi : "—"}</span>
      <span class="ai-dec-regime">${regShort}</span>
      <span style="font-size:9px;color:rgba(255,255,255,.4);min-width:30px">${src}</span>
      <span class="ai-dec-grade" style="color:${gc}">${d.quality || "C"}(${d.score || 0})</span>
    </div>`;
  }).join("");
}

// Раз в 15 сек запрашиваем лог отдельно
(function pollDecisionLog() {
  function fetchLog() {
    fetch("/api/ai/decisions").then(r => r.json()).then(log => {
      renderDecisionLog(log);
    }).catch(() => {});
  }
  fetchLog();
  setInterval(fetchLog, 15000);
})();

// ═══════════════════════════════════════════════════════════════════
//  🗄️ DB SYNC STATUS
// ═══════════════════════════════════════════════════════════════════

function updateBreakout(bo) {
  const row  = document.getElementById("brain-breakout-row");
  const icon = document.getElementById("bo-icon");
  const sig  = document.getElementById("bo-signal");
  const arc  = document.getElementById("bo-ring-arc");
  const txt  = document.getElementById("bo-score-txt");
  const km   = document.getElementById("bo-kelly-mult");
  const cb   = document.getElementById("bo-conf-boost");
  const trl  = document.getElementById("bo-trail-hint");
  if (!row) return;

  const b = bo || {};
  const score   = Number(b.score   || 0);
  const signal  = (b.signal  || "FLAT").toUpperCase();
  const kMult   = Number(b.kelly_mult || 1.0);
  const cBoost  = Number(b.conf_boost || 0);

  // Ring arc: circumference = 2π×18 ≈ 113.1
  const circ = 113.1;
  const fill  = (score / 100) * circ;
  if (arc) arc.setAttribute("stroke-dasharray", `${fill.toFixed(1)} ${circ}`);

  // Color by signal
  const colorMap = { RUNAWAY:"#ff4444", BREAKOUT:"#ffdd00", PRIMED:"#ff9900", COILING:"#0098ea", FLAT:"#00ff88" };
  if (arc) arc.setAttribute("stroke", colorMap[signal] || "#00ff88");
  if (txt) txt.textContent = score.toFixed(0);

  // Signal label + row class
  const clsMap = { RUNAWAY:"runaway", BREAKOUT:"breakout", PRIMED:"primed" };
  if (row) row.className = "brain-breakout-row " + (clsMap[signal] || "");
  if (sig) {
    sig.textContent = b.icon ? b.icon + " " + signal : signal;
    sig.className   = "bo-signal " + (clsMap[signal] || "");
  }
  if (icon) icon.textContent = b.icon || "💤";

  // Sub-bars
  const bars = {
    "bo-bb":   b.bb_squeeze  || 0,
    "bo-vol":  b.vol_acc     || 0,
    "bo-rsi":  b.rsi_build   || 0,
    "bo-macd": b.macd_cross  || 0,
    "bo-coil": b.coiling     || 0,
  };
  for (const [id, pct] of Object.entries(bars)) {
    const el = document.getElementById(id);
    if (el) el.style.width = Math.min(100, Math.max(0, pct)).toFixed(0) + "%";
  }

  // Footer
  if (km) km.textContent = kMult.toFixed(1) + "×";
  if (cb) {
    cb.textContent  = cBoost > 0 ? "+" + cBoost.toFixed(0) + "%" : "+0%";
    cb.style.color  = cBoost > 8 ? "#ff9900" : cBoost > 3 ? "#ffdd00" : "rgba(255,255,255,.5)";
  }
  if (trl) {
    const isWide = ["BREAKOUT","RUNAWAY","PRIMED"].includes(signal);
    trl.textContent  = signal === "RUNAWAY" ? "МАКС" : signal === "BREAKOUT" ? "ШИРОКИЙ" : isWide ? "шире" : "норма";
    trl.className    = "bbr-trail-hint" + (isWide ? " wide" : "");
  }
}

function updateMomentum(mom, fullRights) {
  const bar   = document.getElementById("bm-momentum-bar");
  const sig   = document.getElementById("bm-momentum-sig");
  const score = document.getElementById("bm-momentum-score");
  const vol   = document.getElementById("bm-vol-ratio");
  const rsi   = document.getElementById("bm-rsi-vel");
  const pvel  = document.getElementById("bm-price-vel");
  const boost = document.getElementById("bm-boost");
  const badge = document.getElementById("bm-rights-badge");
  if (!bar) return;

  const m = mom || {};
  const sc    = Number(m.score || 0);
  const msig  = (m.signal || "CALM").toUpperCase();
  const mboost = Number(m.boost || 0);

  if (bar) {
    bar.style.width = sc + "%";
    bar.className = "bm-momentum-bar" +
      (msig === "EXPLOSIVE" ? " explosive" : msig === "SURGE" ? " surge" : "");
  }
  if (sig) {
    sig.textContent = msig;
    sig.className = "bm-momentum-sig" +
      (msig === "EXPLOSIVE" ? " explosive" : msig === "SURGE" ? " surge" : "");
  }
  if (score) score.textContent = sc.toFixed(0);
  if (vol)   vol.textContent   = (Number(m.vol_ratio || 1)).toFixed(2) + "×";
  if (rsi)   rsi.textContent   = (Number(m.rsi_vel || 0) >= 0 ? "+" : "") + (Number(m.rsi_vel || 0)).toFixed(1);
  if (pvel)  pvel.textContent  = (Number(m.price_vel || 0) >= 0 ? "+" : "") + (Number(m.price_vel || 0)).toFixed(2) + "%";
  if (boost) {
    boost.textContent = mboost > 0 ? "+" + mboost.toFixed(0) + "%" : "+0%";
    boost.className   = "bm-ms-val bm-boost-val" + (mboost > 0 ? " active" : "");
  }

  if (badge) {
    const active = fullRights !== false;
    badge.style.opacity = active ? "1" : "0.4";
    badge.title = active
      ? "AI имеет полные права — ATR-фильтр снят при достаточной уверенности"
      : "AI полные права: ожидание достаточной уверенности";
  }
}

function updateDBSync(data) {
  const secs = data != null ? Number(data) : null;
  const badgeEl  = document.getElementById("ai-db-badge");
  const detailEl = document.getElementById("ai-db-detail");
  const atDbEl   = document.getElementById("at-db-status");
  let badgeTxt, badgeCls, detail;
  if (secs === null) {
    badgeTxt = "нет синхр."; badgeCls = "db-warn";
    detail = "Ещё не было синхронизации";
  } else if (secs < 120) {
    badgeTxt = "✅ SYNC"; badgeCls = "db-ok";
    detail = `Синхронизировано ${secs}с назад`;
  } else {
    badgeTxt = "⚠️ " + Math.floor(secs/60) + "м назад"; badgeCls = "db-warn";
    detail = `Последняя синхр. ${Math.floor(secs/60)} мин назад`;
  }
  if (badgeEl) { badgeEl.textContent = badgeTxt; badgeEl.className = "ai-db-badge " + badgeCls; }
  if (detailEl) detailEl.textContent = detail;
  if (atDbEl) atDbEl.textContent = secs != null ? (secs < 120 ? "✅ " + secs + "с" : "⚠️ " + Math.floor(secs/60) + "м") : "—";
}

(function pollDBSync() {
  function fetchDB() {
    fetch("/api/db/sync_status").then(r => r.json()).then(d => {
      const secs = d.secs_ago;
      updateDBSync(secs);
      const detailEl = document.getElementById("ai-db-detail");
      if (detailEl && d.ok) {
        const sStr = secs != null ? ` · ${secs}с назад` : "";
        detailEl.textContent = `✅ Подключено · ${d.trades || 0} сделок в БД · ${d.open || 0} открытых${sStr}`;
      }
      if (detailEl && !d.ok) {
        detailEl.textContent = "❌ Нет подключения к БД";
      }
    }).catch(() => updateDBSync(null));
  }
  fetchDB();
  setInterval(fetchDB, 30000);
})();

// ═══════════════════════════════════════════════════════════════════
//  ⚡ РУЧНОЕ УПРАВЛЕНИЕ
// ═══════════════════════════════════════════════════════════════════

function adjustManualAmt(delta) {
  const el = document.getElementById("manual-amount");
  if (!el) return;
  const cur = parseInt(el.value) || 7;
  el.value = Math.max(1, Math.min(1000, cur + delta));
}

function setManualStatus(msg, color) {
  const el = document.getElementById("ai-manual-status");
  if (!el) return;
  el.textContent = msg;
  el.style.color = color || "rgba(255,255,255,.5)";
  if (msg) setTimeout(() => { el.textContent = ""; }, 5000);
}

function setManualLoading(loading) {
  const btnBuy  = document.getElementById("btn-manual-buy");
  const btnSell = document.getElementById("btn-manual-sell");
  if (btnBuy)  btnBuy.disabled  = loading;
  if (btnSell) btnSell.disabled = loading;
}

function manualBuy() {
  const amount = parseFloat(document.getElementById("manual-amount")?.value || 7);
  if (isNaN(amount) || amount < 1) { setManualStatus("❌ Укажите корректный объём", "#ff4d6d"); return; }
  setManualLoading(true);
  setManualStatus("⏳ Отправляю ордер покупки…", "#ffd166");
  fetch("/api/trade/manual_buy", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({amount})
  }).then(r => r.json()).then(d => {
    setManualLoading(false);
    if (d.ok) {
      setManualStatus(`✅ Куплено! Цена: ${d.price != null ? fmtPrice(d.price) : "—"}`, "#00ff88");
      showToast("✅ Ручная покупка исполнена", "ok");
    } else {
      setManualStatus("❌ " + (d.error || "Ошибка"), "#ff4d6d");
      showToast("❌ " + (d.error || "Ошибка"), "err");
    }
  }).catch(() => {
    setManualLoading(false);
    setManualStatus("❌ Ошибка соединения", "#ff4d6d");
  });
}

function manualSellAll() {
  if (!confirm("Продать все позиции? (применяется правило: только в плюс)")) return;
  setManualLoading(true);
  setManualStatus("⏳ Закрываю все позиции…", "#ffd166");
  fetch("/api/trade/manual_sell_all", {method:"POST"}).then(r => r.json()).then(d => {
    setManualLoading(false);
    if (d.ok) {
      setManualStatus(`✅ Закрыто позиций: ${d.closed || 0}`, "#00ff88");
      showToast("✅ Все позиции закрыты", "ok");
    } else {
      setManualStatus("⚠️ " + (d.error || "Нельзя продать"), "#ffd166");
      showToast("⚠️ " + (d.error || "Нельзя продать сейчас"), "info");
    }
  }).catch(() => {
    setManualLoading(false);
    setManualStatus("❌ Ошибка соединения", "#ff4d6d");
  });
}

// ═══════════════════════════════════════════════════════════════════
//  📉 ШОРТ-ПОЗИЦИИ — рендер карточек
// ═══════════════════════════════════════════════════════════════════

function renderOpenShortTrades(shorts, curPrice, gramPrice) {
  const el = document.getElementById("open-short-trades-list");
  if (!el) return;
  if (!shorts || !shorts.length) {
    el.innerHTML = '<div class="empty-msg">Нет открытых шортов — AI ищет сигнал SELL</div>';
    return;
  }
  el.innerHTML = shorts.map(t => {
    const entry    = Number(t.entry_price) || 0;
    const amount   = Number(t.amount) || 0;
    const tonRecv  = Number(t.ton_received) || 0;
    const dropNow  = Number(t.drop_pct_now) || 0;
    const reqDrop  = Number(t.required_drop_pct) || 0;
    const progress = Number(t.progress_pct) || 0;
    const inProfit = !!t.in_profit;
    const pnlTon   = t.pnl_ton_now != null ? Number(t.pnl_ton_now) : null;
    const gProfit  = t.grinch_profit_est != null ? Number(t.grinch_profit_est) : null;
    const lowWater = Number(t.low_water) || entry;
    const tp       = Number(t.take_profit) || 0;

    const pnlCls  = inProfit ? "pnl-pos" : "pnl-neg";
    const barClr  = inProfit
      ? "linear-gradient(90deg,#ffd166,#00ff88)"
      : "linear-gradient(90deg,#ff4d6d,#ffd166)";

    let waitLabel, waitColor;
    if (inProfit && dropNow >= reqDrop * 2) {
      waitLabel = "🎯 Готово к откупке (≥2× цели)"; waitColor = "#00ff88";
    } else if (inProfit) {
      waitLabel = `✅ В прибыли ${dropNow.toFixed(1)}% — ждём трейлинг`; waitColor = "#00d4aa";
    } else {
      waitLabel = `⏳ Нужно упасть ещё −${Math.max(0, reqDrop - dropNow).toFixed(1)}%`; waitColor = "#ffd166";
    }

    return `
    <div class="trade-card short-card">
      <div class="trade-row">
        <span class="trade-side short">📉 ШОРТ — ЖДЁТ ОТКУПКИ</span>
        <span class="${pnlCls}" style="font-weight:700">${dropNow >= 0 ? "-" : "+"}${Math.abs(dropNow).toFixed(2)}%</span>
      </div>
      <div class="trade-row" style="font-size:11px;color:#8892b0">
        <span>Продали по: <b style="color:#e2e8f0">${fmtPrice(entry)}</b></span>
        <span>Дно: <b style="color:#ff4d6d">${fmtPrice(lowWater)}</b></span>
      </div>
      <div class="trade-row" style="font-size:11px;color:#8892b0">
        <span>Откупить при: <b style="color:#00ff88">${fmtPrice(tp)}</b> (−${reqDrop.toFixed(1)}%)</span>
      </div>
      <!-- Прогресс к цели -->
      <div class="short-prog-wrap" title="Прогресс к цели">
        <div class="short-prog-bar" style="width:${Math.min(progress, 100).toFixed(1)}%;background:${barClr}"></div>
      </div>
      <div class="trade-row" style="font-size:10px;color:rgba(255,255,255,.4)">
        <span>Прогресс: ${progress.toFixed(0)}% от цели (нужно −${reqDrop.toFixed(1)}%)</span>
      </div>
      <div class="trade-row">
        <span class="ot-wait" style="color:${waitColor}">${waitLabel}</span>
      </div>
      <div style="margin:6px 0;padding:8px 10px;border-radius:8px;background:${inProfit ? 'rgba(0,255,136,0.08)' : 'rgba(255,77,109,0.08)'};border:1px solid ${inProfit ? 'rgba(0,255,136,0.25)' : 'rgba(255,77,109,0.25)'}">
        <div class="trade-row" style="font-size:11px;color:#8892b0;margin-bottom:3px">
          <span>Если откупить сейчас (−1% DEX −газ):</span>
        </div>
        <div class="trade-row">
          <b style="font-size:16px;font-weight:900;color:${inProfit ? '#00ff88' : '#ff4d6d'}">
            ${gProfit != null ? (gProfit >= 0 ? "+" : "") + gProfit.toFixed(2) + " GRINCH" : "—"}
          </b>
          <span style="font-size:12px;color:rgba(255,255,255,.4)">
            ${pnlTon != null ? (pnlTon >= 0 ? "+" : "") + pnlTon.toFixed(4) + " TON" : ""}
          </span>
        </div>
      </div>
      <div class="trade-row" style="font-size:10px;color:#4a5568">
        <span>Продано: <b style="color:#e2e8f0">${amount.toFixed(2)}</b> GRINCH</span>
        <span>TON получено: <b style="color:#e2e8f0">${tonRecv.toFixed(4)}</b></span>
        ${t.ai_confidence ? `<span style="color:#a78bfa">AI ${t.ai_confidence}%</span>` : ""}
      </div>
    </div>`;
  }).join("");
}
