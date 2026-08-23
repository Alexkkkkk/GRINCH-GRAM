const socket = io();
let pnlChart, priceChart;

function initCharts() {
    const ctx1 = document.getElementById('pnlChart').getContext('2d');
    pnlChart = new Chart(ctx1, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'PnL USDT',
                data: [],
                borderColor: '#0ecb81',
                backgroundColor: 'rgba(14, 203, 129, 0.08)',
                fill: true,
                tension: 0.4,
                pointRadius: 0,
                borderWidth: 2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { display: false },
                y: { grid: { color: '#2b3139' }, ticks: { color: '#848e9c', font: { size: 10 } } }
            },
            interaction: { intersect: false, mode: 'index' },
        }
    });

    const ctx2 = document.getElementById('priceChart').getContext('2d');
    priceChart = new Chart(ctx2, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Цена',
                data: [],
                borderColor: '#f0b90b',
                backgroundColor: 'rgba(240, 185, 11, 0.05)',
                fill: true,
                tension: 0.4,
                pointRadius: 0,
                borderWidth: 2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { display: false },
                y: { grid: { color: '#2b3139' }, ticks: { color: '#848e9c', font: { size: 10 } } }
            }
        }
    });
}

socket.on('status', (data) => {
    if (!data || !data.stats) return;
    updateUI(data);
});

function updateUI(data) {
    const s = data.stats;
    document.getElementById('currentPrice').textContent = s.current_price ? s.current_price.toFixed(5) : '0.01315';
    document.getElementById('symbol').textContent = data.symbol || 'AUDIO/USDT';

    const roiEl = document.getElementById('roi');
    roiEl.textContent = (s.roi_pct >= 0 ? '+' : '') + s.roi_pct.toFixed(2) + '%';
    roiEl.className = 'stat-value ' + (s.roi_pct >= 0 ? 'positive' : 'negative');

    const pnlEl = document.getElementById('pnl');
    pnlEl.textContent = (s.total_profit_usdt >= 0 ? '+' : '') + s.total_profit_usdt.toFixed(2);
    pnlEl.className = 'stat-value ' + (s.total_profit_usdt >= 0 ? 'positive' : 'negative');

    document.getElementById('avgProfit').textContent = (s.avg_profit_per_grid || 0).toFixed(2) + ' USDT';
    document.getElementById('trades').textContent = (s.buy_trades || 0) + ' / ' + (s.sell_trades || 0);
    document.getElementById('activeOrders').textContent = s.active_orders || 0;

    const sec = s.running_time_sec || 0;
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    document.getElementById('uptime').textContent = d + 'д. ' + h + 'ч. ' + m + 'мин.';

    updateCharts(data);
    updateGridVisual(data.levels, s.current_price, s.upper_price, s.lower_price);
}

function updateCharts(data) {
    fetch('/api/history?hours=24')
        .then(r => r.json())
        .then(hist => {
            if (!hist.pnl || hist.pnl.length === 0) return;
            const labels = hist.pnl.map((_, i) => i);
            pnlChart.data.labels = labels;
            pnlChart.data.datasets[0].data = hist.pnl.map(p => p.total_profit_usdt);
            pnlChart.update('none');
            priceChart.data.labels = labels;
            priceChart.data.datasets[0].data = hist.pnl.map(p => p.current_price);
            priceChart.update('none');
        });
}

function updateGridVisual(levels, currentPrice, upper, lower) {
    const container = document.getElementById('gridVisual');
    if (!container) return;
    container.querySelectorAll('.grid-level').forEach(e => e.remove());

    if (!levels || levels.length === 0 || !currentPrice) {
        const pl = document.getElementById('priceLine');
        if (pl) pl.style.top = '50%';
        return;
    }

    const minP = lower || Math.min(...levels.map(l => l.price));
    const maxP = upper || Math.max(...levels.map(l => l.price));
    const range = maxP - minP || 1;

    const priceLine = document.getElementById('priceLine');
    const pct = 1 - ((currentPrice - minP) / range);
    priceLine.style.top = (Math.max(0.02, Math.min(0.98, pct)) * 100) + '%';
    document.getElementById('priceTag').textContent = currentPrice.toFixed(5);

    levels.forEach(lvl => {
        const el = document.createElement('div');
        el.className = 'grid-level ' + lvl.side + ' ' + lvl.status;
        const lvlPct = 1 - ((lvl.price - minP) / range);
        el.style.top = (Math.max(0, Math.min(1, lvlPct)) * 100) + '%';
        const tag = document.createElement('span');
        tag.className = 'level-tag ' + lvl.side;
        tag.textContent = lvl.side[0].toUpperCase() + ' ' + lvl.price.toFixed(5);
        el.appendChild(tag);
        container.appendChild(el);
    });
}

async function startBot() {
    await fetch('/api/start', {method: 'POST'});
    addLog('Бот запущен', 'info');
}

async function stopBot() {
    await fetch('/api/stop', {method: 'POST'});
    addLog('Бот остановлен', 'sell');
}

async function buildGrid() {
    const body = {
        upper: parseFloat(document.getElementById('upperInput').value) || null,
        lower: parseFloat(document.getElementById('lowerInput').value) || null,
        grid_count: parseInt(document.getElementById('gridCount').value) || 40,
        investment: parseFloat(document.getElementById('investment').value) || 1000,
    };
    const res = await fetch('/api/build', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
    const data = await res.json();
    if (data.ok) {
        addLog('Сетка построена: ' + data.levels_count + ' уровней', 'buy');
    } else {
        addLog('Ошибка: ' + (data.error || 'unknown'), 'sell');
    }
}

function addLog(msg, type) {
    const panel = document.getElementById('logPanel');
    const entry = document.createElement('div');
    entry.className = 'log-entry ' + type;
    entry.textContent = '[' + new Date().toLocaleTimeString() + '] ' + msg;
    panel.insertBefore(entry, panel.firstChild);
    if (panel.children.length > 40) panel.lastChild.remove();
}

socket.emit('connect');
initCharts();
fetch('/api/status').then(r => r.json()).then(updateUI);
