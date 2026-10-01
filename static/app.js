const PAGE_SIZE = 200;

// Two tabs over the same table machinery. 'active' = products still firing
// out_of_stock_view; 'recovered' = the ones the server's recency gate dropped,
// i.e. presumed back in stock, with how long they were out of stock.
// Each keeps its own sort/page so switching tabs doesn't reset the other.
const VIEWS = {
  active: {
    endpoint: '/api/products',
    exportView: 'active',
    tableWrap: 'activeTableWrap',
    body: 'productsBody',
    table: 'productsTable',
    tab: 'tabActive',
    count: 'tabActiveCount',
    cols: 7,
    usesDateRange: true,
  },
  recovered: {
    endpoint: '/api/back-in-stock',
    exportView: 'back_in_stock',
    tableWrap: 'recoveredTableWrap',
    body: 'recoveredBody',
    table: 'recoveredTable',
    tab: 'tabRecovered',
    count: 'tabRecoveredCount',
    cols: 9,
    // The back-in-stock figures are counted across all synced data, not the
    // toolbar range (a finished outage can sit entirely outside it), so the
    // date inputs are hidden on this tab rather than silently ignored.
    usesDateRange: false,
  },
};

const state = {
  view: 'active',
  active: { items: [], offset: 0, total: 0, sort: 'oos_views', dir: 'desc', meta: null },
  recovered: { items: [], offset: 0, total: 0, sort: 'days_oos', dir: 'desc', meta: null },
};

const el = (id) => document.getElementById(id);
const cfg = () => VIEWS[state.view];
const vs = () => state[state.view];

function defaultDates() {
  const end = new Date(); end.setDate(end.getDate() - 1);
  const start = new Date(end); start.setDate(start.getDate() - 29);
  const fmt = (d) => d.toISOString().split('T')[0];
  el('startDate').value = fmt(start);
  el('endDate').value = fmt(end);
}

async function loadFilters() {
  const [cats, partners] = await Promise.all([
    fetch('/api/categories').then(r => r.json()),
    fetch('/api/partners').then(r => r.json()),
  ]);
  const catSel = el('categoryFilter');
  cats.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c; opt.textContent = c;
    catSel.appendChild(opt);
  });
  const partnerSel = el('partnerFilter');
  partners.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p; opt.textContent = p;
    partnerSel.appendChild(opt);
  });
}

async function loadStatus() {
  const s = await fetch('/api/status').then(r => r.json());
  const bar = el('statusBar');
  if (!s || !s.products) {
    bar.textContent = 'No data yet — click "Refresh Data" to pull the latest out-of-stock events.';
    return;
  }
  bar.textContent = `${s.products} products tracked (${s.active_products} active in the last ${s.recency_days} day${s.recency_days === 1 ? '' : 's'}) · data ${s.min_date} → ${s.max_date} · last refreshed ${s.last_updated || '—'}`;
}

// The recency gate is applied server-side to every view and to the export;
// this explains why a product the user expected to see isn't listed (active
// tab), or why it's listed as recovered (back-in-stock tab).
function renderRecencyNote(r) {
  const note = el('recencyNote');
  vs().meta = r || null;
  if (!r) { note.hidden = true; return; }
  note.hidden = false;
  note.classList.toggle('recency-stale', !!r.data_stale);
  note.classList.toggle('recency-recovered', state.view === 'recovered' && !r.data_stale);

  if (state.view === 'recovered') {
    const stale = r.data_stale
      ? ` ⚠️ Latest synced data is ${r.latest_data_date}, older than the cutoff — so every product looks recovered because the data stopped, not because stock came back. Click “Refresh Data”.`
      : '';
    note.innerHTML = `✅ <strong>Products that stopped going out of stock</strong> — no out-of-stock hit since <strong>${r.cutoff}</strong> (last ${r.days} day${r.days === 1 ? '' : 's'}), so they're presumed back in stock. <strong>Days OOS</strong> counts days that fired at least one event and <strong>OOS Span</strong> is the first→last calendar gap, both across all synced data (not the date range above) — a GA4-visit proxy, not an inventory feed.${stale}`;
    return;
  }

  const hidden = r.hidden_stale
    ? ` ${r.hidden_stale.toLocaleString()} product${r.hidden_stale === 1 ? '' : 's'} with hits inside the date range ${r.hidden_stale === 1 ? 'was' : 'were'} hidden as no longer active — see the “Back in stock” tab.`
    : '';
  const stale = r.data_stale
    ? ` ⚠️ Latest synced data is ${r.latest_data_date}, older than the cutoff — click “Refresh Data” to pull the newest GA4 days.`
    : '';
  note.innerHTML = `🕒 <strong>Showing only still-active products</strong> — at least one out-of-stock hit on or after <strong>${r.cutoff}</strong> (last ${r.days} day${r.days === 1 ? '' : 's'}, counted back from today).${hidden}${stale}`;
}

function buildFilterParams(view = state.view) {
  const params = new URLSearchParams();
  if (VIEWS[view].usesDateRange) {
    if (el('startDate').value) params.set('start', el('startDate').value);
    if (el('endDate').value) params.set('end', el('endDate').value);
  }
  if (el('categoryFilter').value) params.set('category', el('categoryFilter').value);
  if (el('partnerFilter').value) params.set('partner', el('partnerFilter').value);
  if (el('sourceFilter').value) params.set('source', el('sourceFilter').value);
  if (el('searchBox').value.trim()) params.set('q', el('searchBox').value.trim());
  params.set('sort', state[view].sort);
  params.set('dir', state[view].dir);
  return params;
}

function buildQuery() {
  const params = buildFilterParams();
  params.set('limit', PAGE_SIZE);
  params.set('offset', vs().offset);
  return params.toString();
}

async function loadProducts(resetPage = true) {
  const view = state.view;
  if (resetPage) state[view].offset = 0;
  const body = el(cfg().body);
  body.innerHTML = `<tr><td colspan="${cfg().cols}" class="empty-state">Loading…</td></tr>`;
  const data = await fetch(`${cfg().endpoint}?${buildQuery()}`).then(r => r.json());
  state[view].items = data.items || [];
  state[view].total = data.count || 0;
  renderRecencyNote(data.recency);
  renderTable();
  renderPagination();
  renderTabCounts();
  loadOtherTabCount();
}

function renderTabCounts() {
  el(cfg().count).textContent = vs().total.toLocaleString();
}

// Badge for the tab you're NOT on. Hitting that tab's real endpoint would
// re-run its whole per-product rollup (seconds, on a real month of data)
// just to read one number, so there's a dedicated count-only endpoint that
// both badges come from instead.
async function loadOtherTabCount() {
  const other = state.view === 'active' ? 'recovered' : 'active';
  try {
    const params = new URLSearchParams();
    if (el('startDate').value) params.set('start', el('startDate').value);
    if (el('endDate').value) params.set('end', el('endDate').value);
    if (el('categoryFilter').value) params.set('category', el('categoryFilter').value);
    if (el('partnerFilter').value) params.set('partner', el('partnerFilter').value);
    if (el('sourceFilter').value) params.set('source', el('sourceFilter').value);
    if (el('searchBox').value.trim()) params.set('q', el('searchBox').value.trim());
    params.set('need', other);  // skip the half this page already knows exactly
    const counts = await fetch(`/api/tab-counts?${params.toString()}`).then(r => r.json());
    state[other].total = counts[other] || 0;
    el(VIEWS[other].count).textContent = state[other].total.toLocaleString();
  } catch (e) {
    el(VIEWS[other].count).textContent = '—';
  }
}

function renderPagination() {
  const el2 = el('pagination');
  const v = vs();
  if (v.total === 0) { el2.innerHTML = ''; return; }
  const start = v.offset + 1;
  const end = Math.min(v.offset + PAGE_SIZE, v.total);
  el2.innerHTML = `
    <button class="btn btn-secondary" id="prevPage" ${v.offset === 0 ? 'disabled' : ''}>← Previous</button>
    <span>${start}–${end} of ${v.total.toLocaleString()}</span>
    <button class="btn btn-secondary" id="nextPage" ${end >= v.total ? 'disabled' : ''}>Next →</button>
  `;
  const prev = document.getElementById('prevPage');
  const next = document.getElementById('nextPage');
  if (prev) prev.addEventListener('click', () => { vs().offset = Math.max(0, vs().offset - PAGE_SIZE); loadProducts(false); });
  if (next) next.addEventListener('click', () => { vs().offset += PAGE_SIZE; loadProducts(false); });
}

function emptyMessage() {
  const m = vs().meta;
  if (state.view === 'recovered') {
    return m
      ? `No products have gone quiet. Everything tracked still fired an out-of-stock hit on or after ${m.cutoff}.`
      : 'No recovered products match these filters.';
  }
  const gate = m
    ? ` Products with no out-of-stock hits since ${m.cutoff} (last ${m.days} day${m.days === 1 ? '' : 's'}) are excluded — see the “Back in stock” tab.`
    : '';
  return `No products match these filters.${gate}`;
}

function renderTable() {
  const body = el(cfg().body);
  const items = vs().items;
  if (items.length === 0) {
    body.innerHTML = `<tr><td colspan="${cfg().cols}" class="empty-state">${escapeHtml(emptyMessage())}</td></tr>`;
    return;
  }
  body.innerHTML = state.view === 'recovered'
    ? items.map(recoveredRow).join('')
    : items.map(activeRow).join('');

  body.querySelectorAll('tr[data-code]').forEach(row => {
    row.addEventListener('click', () => openTrend(row.dataset.code));
  });
}

function activeRow(item) {
  return `
    <tr data-code="${escapeAttr(item.product_code)}">
      <td class="pc-code">${escapeHtml(item.product_code)}</td>
      <td>${escapeHtml(item.product_name || '—')}</td>
      <td>${item.partner_code ? `<span class="partner-badge">${escapeHtml(item.partner_code)}</span>` : '—'}</td>
      <td>${item.category ? `<span class="category-badge">${escapeHtml(item.category)}</span>` : '—'}</td>
      <td class="num">${item.oos_views.toLocaleString()}</td>
      <td class="num">${item.days_oos}</td>
      <td class="dates-cell">${item.first_oos_date || '—'} → ${item.last_oos_date || '—'}</td>
    </tr>
  `;
}

function recoveredRow(item) {
  const back = item.days_since_last_oos;
  // "~" because the gate works off daily buckets with GA4's own reporting
  // lag behind them, so this is accurate to a day or two at best.
  const backLabel = back === null || back === undefined
    ? '—'
    : `<span class="recovered-badge">~${back}d</span>`;
  return `
    <tr data-code="${escapeAttr(item.product_code)}">
      <td class="pc-code">${escapeHtml(item.product_code)}</td>
      <td>${escapeHtml(item.product_name || '—')}</td>
      <td>${item.partner_code ? `<span class="partner-badge">${escapeHtml(item.partner_code)}</span>` : '—'}</td>
      <td>${item.category ? `<span class="category-badge">${escapeHtml(item.category)}</span>` : '—'}</td>
      <td class="num days-oos-strong">${item.days_oos}</td>
      <td class="num"><span class="muted-note">${item.oos_span_days}d</span></td>
      <td class="num">${item.oos_views.toLocaleString()}</td>
      <td class="dates-cell">${item.first_oos_date || '—'} → ${item.last_oos_date || '—'}</td>
      <td class="num">${backLabel}</td>
    </tr>
  `;
}

function renderSortIndicators() {
  const table = el(cfg().table);
  table.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === vs().sort) {
      th.classList.add(vs().dir === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  });
}

// Delegated so both tables' headers share one handler and sort the view
// they belong to.
['productsTable', 'recoveredTable'].forEach(tableId => {
  el(tableId).querySelectorAll('th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      const v = vs();
      if (v.sort === key) {
        v.dir = v.dir === 'asc' ? 'desc' : 'asc';
      } else {
        v.sort = key;
        v.dir = 'desc';
      }
      renderSortIndicators();
      loadProducts();
    });
  });
});

function switchView(view) {
  if (!VIEWS[view] || view === state.view) return;
  state.view = view;
  Object.entries(VIEWS).forEach(([name, c]) => {
    const isCurrent = name === view;
    el(c.tableWrap).hidden = !isCurrent;
    el(c.tab).classList.toggle('active', isCurrent);
    el(c.tab).setAttribute('aria-selected', String(isCurrent));
  });
  // Date range only means something on the active report (see VIEWS above).
  el('dateFilterGroup').hidden = !cfg().usesDateRange;
  renderSortIndicators();
  loadProducts(false);
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => switchView(tab.dataset.view));
});

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

// ─── Trend modal ───────────────────────────────────────────────

async function openTrend(productCode) {
  const modal = el('trendModal');
  modal.classList.add('open');
  el('trendTitle').textContent = 'Loading trend…';
  el('trendMeta').innerHTML = '';

  const data = await fetch(`/api/trend/${encodeURIComponent(productCode)}?days=90`).then(r => r.json());
  if (data.detail) {
    el('trendTitle').textContent = 'Not found';
    return;
  }

  const sourceLabel = data.source_type === 'partner_central' ? 'Partner Central' : 'Ecommerce';
  el('trendTitle').textContent = data.product_name || data.product_code;
  el('trendMeta').innerHTML = `
    <span>Code: <strong>${escapeHtml(data.product_code)}</strong></span>
    <span>Source: <strong>${sourceLabel}</strong></span>
    <span>Partner: <strong>${escapeHtml(data.partner_code || '—')}</strong></span>
    <span>Category: <strong>${escapeHtml(data.category || '—')}</strong></span>
    <span>Days OOS: <strong>${data.days_oos}</strong></span>
    <span>First/Last OOS: <strong>${data.first_oos_date || '—'} → ${data.last_oos_date || '—'}</strong></span>
  `;

  drawTrend(data.series || []);
}

function drawTrend(series) {
  const canvas = el('trendCanvas');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 900;
  const h = 360;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  if (series.length === 0) {
    ctx.fillStyle = '#86868b';
    ctx.font = '14px DM Sans, sans-serif';
    ctx.fillText('No data in range', 20, 30);
    return;
  }

  const padding = { top: 20, right: 20, bottom: 30, left: 44 };
  const chartW = w - padding.left - padding.right;
  const chartH = h - padding.top - padding.bottom;

  const maxVal = Math.max(...series.map(s => s.oos_views), 1);

  // gridlines
  ctx.strokeStyle = '#f2f2f7';
  ctx.lineWidth = 1;
  const gridLines = 4;
  for (let i = 0; i <= gridLines; i++) {
    const y = padding.top + (chartH / gridLines) * i;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(w - padding.right, y);
    ctx.stroke();
    const val = Math.round(maxVal - (maxVal / gridLines) * i);
    ctx.fillStyle = '#86868b';
    ctx.font = '11px DM Sans, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(val, padding.left - 8, y + 4);
  }

  const xStep = series.length > 1 ? chartW / (series.length - 1) : 0;
  const xFor = (i) => padding.left + xStep * i;
  const yFor = (val) => padding.top + chartH - (val / maxVal) * chartH;

  function drawLine(key, color) {
    ctx.beginPath();
    series.forEach((pt, i) => {
      const x = xFor(i);
      const y = yFor(pt[key]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();

    series.forEach((pt, i) => {
      ctx.beginPath();
      ctx.arc(xFor(i), yFor(pt[key]), 2.5, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    });
  }

  drawLine('oos_views', '#3d2166');

  // x-axis labels (sparse)
  ctx.fillStyle = '#86868b';
  ctx.font = '11px DM Sans, sans-serif';
  ctx.textAlign = 'center';
  const labelEvery = Math.max(1, Math.ceil(series.length / 8));
  series.forEach((pt, i) => {
    if (i % labelEvery === 0 || i === series.length - 1) {
      ctx.fillText(pt.date.slice(5), xFor(i), h - 8);
    }
  });
}

el('modalClose').addEventListener('click', () => el('trendModal').classList.remove('open'));
el('trendModal').addEventListener('click', (e) => {
  if (e.target === el('trendModal')) el('trendModal').classList.remove('open');
});

// Filters auto-apply: every select fires immediately on change, the search
// box debounces so it doesn't re-query on every keystroke.
['startDate', 'endDate', 'categoryFilter', 'partnerFilter', 'sourceFilter'].forEach(id => {
  el(id).addEventListener('change', () => loadProducts());
});

let searchDebounce;
el('searchBox').addEventListener('input', () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => loadProducts(), 400);
});
el('searchBox').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { clearTimeout(searchDebounce); loadProducts(); }
});

el('refreshBtn').addEventListener('click', async () => {
  const btn = el('refreshBtn');
  btn.disabled = true;
  btn.textContent = '↻ Refreshing…';
  try {
    await fetch('/api/refresh?days=30', { method: 'POST' });
    await loadStatus();
    await loadFilters();
    await loadProducts();
  } catch (e) {
    alert('Refresh failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ Refresh Data';
  }
});

// Exports whichever tab is open, with that tab's filters and sort.
el('exportBtn').addEventListener('click', () => {
  const params = buildFilterParams();
  params.set('view', cfg().exportView);
  window.location.href = `/api/export?${params.toString()}`;
});

(async function init() {
  defaultDates();
  renderSortIndicators();
  await loadFilters();
  await loadStatus();
  await loadProducts();
})();
