// Revenue loss tab: two uploaded monthly sales extracts (product code, total
// quantity[, unit price]) joined server-side to the GA4 out-of-stock data for
// month 2. Files are parsed here in the browser and posted as JSON rows to
// /api/revenue-loss; nothing is stored. app.js owns the other two tabs and
// ignores clicks on this one (its VIEWS map has no 'revenue'), so this file
// handles all tab clicks only to show/hide the right panel.
(function () {
  const $ = (id) => document.getElementById(id);
  const PAGE = 100;
  const SHEETJS = 'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js';

  const files = { 1: null, 2: null };   // { name, rows: Map<code,{qty,price}>, rawRows, hasPrice, total }
  const st = {
    data: null, mode: 'linked', category: '', q: '',
    sort: 'revenue_lost', dir: 'desc', offset: 0,
    catSort: 'units_lost_oos', catDir: 'desc',
  };

  // ───────────── tab switching
  const MAIN = ['oosTop', 'oosMain'];
  function showRevenue(on) {
    MAIN.forEach((id) => { $(id).hidden = on; });
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

  // Header names win; with no header row it is positionally code, qty, price.
  function interpret(rows) {
    if (!rows.length) throw new Error('The file is empty.');
    const head = rows[0].map((h) => String(h ?? '').trim().toLowerCase());
    const hasHeader = Number.isNaN(toNum(rows[0][1]));
    let ci = 0, qi = 1, pi = -1;
    if (hasHeader) {
      const find = (re) => head.findIndex((h) => re.test(h));
      const c = find(/code|sku|item|product(?!.*name)/);
      const q = find(/qty|quant|units|sold|total/);
      const p = find(/price|rate|amount|value/);
      if (c >= 0) ci = c;
      if (q >= 0 && q !== ci) qi = q; else qi = ci === 0 ? 1 : 0;
      if (p >= 0 && p !== ci && p !== qi) pi = p;
    } else if (rows[0].length > 2) pi = 2;
    const map = new Map(); let total = 0; let bad = 0; let priced = 0;
    for (const r of rows.slice(hasHeader ? 1 : 0)) {
      const code = String(r[ci] ?? '').trim();
      const qty = toNum(r[qi]);
      if (!code) continue;
      if (Number.isNaN(qty)) { bad++; continue; }
      const price = pi >= 0 ? toNum(r[pi]) : NaN;
      const e = map.get(code) || { qty: 0, price: null };
      e.qty += qty; total += qty;
      if (price > 0) { e.price = price; priced++; }
      map.set(code, e);
    }
    if (!map.size) throw new Error('No usable rows. Expected columns: Product code, Quantity.');
    return { map, total, bad, hasPrice: priced > 0, header: hasHeader ? { code: head[ci], qty: head[qi], price: pi >= 0 ? head[pi] : null } : null };
  }

  async function takeFile(slot, file) {
    const info = $('info' + slot);
    try {
      const parsed = interpret(await readRows(file));
      files[slot] = { name: file.name, ...parsed };
      info.hidden = false; info.classList.remove('err');
      const cols = parsed.header ? `Using <em>${esc(parsed.header.code)}</em> + <em>${esc(parsed.header.qty)}</em>${parsed.header.price ? ` + <em>${esc(parsed.header.price)}</em>` : ''}` : 'No header row — read as code, quantity';
      info.innerHTML = `<div class="rev-file-name">✔ ${esc(file.name)} <button class="btn-link" data-clear="${slot}" type="button">remove</button></div>
        <div class="rev-file-stats"><span><strong>${parsed.map.size.toLocaleString()}</strong> products</span><span><strong>${fmt(parsed.total)}</strong> units</span>${parsed.hasPrice ? '<span class="rev-tag">has price</span>' : ''}${parsed.bad ? `<span class="rev-tag warn">${parsed.bad} rows skipped</span>` : ''}</div>
        <div class="rev-file-cols">${cols}</div>`;
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
    ['Product Code', 'Quantity', 'Unit Price'],
    ['grocery001737', 120, 450],
    ['pharmacy00844', 85, 1200],
    ['ef_pc_home0v2057pod00141p', 30, 3500],
  ];
  function download(blob, name) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name;
    a.click(); URL.revokeObjectURL(a.href);
  }
  $('tplCsv').addEventListener('click', () => {
    const csv = TEMPLATE.map((r) => r.join(',')).join('\r\n');
    download(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' }), 'revenue-loss-template.csv');
  });
  $('tplXlsx').addEventListener('click', async () => {
    try {
      await loadSheetJS();
      const ws = XLSX.utils.aoa_to_sheet(TEMPLATE);
      ws['!cols'] = [{ wch: 30 }, { wch: 12 }, { wch: 12 }];
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, 'Sales');
      XLSX.writeFile(wb, 'revenue-loss-template.xlsx');
    } catch (e) { alert(e.message); }
  });

  // ───────────── controls
  (function defaultMonth() {
    const d = new Date(); d.setDate(1); d.setMonth(d.getMonth() - 1);
    $('revMonth2').value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  })();

  function updateRunState() {
    $('revRun').disabled = !(files[1] && files[2] && $('revMonth2').value);
  }
  $('revMonth2').addEventListener('input', updateRunState);

  $('revReset').addEventListener('click', () => {
    files[1] = files[2] = null; st.data = null;
    [1, 2].forEach((s) => { $('info' + s).hidden = true; $('drop' + s).classList.remove('has-file'); });
    $('revResults').hidden = true; $('revNotice').hidden = true;
    updateRunState();
  });

  $('revRun').addEventListener('click', run);

  async function run() {
    const [y, m] = $('revMonth2').value.split('-').map(Number);
    const last = new Date(y, m, 0).getDate();
    const p2 = (n) => String(n).padStart(2, '0');
    const rows = [];
    const codes = new Set([...files[1].map.keys(), ...files[2].map.keys()]);
    for (const code of codes) {
      const a = files[1].map.get(code), b = files[2].map.get(code);
      rows.push({ code, m1: a ? a.qty : 0, m2: b ? b.qty : 0, price: (b && b.price) || (a && a.price) || null });
    }
    const btn = $('revRun');
    btn.disabled = true; btn.textContent = 'Analysing…';
    try {
      const res = await fetch('api/revenue-loss', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rows, oos_start: `${y}-${p2(m)}-01`, oos_end: `${y}-${p2(m)}-${p2(last)}`,
          min_drop_pct: Number($('revMinDrop').value) || 0,
          min_oos_days: Number($('revMinDays').value) || 1,
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      st.data = await res.json();
      st.mode = 'linked'; st.category = ''; st.q = ''; st.offset = 0;
      st.sort = st.data.totals.has_price ? 'revenue_lost' : 'drop'; st.dir = 'desc';
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
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n) => Math.round(n).toLocaleString();
  const money = (n) => (n == null ? '—' : Math.round(n).toLocaleString());
  const pct = (a, b) => (b > 0 ? (a / b * 100).toFixed(1) + '%' : '—');

  function renderAll() {
    const d = st.data, t = d.totals;
    $('revResults').hidden = false;
    $('revPanel').classList.toggle('rev-no-price', !t.has_price);

    const n = $('revNotice'); n.hidden = false;
    const covered = d.window.covered_days, days = d.window.days;
    if (!d.window.data_from || covered === 0) {
      n.className = 'recency-note recency-stale';
      n.innerHTML = `⚠️ No GA4 out-of-stock data is synced for <strong>${d.window.start} → ${d.window.end}</strong>, so nothing can be marked OOS-linked. Backfill that month (see README: <code>POST /api/refresh?start=…&amp;end=…</code>) and run again.`;
    } else if (covered < days) {
      n.className = 'recency-note recency-stale';
      n.innerHTML = `⚠️ Synced GA4 data covers only <strong>${covered} of ${days} days</strong> of the month (data ${d.window.data_from} → ${d.window.data_to}). Products that were out of stock on the missing days are not flagged, so OOS-linked loss is understated.`;
    } else {
      n.className = 'recency-note';
      n.innerHTML = `📅 OOS evidence counted over <strong>${d.window.start} → ${d.window.end}</strong> (${days} days, fully covered by synced data). Showing products that dropped ≥ <strong>${d.params.min_drop_pct}%</strong> and had out-of-stock views on ≥ <strong>${d.params.min_oos_days}</strong> day${d.params.min_oos_days === 1 ? '' : 's'}.`;
    }

    const totalDrop = t.units_lost_oos + t.units_lost_other;
    const kpi = (label, value, sub, cls = '') => `<div class="rev-kpi ${cls}"><div class="rev-kpi-label">${label}</div><div class="rev-kpi-value">${value}</div><div class="rev-kpi-sub">${sub}</div></div>`;
    $('revKpis').innerHTML =
      kpi('Products compared', fmt(t.products), `${fmt(t.units_m1)} → ${fmt(t.units_m2)} units (${pct(t.units_m2 - t.units_m1, t.units_m1).replace(/^(?!-)/, '+')})`) +
      kpi('Products with a drop', fmt(t.dropped), `${pct(t.dropped, t.products)} of all compared`) +
      kpi('Dropped & went OOS', fmt(t.oos_linked), `${pct(t.oos_linked, t.dropped)} of dropped products`, 'accent') +
      kpi('Units lost to OOS', fmt(t.units_lost_oos), `${pct(t.units_lost_oos, totalDrop)} of the total drop`, 'accent') +
      (t.has_price
        ? kpi('Revenue lost to OOS', money(t.revenue_lost_oos), `vs ${money(t.revenue_lost_other)} from other drops`, 'danger')
        : kpi('Revenue lost', 'n/a', 'add a unit price column to either file', 'muted'));

    renderCatChart(); renderCatTable(); renderList();
  }

  function renderCatChart() {
    const cats = st.data.categories.filter((c) => c.units_lost_oos > 0 || c.units_lost_other > 0)
      .sort((a, b) => (b.units_lost_oos + b.units_lost_other) - (a.units_lost_oos + a.units_lost_other)).slice(0, 12);
    const max = Math.max(1, ...cats.map((c) => c.units_lost_oos + c.units_lost_other));
    $('revCatChart').innerHTML = cats.length ? cats.map((c) => `
      <div class="rev-bar-row" data-cat="${esc(c.category)}" title="${esc(c.category)}: ${fmt(c.units_lost_oos)} units lost to OOS, ${fmt(c.units_lost_other)} other drops">
        <span class="rev-bar-label">${esc(c.category)}</span>
        <span class="rev-bar-track"><i class="seg-oos" style="width:${c.units_lost_oos / max * 100}%"></i><i class="seg-other" style="width:${c.units_lost_other / max * 100}%"></i></span>
        <span class="rev-bar-val">${fmt(c.units_lost_oos)}</span>
      </div>`).join('') + `<div class="rev-legend"><span><i class="seg-oos"></i> Lost on OOS products</span><span><i class="seg-other"></i> Other drops</span></div>`
      : '<div class="empty-state">No quantity drops found.</div>';
    $('revCatChart').querySelectorAll('.rev-bar-row').forEach((r) => r.addEventListener('click', () => setCategory(r.dataset.cat)));
  }

  function renderCatTable() {
    const k = st.catSort, dir = st.catDir === 'asc' ? 1 : -1;
    const val = (c) => (k === 'pct' ? (c.units_m1 > 0 ? c.units_lost_oos / c.units_m1 : 0) : c[k]);
    const cats = [...st.data.categories].sort((a, b) => {
      const x = val(a), y = val(b);
      return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
    });
    $('revCatBody').innerHTML = cats.map((c) => `
      <tr data-cat="${esc(c.category)}" class="${st.category === c.category ? 'sel' : ''}">
        <td><span class="category-badge">${esc(c.category)}</span></td>
        <td class="num">${fmt(c.products)}</td><td class="num">${fmt(c.dropped)}</td>
        <td class="num days-oos-strong">${fmt(c.oos_linked)}</td>
        <td class="num">${fmt(c.units_lost_oos)}</td>
        <td class="num">${pct(c.units_lost_oos, c.units_m1)}</td>
        <td class="num rev-col-rev">${money(c.revenue_lost_oos)}</td>
      </tr>`).join('');
    $('revCatBody').querySelectorAll('tr').forEach((r) => r.addEventListener('click', () => setCategory(r.dataset.cat)));
    sortMarks('revCatTable', st.catSort, st.catDir);
  }

  function setCategory(cat) {
    st.category = st.category === cat ? '' : cat; st.offset = 0;
    renderCatTable(); renderList();
    if (st.category) $('revSeg').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function filtered() {
    const q = st.q.toLowerCase();
    return st.data.items.filter((i) =>
      (st.mode === 'linked' ? i.oos_linked : !i.oos_linked) &&
      (!st.category || i.category === st.category) &&
      (!q || i.product_code.toLowerCase().includes(q) || (i.product_name || '').toLowerCase().includes(q)));
  }

  function sorted(list) {
    const k = st.sort, dir = st.dir === 'asc' ? 1 : -1;
    return list.sort((a, b) => {
      const x = a[k] ?? -Infinity, y = b[k] ?? -Infinity;
      return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
    });
  }

  function renderList() {
    const items = st.data.items;
    $('segLinked').textContent = fmt(items.filter((i) => i.oos_linked && (!st.category || i.category === st.category)).length);
    $('segOther').textContent = fmt(items.filter((i) => !i.oos_linked && (!st.category || i.category === st.category)).length);
    document.querySelectorAll('#revSeg button').forEach((b) => b.classList.toggle('active', b.dataset.mode === st.mode));
    const chip = $('revCatChip');
    chip.hidden = !st.category;
    chip.innerHTML = st.category ? `Category: <strong>${esc(st.category)}</strong> ✕` : '';

    const list = sorted(filtered());
    const page = list.slice(st.offset, st.offset + PAGE);
    $('revBody').innerHTML = page.length ? page.map((i) => `
      <tr>
        <td class="pc-code">${esc(i.product_code)}</td>
        <td>${esc(i.product_name || '—')}</td>
        <td><span class="category-badge">${esc(i.category)}</span></td>
        <td class="num">${fmt(i.m1)}</td><td class="num">${fmt(i.m2)}</td>
        <td class="num loss">−${fmt(i.drop)}</td>
        <td class="num">${i.drop_pct}%</td>
        <td class="num">${i.oos_days ? `<span class="oos-days-badge">${i.oos_days}d</span>` : '<span class="muted-note">0</span>'}</td>
        <td class="num">${fmt(i.oos_views)}</td>
        <td class="num rev-col-rev">${money(i.revenue_lost)}</td>
      </tr>`).join('')
      : `<tr><td colspan="10" class="empty-state">${st.mode === 'linked' ? 'No dropped products had out-of-stock views in month 2 with these settings.' : 'No other drops.'}</td></tr>`;
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
  $('revCatTable').querySelectorAll('th.sortable').forEach((th) => th.addEventListener('click', () => {
    const k = th.dataset.sort;
    if (st.catSort === k) st.catDir = st.catDir === 'asc' ? 'desc' : 'asc'; else { st.catSort = k; st.catDir = 'desc'; }
    renderCatTable();
  }));
  document.querySelectorAll('#revSeg button').forEach((b) => b.addEventListener('click', () => { st.mode = b.dataset.mode; st.offset = 0; renderList(); }));
  $('revCatChip').addEventListener('click', () => setCategory(st.category));
  let deb; $('revSearch').addEventListener('input', (e) => { clearTimeout(deb); deb = setTimeout(() => { st.q = e.target.value.trim(); st.offset = 0; renderList(); }, 250); });

  // Client-side CSV of exactly the rows the current filters select.
  $('revExport').addEventListener('click', () => {
    if (!st.data) return;
    const hasPrice = st.data.totals.has_price;
    const head = ['Product Code', 'Product Name', 'Category', 'Month 1 Qty', 'Month 2 Qty', 'Qty Drop', 'Drop %', 'OOS Days (month 2)', 'OOS Views (month 2)', 'First OOS', 'Last OOS', 'OOS-linked'];
    if (hasPrice) head.push('Unit Price', 'Revenue Lost');
    const q = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
    const lines = [head.map(q).join(',')];
    sorted(filtered()).forEach((i) => {
      const r = [i.product_code, i.product_name, i.category, i.m1, i.m2, i.drop, i.drop_pct, i.oos_days, i.oos_views, i.first_oos, i.last_oos, i.oos_linked ? 'Yes' : 'No'];
      if (hasPrice) r.push(i.price, i.revenue_lost);
      lines.push(r.map(q).join(','));
    });
    const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `kapruka-revenue-loss-${st.data.window.start.slice(0, 7)}.csv`;
    a.click(); URL.revokeObjectURL(a.href);
  });
})();
