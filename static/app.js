const PAGE_SIZE = 200;
const state = { items: [], offset: 0, total: 0, sort: 'oos_views', dir: 'desc', recency: null };

const el = (id) => document.getElementById(id);

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
    bar.textContent = 'No data yet — click "Refresh from GA4" to pull the latest out-of-stock events.';
    return;
  }
  bar.textContent = `${s.products} products tracked (${s.active_products} active in the last ${s.recency_days} day${s.recency_days === 1 ? '' : 's'}) · data ${s.min_date} → ${s.max_date} · last refreshed ${s.last_updated || '—'}`;
}

// The recency gate is applied server-side to every view and to the export;
// this just explains why a product the user expected to see isn't listed.
function renderRecencyNote(r) {
  const note = el('recencyNote');
  state.recency = r || null;
  if (!r) { note.hidden = true; return; }
  note.hidden = false;
  note.classList.toggle('recency-stale', !!r.data_stale);
  const hidden = r.hidden_stale
    ? ` ${r.hidden_stale.toLocaleString()} product${r.hidden_stale === 1 ? '' : 's'} with hits inside the date range ${r.hidden_stale === 1 ? 'was' : 'were'} hidden as no longer active.`
    : '';
  const stale = r.data_stale
    ? ` ⚠️ Latest synced data is ${r.latest_data_date}, older than the cutoff — click “Refresh Data” to pull the newest GA4 days.`
    : '';
  note.innerHTML = `🕒 <strong>Showing only still-active products</strong> — at least one out-of-stock hit on or after <strong>${r.cutoff}</strong> (last ${r.days} day${r.days === 1 ? '' : 's'}, counted back from today).${hidden}${stale}`;
}

function buildFilterParams() {
  const params = new URLSearchParams();
  if (el('startDate').value) params.set('start', el('startDate').value);
  if (el('endDate').value) params.set('end', el('endDate').value);
  if (el('categoryFilter').value) params.set('category', el('categoryFilter').value);
  if (el('partnerFilter').value) params.set('partner', el('partnerFilter').value);
  if (el('sourceFilter').value) params.set('source', el('sourceFilter').value);
  if (el('searchBox').value.trim()) params.set('q', el('searchBox').value.trim());
  params.set('sort', state.sort);
  params.set('dir', state.dir);
  return params;
}

function buildQuery() {
  const params = buildFilterParams();
  params.set('limit', PAGE_SIZE);
  params.set('offset', state.offset);
  return params.toString();
}

async function loadProducts(resetPage = true) {
  if (resetPage) state.offset = 0;
  const body = el('productsBody');
  body.innerHTML = '<tr><td colspan="7" class="empty-state">Loading…</td></tr>';
  const data = await fetch(`/api/products?${buildQuery()}`).then(r => r.json());
  state.items = data.items || [];
  state.total = data.count || 0;
  renderRecencyNote(data.recency);
  renderTable();
  renderPagination();
}

function renderPagination() {
  const el2 = el('pagination');
  if (state.total === 0) { el2.innerHTML = ''; return; }
  const start = state.offset + 1;
  const end = Math.min(state.offset + PAGE_SIZE, state.total);
  el2.innerHTML = `
    <button class="btn btn-secondary" id="prevPage" ${state.offset === 0 ? 'disabled' : ''}>← Previous</button>
    <span>${start}–${end} of ${state.total.toLocaleString()}</span>
    <button class="btn btn-secondary" id="nextPage" ${end >= state.total ? 'disabled' : ''}>Next →</button>
  `;
  const prev = document.getElementById('prevPage');
  const next = document.getElementById('nextPage');
  if (prev) prev.addEventListener('click', () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); loadProducts(false); });
  if (next) next.addEventListener('click', () => { state.offset += PAGE_SIZE; loadProducts(false); });
}

function renderTable() {
  const body = el('productsBody');
  if (state.items.length === 0) {
    const gate = state.recency
      ? ` Products with no out-of-stock hits since ${state.recency.cutoff} (last ${state.recency.days} day${state.recency.days === 1 ? '' : 's'}) are excluded.`
      : '';
    body.innerHTML = `<tr><td colspan="7" class="empty-state">No products match these filters.${escapeHtml(gate)}</td></tr>`;
    return;
  }
  body.innerHTML = state.items.map(item => `
    <tr data-code="${escapeAttr(item.product_code)}">
      <td class="pc-code">${escapeHtml(item.product_code)}</td>
      <td>${escapeHtml(item.product_name || '—')}</td>
      <td>${item.partner_code ? `<span class="partner-badge">${escapeHtml(item.partner_code)}</span>` : '—'}</td>
      <td>${item.category ? `<span class="category-badge">${escapeHtml(item.category)}</span>` : '—'}</td>
      <td class="num">${item.oos_views.toLocaleString()}</td>
      <td class="num">${item.days_oos}</td>
      <td class="dates-cell">${item.first_oos_date || '—'} → ${item.last_oos_date || '—'}</td>
    </tr>
  `).join('');

  body.querySelectorAll('tr[data-code]').forEach(row => {
    row.addEventListener('click', () => openTrend(row.dataset.code));
  });
}

function renderSortIndicators() {
  document.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === state.sort) {
      th.classList.add(state.dir === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  });
}

document.querySelectorAll('th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (state.sort === key) {
      state.dir = state.dir === 'asc' ? 'desc' : 'asc';
    } else {
      state.sort = key;
      state.dir = 'desc';
    }
    renderSortIndicators();
    loadProducts();
  });
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

el('exportBtn').addEventListener('click', () => {
  const params = buildFilterParams();
  window.location.href = `/api/export?${params.toString()}`;
});

(async function init() {
  defaultDates();
  renderSortIndicators();
  await loadFilters();
  await loadStatus();
  await loadProducts();
})();
