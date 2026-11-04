// Revenue loss tab: two uploaded monthly sales extracts (Product Code, Product
// Name, Vendor, Product Qty, Total Selling price) joined server-side to the
// GA4 out-of-stock data. Files are parsed here in the browser and posted as
// JSON rows to /api/revenue-loss; nothing is stored. app.js owns the other two
// tabs and ignores clicks on this one (its VIEWS map has no 'revenue'), so
// this file handles all tab clicks only to show/hide the right panel.
(function () {
  const $ = (id) => document.getElementById(id);
  const PAGE = 100;
  const SHEETJS = 'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js';

  const files = { 1: null, 2: null };   // { name, map: Map<code,{name,vendor,qty,rev}>, total, hasRev, ... }
  const st = {
    data: null, vendor: '', q: '',
    sort: 'lost', dir: 'desc', offset: 0,
    vSort: 'lost', vDir: 'desc',
  };

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n) => Math.round(n).toLocaleString();
  const pct = (a, b) => (b > 0 ? (a / b * 100).toFixed(1) + '%' : '—');

  // ───────────── tab switching
  function showRevenue(on) {
    ['oosTop', 'oosMain'].forEach((id) => { $(id).hidden = on; });
    $('revPanel').hidden = !on;
    $('tabRevenue').classList.toggle('active', on);
    $('tabRevenue').setAttribute('aria-selected', String(on));
    if (on) {
      ['tabActive', 'tabRecovered'].forEach((id) => {
        $(id).classList.remove('active');
        $(id).setAttribute('aria-selected', 'false');
      });
    }
  }
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      const isRev = tab.dataset.view === 'revenue';
      showRevenue(isRev);
      if (!isRev) {
        // app.js's switchView returns early when the clicked tab is already
        // its current view (coming back from Revenue), so restore the
        // highlight here.
        document.querySelectorAll('.tab').forEach((t) => {
          const on = t === tab;
          t.classList.toggle('active', on);
          t.setAttribute('aria-selected', String(on));
        });
      }
    });
  });

  // ───────────── parsing
  function parseCSV(text) {
    text = text.replace(/^﻿/, '');
    const first = text.split(/\r?\n/, 1)[0] || '';
    const delim = [',', ';', '\t'].map((d) => [d, first.split(d).length]).sort((a, b) => b[1] - a[1])[0][0];
    const rows = []; let row = []; let cell = ''; let q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) {
        if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; }
        else cell += c;
      } else if (c === '"') q = true;
      else if (c === delim) { row.push(cell); cell = ''; }
      else if (c === '\n' || c === '\r') {
        if (c === '\r' && text[i + 1] === '\n') i++;
        row.push(cell); rows.push(row); row = []; cell = '';
      } else cell += c;
    }
    if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
    return rows.filter((r) => r.some((x) => String(x).trim() !== ''));
  }

  function loadSheetJS() {
    if (window.XLSX) return Promise.resolve();
    return new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = SHEETJS; s.onload = res;
      s.onerror = () => rej(new Error('Could not load the Excel reader. Save the file as CSV and retry.'));
      document.head.appendChild(s);
    });
  }

  async function readRows(file) {
    if (/\.(xlsx|xls)$/i.test(file.name)) {
      await loadSheetJS();
      const wb = XLSX.read(await file.arrayBuffer(), { type: 'array' });
      return XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]], { header: 1, raw: true, defval: '' });
    }
    return parseCSV(await file.text());
  }

  const toNum = (v) => {
    if (typeof v === 'number') return v;
    const n = parseFloat(String(v ?? '').replace(/[, ]/g, ''));
    return Number.isFinite(n) ? n : NaN;
  };

  // Columns are found by header name, each column taking one role only (so
  // "Total Selling price" is revenue, not a second "name"). Roles are tried in
  // this order. With no recognisable header the layout is positional:
  // code, name, vendor, qty, revenue.
  const ROLES = [
    ['rev', /selling|revenue|sales|amount|value|total.*price/],
    ['qty', /qty|quant|units/],
    ['vendor', /vendor|supplier|partner|seller|brand/],
    ['name', /name|title|description/],
    ['code', /code|sku|item|id$/],
  ];
  function interpret(rows) {
    if (!rows.length) throw new Error('The file is empty.');
    const head = rows[0].map((h) => String(h ?? '').trim().toLowerCase());
    const col = {};
    head.forEach((h, i) => {
      const hit = ROLES.find(([role, re]) => !(role in col) && re.test(h));
      if (hit) col[hit[0]] = i;
    });
    let hasHeader = 'code' in col && 'qty' in col;
    if (!hasHeader) {
      if (rows[0].length < 5) throw new Error('Could not find the columns. Expected: Product Code, Product Name, Vendor, Product Qty, Total Selling price.');
      Object.assign(col, { code: 0, name: 1, vendor: 2, qty: 3, rev: 4 });
      hasHeader = Number.isNaN(toNum(rows[0][3]));   // a numeric qty in row 1 means there is no header row
    }
    const map = new Map(); let total = 0; let revTotal = 0; let bad = 0;
    for (const r of rows.slice(hasHeader ? 1 : 0)) {
      const code = String(r[col.code] ?? '').trim();
      if (!code) continue;
      const qty = toNum(r[col.qty]);
      if (Number.isNaN(qty)) { bad++; continue; }
      const rev = 'rev' in col ? toNum(r[col.rev]) : NaN;
      const e = map.get(code) || { name: '', vendor: '', qty: 0, rev: null };
      e.qty += qty; total += qty;
      if (!Number.isNaN(rev)) { e.rev = (e.rev || 0) + rev; revTotal += rev; }
      if (!e.name && 'name' in col) e.name = String(r[col.name] ?? '').trim();
      if (!e.vendor && 'vendor' in col) e.vendor = String(r[col.vendor] ?? '').trim();
      map.set(code, e);
    }
    if (!map.size) throw new Error('No usable rows found.');
    return {
      map, total, revTotal, bad, hasRev: 'rev' in col, hasVendor: 'vendor' in col,
      cols: hasHeader ? ['code', 'name', 'vendor', 'qty', 'rev'].filter((k) => k in col).map((k) => String(rows[0][col[k]])) : null,
    };
  }

  async function takeFile(slot, file) {
    const info = $('info' + slot);
    try {
      const parsed = interpret(await readRows(file));
      files[slot] = { name: file.name, ...parsed };
      info.hidden = false; info.classList.remove('err');
      const tags = [
        parsed.hasRev ? '' : '<span class="rev-tag warn">no revenue column</span>',
        parsed.hasVendor ? '' : '<span class="rev-tag warn">no vendor column</span>',
        parsed.bad ? `<span class="rev-tag warn">${parsed.bad} rows skipped</span>` : '',
      ].join('');
      info.innerHTML = `<div class="rev-file-name">✔ ${esc(file.name)} <button class="btn-link" data-clear="${slot}" type="button">remove</button></div>
        <div class="rev-file-stats"><span><strong>${parsed.map.size.toLocaleString()}</strong> products</span><span><strong>${fmt(parsed.total)}</strong> units</span>${parsed.hasRev ? `<span><strong>${fmt(parsed.revTotal)}</strong> revenue</span>` : ''}${tags}</div>
        <div class="rev-file-cols">${parsed.cols ? 'Columns read: ' + parsed.cols.map((c) => `<em>${esc(c)}</em>`).join(', ') : 'No header row — read as code, name, vendor, qty, revenue'}</div>`;
      $('drop' + slot).classList.add('has-file');
    } catch (e) {
      files[slot] = null;
      info.hidden = false; info.classList.add('err');
      info.innerHTML = `⚠ ${esc(e.message)}`;
      $('drop' + slot).classList.remove('has-file');
    }
    updateRunState();
  }

  [1, 2].forEach((slot) => {
    const input = $('file' + slot), drop = $('drop' + slot);
    input.addEventListener('change', () => { if (input.files[0]) takeFile(slot, input.files[0]); input.value = ''; });
    ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', (e) => { const f = e.dataTransfer.files[0]; if (f) takeFile(slot, f); });
    $('info' + slot).addEventListener('click', (e) => {
      if (e.target.dataset.clear) { files[slot] = null; $('info' + slot).hidden = true; drop.classList.remove('has-file'); updateRunState(); }
    });
  });

  // ───────────── template download
  const TEMPLATE = [
    ['Product Code', 'Product Name', 'Vendor', 'Product Qty', 'Total Selling price'],
    ['grocery001737', 'Basmati Rice 5kg', 'Sample Vendor A', 120, 54000],
    ['pharmacy00844', 'Vitamin C 500mg', 'Sample Vendor B', 85, 102000],
    ['ef_pc_home0v2057pod00141p', 'Ceramic Vase', 'Sample Vendor C', 30, 105000],
  ];
  function download(blob, name) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name;
    a.click(); URL.revokeObjectURL(a.href);
  }
  $('tplCsv').addEventListener('click', () => {
    const q = (v) => `"${String(v).replace(/"/g, '""')}"`;
    const csv = TEMPLATE.map((r) => r.map(q).join(',')).join('\r\n');
    download(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' }), 'revenue-loss-template.csv');
  });
  $('tplXlsx').addEventListener('click', async () => {
    try {
      await loadSheetJS();
      const ws = XLSX.utils.aoa_to_sheet(TEMPLATE);
      ws['!cols'] = [{ wch: 28 }, { wch: 28 }, { wch: 22 }, { wch: 12 }, { wch: 20 }];
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, 'Sales');
      XLSX.writeFile(wb, 'revenue-loss-template.xlsx');
    } catch (e) { alert(e.message); }
  });

  // ───────────── controls
  // Default OOS range: last calendar month (the analysis is run monthly).
  (function defaultRange() {
    const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const now = new Date();
    $('revStart').value = iso(new Date(now.getFullYear(), now.getMonth() - 1, 1));
    $('revEnd').value = iso(new Date(now.getFullYear(), now.getMonth(), 0));
  })();

  function updateRunState() {
    $('revRun').disabled = !(files[1] && files[2] && $('revStart').value && $('revEnd').value);
  }
  ['revStart', 'revEnd'].forEach((id) => $(id).addEventListener('input', updateRunState));

  $('revReset').addEventListener('click', () => {
    files[1] = files[2] = null; st.data = null;
    [1, 2].forEach((s) => { $('info' + s).hidden = true; $('drop' + s).classList.remove('has-file'); });
    $('revResults').hidden = true; $('revNotice').hidden = true;
    updateRunState();
  });

  $('revRun').addEventListener('click', run);

  async function run() {
    const rows = [];
    const codes = new Set([...files[1].map.keys(), ...files[2].map.keys()]);
    for (const code of codes) {
      const a = files[1].map.get(code), b = files[2].map.get(code);
      const revKnown = (a && a.rev != null) || (b && b.rev != null);
      rows.push({
        code, name: (a && a.name) || (b && b.name) || null, vendor: (a && a.vendor) || (b && b.vendor) || null,
        m1: a ? a.qty : 0, m2: b ? b.qty : 0,
        r1: revKnown ? (a && a.rev) || 0 : null, r2: revKnown ? (b && b.rev) || 0 : null,
      });
    }
    const btn = $('revRun');
    btn.disabled = true; btn.textContent = 'Analysing…';
    try {
      const res = await fetch('api/revenue-loss', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rows, oos_start: $('revStart').value, oos_end: $('revEnd').value,
          rank_by: $('revRank').value, top_n: Math.max(0, parseInt($('revTopN').value, 10) || 0),
          min_oos_days: Number($('revMinDays').value) || 1,
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      st.data = await res.json();
      st.vendor = ''; st.q = ''; st.offset = 0; st.sort = 'lost'; st.dir = 'desc';
      $('revSearch').value = '';
      renderAll();
    } catch (e) {
      const n = $('revNotice'); n.hidden = false; n.className = 'recency-note recency-stale';
      n.textContent = 'Analysis failed: ' + e.message;
    } finally {
      btn.textContent = 'Analyse revenue loss'; updateRunState();
    }
  }

  // ───────────── render
  function vendors() {
    const m = new Map();
    for (const i of st.data.items) {
      const v = m.get(i.vendor) || { vendor: i.vendor, products: 0, oos_days: 0, lost: 0 };
      v.products++; v.oos_days += i.oos_days; v.lost += i.lost;
      m.set(i.vendor, v);
    }
    return [...m.values()];
  }

  function renderAll() {
    const d = st.data, t = d.totals;
    $('revResults').hidden = false;

    const n = $('revNotice'); n.hidden = false;
    const covered = d.window.covered_days, days = d.window.days;
    if (!d.window.data_from || covered === 0) {
      n.className = 'recency-note recency-stale';
      n.innerHTML = `⚠️ No GA4 out-of-stock data is synced for <strong>${d.window.start} → ${d.window.end}</strong>, so no product can be matched as out of stock. Backfill that period (README: <code>POST /api/refresh?start=…&amp;end=…</code>) and run again.`;
    } else if (covered < days) {
      n.className = 'recency-note recency-stale';
      n.innerHTML = `⚠️ Synced GA4 data covers only <strong>${covered} of ${days} days</strong> of the selected range (data ${d.window.data_from} → ${d.window.data_to}). Products out of stock on the missing days are not flagged, so the loss is understated.`;
    } else {
      n.className = 'recency-note';
      n.innerHTML = `📅 OOS counted over <strong>${d.window.start} → ${d.window.end}</strong> (${days} day${days === 1 ? '' : 's'}, fully covered by synced data). Checked ${d.params.top_n ? 'the top ' + d.params.top_n : 'all'} Month 1 products by <strong>${d.params.rank_by === 'top_selling' ? 'sales' : 'drop %'}</strong> for ≥ <strong>${d.params.min_oos_days}</strong> OOS day${d.params.min_oos_days === 1 ? '' : 's'}.${d.has_revenue ? '' : ' <strong>No revenue column found — loss is shown in units.</strong>'}`;
    }

    const kpi = (label, value, sub, cls = '') => `<div class="rev-kpi ${cls}"><div class="rev-kpi-label">${label}</div><div class="rev-kpi-value">${value}</div><div class="rev-kpi-sub">${sub}</div></div>`;
    $('revKpis').innerHTML =
      kpi('Month 1 products checked', fmt(t.ranked), `of ${fmt(t.uploaded)} uploaded · ${d.params.rank_by === 'top_selling' ? 'top sellers' : 'biggest drops'}`) +
      kpi('Went out of stock', fmt(t.went_oos), `${pct(t.went_oos, t.ranked)} of those checked`, 'accent') +
      kpi('With lost sales', fmt(t.with_loss), 'OOS and sold less than Month 1', 'accent') +
      kpi(d.has_revenue ? 'Revenue lost' : 'Units lost', fmt(t.lost), `${pct(t.lost, t.ranked_base1)} of their Month 1 ${d.has_revenue ? 'revenue' : 'units'}`, 'danger');

    renderVendorChart(); renderVendorTable(); renderList();
  }

  function renderVendorChart() {
    const vs = vendors().sort((a, b) => b.lost - a.lost).slice(0, 12);
    const max = Math.max(1, ...vs.map((v) => v.lost));
    $('revVendorChart').innerHTML = vs.length ? vs.map((v) => `
      <div class="rev-bar-row" data-vendor="${esc(v.vendor)}" title="${esc(v.vendor)}: ${fmt(v.lost)} lost across ${v.products} product${v.products === 1 ? '' : 's'}">
        <span class="rev-bar-label">${esc(v.vendor)}</span>
        <span class="rev-bar-track"><i class="seg-oos" style="width:${v.lost / max * 100}%"></i></span>
        <span class="rev-bar-val">${fmt(v.lost)}</span>
      </div>`).join('') : '<div class="empty-state">No out-of-stock products with lost sales found.</div>';
    $('revVendorChart').querySelectorAll('.rev-bar-row').forEach((r) => r.addEventListener('click', () => setVendor(r.dataset.vendor)));
  }

  function renderVendorTable() {
    const k = st.vSort, dir = st.vDir === 'asc' ? 1 : -1;
    const vs = vendors().sort((a, b) => {
      const x = a[k], y = b[k];
      return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
    });
    $('revVendorBody').innerHTML = vs.map((v) => `
      <tr data-vendor="${esc(v.vendor)}" class="${st.vendor === v.vendor ? 'sel' : ''}">
        <td title="${esc(v.vendor)}">${esc(v.vendor)}</td>
        <td class="num">${fmt(v.products)}</td>
        <td class="num">${fmt(v.oos_days)}</td>
        <td class="num days-oos-strong">${fmt(v.lost)}</td>
      </tr>`).join('') || '<tr><td colspan="4" class="empty-state">Nothing to show.</td></tr>';
    $('revVendorBody').querySelectorAll('tr[data-vendor]').forEach((r) => r.addEventListener('click', () => setVendor(r.dataset.vendor)));
    sortMarks('revVendorTable', st.vSort, st.vDir);
  }

  function setVendor(v) {
    st.vendor = st.vendor === v ? '' : v; st.offset = 0;
    renderVendorTable(); renderList();
    if (st.vendor) $('revSearch').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function filtered() {
    const q = st.q.toLowerCase();
    return st.data.items.filter((i) =>
      (!st.vendor || i.vendor === st.vendor) &&
      (!q || i.product_code.toLowerCase().includes(q) || (i.product_name || '').toLowerCase().includes(q) || i.vendor.toLowerCase().includes(q)));
  }

  function sorted(list) {
    const k = st.sort, dir = st.dir === 'asc' ? 1 : -1;
    return list.sort((a, b) => {
      const x = a[k] ?? '', y = b[k] ?? '';
      return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
    });
  }

  function renderList() {
    const chip = $('revVendorChip');
    chip.hidden = !st.vendor;
    chip.innerHTML = st.vendor ? `Vendor: <strong>${esc(st.vendor)}</strong> ✕` : '';

    const list = sorted(filtered());
    const page = list.slice(st.offset, st.offset + PAGE);
    $('revBody').innerHTML = page.length ? page.map((i) => `
      <tr>
        <td class="pc-code">${esc(i.product_code)}</td>
        <td title="${esc(i.product_name || '')}">${esc(i.product_name || '—')}</td>
        <td title="${esc(i.vendor)}">${esc(i.vendor)}</td>
        <td class="num"><span class="oos-days-badge">${i.oos_days}d</span></td>
        <td class="num loss">${fmt(i.lost)}</td>
      </tr>`).join('')
      : '<tr><td colspan="5" class="empty-state">No products went out of stock with lost sales for these settings.</td></tr>';
    sortMarks('revTable', st.sort, st.dir);

    const total = list.length, end = Math.min(st.offset + PAGE, total);
    const pg = $('revPagination');
    pg.innerHTML = total > PAGE ? `<button class="btn btn-secondary" id="rvPrev" ${st.offset === 0 ? 'disabled' : ''}>← Previous</button><span>${st.offset + 1}–${end} of ${total.toLocaleString()}</span><button class="btn btn-secondary" id="rvNext" ${end >= total ? 'disabled' : ''}>Next →</button>` : '';
    if (total > PAGE) {
      $('rvPrev').onclick = () => { st.offset = Math.max(0, st.offset - PAGE); renderList(); };
      $('rvNext').onclick = () => { st.offset += PAGE; renderList(); };
    }
  }

  function sortMarks(tableId, key, dir) {
    $(tableId).querySelectorAll('th.sortable').forEach((th) => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.sort === key) th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
    });
  }

  $('revTable').querySelectorAll('th.sortable').forEach((th) => th.addEventListener('click', () => {
    const k = th.dataset.sort;
    if (st.sort === k) st.dir = st.dir === 'asc' ? 'desc' : 'asc'; else { st.sort = k; st.dir = 'desc'; }
    st.offset = 0; renderList();
  }));
  $('revVendorTable').querySelectorAll('th.sortable').forEach((th) => th.addEventListener('click', () => {
    const k = th.dataset.sort;
    if (st.vSort === k) st.vDir = st.vDir === 'asc' ? 'desc' : 'asc'; else { st.vSort = k; st.vDir = 'desc'; }
    renderVendorTable();
  }));
  $('revVendorChip').addEventListener('click', () => setVendor(st.vendor));
  let deb; $('revSearch').addEventListener('input', (e) => { clearTimeout(deb); deb = setTimeout(() => { st.q = e.target.value.trim(); st.offset = 0; renderList(); }, 250); });

  // Client-side CSV of exactly the rows the current filters select. Carries a
  // few extra columns beyond the on-screen five so the maths can be checked.
  $('revExport').addEventListener('click', () => {
    if (!st.data) return;
    const hasRev = st.data.has_revenue;
    const head = ['Product Code', 'Product Name', 'Vendor', 'Days OOS', hasRev ? 'Revenue Lost' : 'Units Lost',
      'Month 1 Qty', 'Month 2 Qty', ...(hasRev ? ['Month 1 Revenue', 'Month 2 Revenue'] : []), 'Drop %', 'OOS Views', 'First OOS', 'Last OOS'];
    const q = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
    const lines = [head.map(q).join(',')];
    sorted(filtered()).forEach((i) => {
      lines.push([i.product_code, i.product_name, i.vendor, i.oos_days, i.lost, i.m1, i.m2,
        ...(hasRev ? [i.r1, i.r2] : []), i.drop_pct, i.oos_views, i.first_oos, i.last_oos].map(q).join(','));
    });
    download(new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }),
      `kapruka-revenue-loss-${st.data.window.start}_${st.data.window.end}.csv`);
  });
})();
