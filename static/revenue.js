// Revenue loss tab: compare two periods of synced Ninja daily sales (units per
// product code) and find the products that sold less in the current period
// AND went out of stock during it (GA4 out_of_stock_view). The sales come
// from /api/sales-range (pushed daily by tools/ninja_sync.py); the matching
// and loss maths happen in /api/revenue-loss. app.js owns the first two tabs
// and ignores clicks on this one (its VIEWS map has no 'revenue'), so this
// file handles all tab clicks only to show/hide the right panel.
(function () {
  const $ = (id) => document.getElementById(id);
  const PAGE = 100;

  const st = {
    data: null, vendor: '', category: '', q: '', topMetric: 'lost',
    sort: 'lost', dir: 'desc', offset: 0, userSorted: false, priceSeq: 0,
  };

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n) => Math.round(n).toLocaleString();
  const money = (n) => (n == null ? '—' : Math.round(n).toLocaleString());
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

  // ───────────── period presets
  // "Current" is the recent period (and the OOS window); "previous" is the
  // baseline it is compared against. Both always have the same length except
  // in Custom. Dates are local calendar dates; "yesterday" is the newest day
  // that can be complete.
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const addDays = (d, n) => { const x = new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); return x; };

  function presetRanges(kind) {
    const today = new Date();
    const y = addDays(today, -1);
    if (kind === 'day') return { cur: [y, y], prev: [addDays(y, -1), addDays(y, -1)] };
    if (kind === 'week') {
      const dow = (today.getDay() + 6) % 7;            // Monday = 0
      const thisMon = addDays(today, -dow);
      return { cur: [addDays(thisMon, -7), addDays(thisMon, -1)], prev: [addDays(thisMon, -14), addDays(thisMon, -8)] };
    }
    if (kind === 'month') {
      // Month to date vs the same number of days of last month. On the 1st
      // there is no month-to-date yet, so use last month vs the one before.
      let curStart, curEnd;
      if (today.getDate() === 1) { curStart = new Date(today.getFullYear(), today.getMonth() - 1, 1); curEnd = addDays(today, -1); }
      else { curStart = new Date(today.getFullYear(), today.getMonth(), 1); curEnd = y; }
      const len = Math.round((curEnd - curStart) / 86400000) + 1;
      const prevStart = new Date(curStart.getFullYear(), curStart.getMonth() - 1, 1);
      const prevMax = new Date(curStart.getFullYear(), curStart.getMonth(), 0);
      const prevEnd = addDays(prevStart, len - 1) > prevMax ? prevMax : addDays(prevStart, len - 1);
      return { cur: [curStart, curEnd], prev: [prevStart, prevEnd] };
    }
    const n = Number(kind);                              // 7 / 14 / 30
    return { cur: [addDays(y, -(n - 1)), y], prev: [addDays(y, -(2 * n - 1)), addDays(y, -n)] };
  }

  // Only the Compare dropdown is shown; the four dates are filled in for you
  // and appear for editing only under "Custom".
  const nice = (d) => d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  function applyPreset() {
    const kind = $('cmpPreset').value;
    const custom = kind === 'custom';
    $('cmpCustom').hidden = !custom;
    $('cmpSummaryWrap').hidden = custom;
    if (custom) return;
    const r = presetRanges(kind);
    $('prevA').value = iso(r.prev[0]); $('prevB').value = iso(r.prev[1]);
    $('curA').value = iso(r.cur[0]); $('curB').value = iso(r.cur[1]);
    const span = (a) => (iso(a[0]) === iso(a[1]) ? nice(a[0]) : `${nice(a[0])} – ${nice(a[1])}`);
    $('cmpSummary').textContent = `${span(r.prev)}  vs  ${span(r.cur)}`;
  }
  $('cmpPreset').addEventListener('change', applyPreset);
  // Editing a date by hand turns the preset into Custom.
  ['prevA', 'prevB', 'curA', 'curB'].forEach((id) => $(id).addEventListener('input', () => { $('cmpPreset').value = 'custom'; }));
  applyPreset();

  fetch('api/sales-coverage').then((r) => r.json()).then((c) => {
    $('revSyncCov').textContent = c.days
      ? `${c.days} day${c.days === 1 ? '' : 's'} of sales synced · ${c.from} → ${c.to}`
      : 'no sales synced yet — run tools/ninja_sync.py on the office machine';
  }).catch(() => { $('revSyncCov').textContent = ''; });

  // ───────────── run
  $('revRun').addEventListener('click', run);

  async function getRange(s, e) {
    const res = await fetch(`api/sales-range?start=${s}&end=${e}`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  }

  // ───────────── make sure both data sources cover the picked ranges
  // Picking a range should never be a dead end: GA4 out-of-stock data the
  // server is missing is pulled by the server itself, and Ninja sales it is
  // missing are queued for the office machine's watcher (tools/ninja_sync.py
  // --watch). We wait for both, show progress, and then analyse.
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function post(url, body) {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  }

  async function ensureGa4(a, b, say) {
    for (let round = 0; round < 3; round++) {
      const r = await post('api/ga4-ensure', { start: a, end: b });
      if (r.covered) return;
      say(`Pulling GA4 out-of-stock data for ${a} → ${b}… this can take a couple of minutes.`);
      for (let i = 0; i < 120; i++) {
        await sleep(3000);
        const s = await (await fetch('api/refresh/status')).json();
        if (s.status === 'error') throw new Error('GA4 pull failed: ' + s.error);
        if (s.status !== 'running') break;
      }
    }
  }

  async function ensureSales(ranges, say) {
    const missing = new Set();
    for (const [a, b] of ranges) (await post('api/sales-request', { start: a, end: b })).missing.forEach((d) => missing.add(d));
    if (!missing.size) return;
    const total = missing.size, t0 = Date.now();
    for (;;) {
      const cov = await (await fetch('api/sales-coverage')).json();
      if (!cov.pending_requests) return;
      const listening = cov.watcher_age_s != null && cov.watcher_age_s < 120;
      say(`Fetching ${total - cov.pending_requests} of ${total} missing sales days from Ninja…` +
        (listening ? '' : ' No office-machine watcher seen recently: run “python tools/ninja_sync.py --watch” there.'));
      if (Date.now() - t0 > 5 * 60 * 1000) return;   // carry on with what we have; the gap warning will say so
      await sleep(4000);
    }
  }

  async function run() {
    const msg = $('syncMsg'), btn = $('revRun');
    const p1 = $('prevA').value, p2 = $('prevB').value, c1 = $('curA').value, c2 = $('curB').value;
    msg.hidden = true;
    if (!(p1 && p2 && c1 && c2)) { msg.hidden = false; msg.textContent = 'Pick both date ranges.'; return; }
    if (p1 > p2 || c1 > c2) { msg.hidden = false; msg.textContent = 'A range starts after it ends.'; return; }
    btn.disabled = true; btn.textContent = 'Syncing…';
    const say = (t) => { msg.hidden = false; msg.textContent = t; };
    try {
      await ensureGa4(c1, c2, say);
      await ensureSales([[p1, p2], [c1, c2]], say);
      btn.textContent = 'Analysing…'; msg.hidden = true;
      const [A, B] = await Promise.all([getRange(p1, p2), getRange(c1, c2)]);
      const gaps = [[A, 'Previous'], [B, 'Current']].filter(([d]) => d.days_synced < d.days_requested)
        .map(([d, n]) => `${n}: ${d.days_synced} of ${d.days_requested} days synced`);
      if (!A.days_synced || !B.days_synced) throw new Error('No synced sales for one of those periods. ' + gaps.join(' · '));
      const notes = [];
      if (gaps.length) notes.push('Missing days, so totals are understated. ' + gaps.join(' · '));
      if (A.days_requested !== B.days_requested) notes.push(`The periods differ in length (${A.days_requested} vs ${B.days_requested} days), so quantity drops are not like for like.`);
      if (p2 >= c1) notes.push('The periods overlap.');
      if (notes.length) { msg.hidden = false; msg.textContent = '⚠ ' + notes.join(' ⚠ '); }

      const prev = new Map(A.rows.map((r) => [r.code, r]));
      const cur = new Map(B.rows.map((r) => [r.code, r]));
      const rows = [];
      for (const code of new Set([...prev.keys(), ...cur.keys()])) {
        const a = prev.get(code), b = cur.get(code);
        rows.push({ code, name: (a && a.name) || (b && b.name) || null, m1: a ? a.qty : 0, m2: b ? b.qty : 0 });
      }
      const res = await fetch('api/revenue-loss', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows, oos_start: c1, oos_end: c2, min_oos_days: Number($('revMinDays').value) || 1 }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      st.data = await res.json();
      st.data.periods = { prev: [p1, p2], cur: [c1, c2], prevDays: A.days_requested, curDays: B.days_requested };
      st.vendor = ''; st.category = ''; st.q = ''; st.offset = 0; st.sort = 'lost'; st.dir = 'desc';
      fillFilters();
      $('revSearch').value = '';
      st.topMetric = 'lost'; st.userMetric = false; st.userSorted = false;
      renderAll();
      pollPrices();
    } catch (e) {
      msg.hidden = false; msg.textContent = e.message;
    } finally {
      btn.disabled = false; btn.textContent = 'Analyse';
    }
  }

  // ───────────── render
  function renderAll() {
    const d = st.data, t = d.totals, per = d.periods;
    $('revResults').hidden = false;

    const n = $('revNotice'); n.hidden = false;
    const covered = d.window.covered_days, days = d.window.days;
    if (!d.window.data_from || covered === 0) {
      n.className = 'recency-note recency-stale';
      n.innerHTML = `⚠️ No GA4 out-of-stock data is synced for <strong>${d.window.start} → ${d.window.end}</strong>, so no product can be matched as out of stock. Backfill that period (README: <code>POST /api/refresh?start=…&amp;end=…</code>) and run again.`;
    } else if (covered < days) {
      n.className = 'recency-note recency-stale';
      n.innerHTML = `⚠️ Synced GA4 out-of-stock data covers only <strong>${covered} of ${days} days</strong> of the current period (data ${d.window.data_from} → ${d.window.data_to}). Products out of stock on the missing days are not flagged, so the loss is understated.`;
    } else {
      n.className = 'recency-note';
      n.innerHTML = `📅 Previous <strong>${per.prev[0]} → ${per.prev[1]}</strong> vs current <strong>${per.cur[0]} → ${per.cur[1]}</strong>. Checked the <strong>${fmt(t.ranked)}</strong> products that sold in the previous period for ≥ <strong>${d.params.min_oos_days}</strong> out-of-stock day${d.params.min_oos_days === 1 ? '' : 's'} in the current one. Loss is in <strong>units</strong> (Ninja gives no price).`;
    }

    renderKpis();
    $('revVendorFilter').hidden = !d.items.some((i) => i.vendor !== 'Unknown');
    renderTop(); renderList();
  }


  // Rupee figures arrive as the server looks products up on Kapruka's MCP, so
  // the KPI cards are rebuilt from the items every time prices come in.
  function renderKpis() {
    const d = st.data, t = d.totals;
    const priced = d.items.filter((i) => i.rev_lost != null);
    const revenue = priced.reduce((a, i) => a + i.rev_lost, 0);
    const pendingN = d.items.length - priced.length - d.items.filter((i) => i.price_failed).length;
    const kpi = (label, value, sub, cls = '') => `<div class="rev-kpi ${cls}"><div class="rev-kpi-label">${label}</div><div class="rev-kpi-value">${value}</div><div class="rev-kpi-sub">${sub}</div></div>`;
    $('revKpis').innerHTML =
      kpi('Sold in previous period', fmt(t.ranked), `of ${fmt(t.uploaded)} products in either period`) +
      kpi('Went out of stock', fmt(t.went_oos), `${pct(t.went_oos, t.ranked)} of those`, 'accent') +
      kpi('With lost sales', fmt(t.with_loss), 'out of stock and sold less than before', 'accent') +
      kpi('Units lost', fmt(t.lost), `${pct(t.lost, t.ranked_base1)} of their previous-period units`, 'accent') +
      kpi('Revenue lost (LKR)', priced.length ? money(revenue) : '…',
        priced.length === d.items.length ? 'units lost × current price'
          : `pricing ${fmt(priced.length)} of ${fmt(d.items.length)}${pendingN > 0 ? ' — still filling in' : ''}`, 'danger');
  }

  // After an analysis, poll the server for prices it is still looking up and
  // fold them in as they land. Stops when nothing is pending or the user
  // starts a new analysis (priceSeq).
  async function pollPrices() {
    const seq = ++st.priceSeq;
    for (let n = 0; n < 400; n++) {
      if (seq !== st.priceSeq || !st.data) return;
      const todo = st.data.items.filter((i) => i.price == null && !i.price_failed);
      if (!todo.length) return;
      let r;
      try { r = await post('api/prices/lookup', { codes: todo.map((i) => i.product_code) }); } catch (e) { return; }
      if (seq !== st.priceSeq) return;
      let changed = false;
      for (const it of todo) {
        const info = r.prices[it.product_code];
        if (!info) continue;
        if (info.price == null) { it.price_failed = true; changed = true; continue; }
        it.price = info.price; it.rev_lost = it.lost * info.price;
        if (info.vendor) it.vendor = info.vendor;
        changed = true;
      }
      if (changed) {
        if (!st.userSorted && st.sort !== 'rev_lost') { st.sort = 'rev_lost'; st.dir = 'desc'; }
        if (st.topMetric === 'lost' && !st.userMetric) st.topMetric = 'rev_lost';
        fillFilters(); renderKpis(); renderTop(); renderList();
      }
      if (!r.pending) { if (!todo.some((i) => !r.prices[i.product_code])) return; }
      await sleep(6000);
    }
  }

  // Top-products panel: the biggest losers by the chosen metric, taken from
  // whatever the category / vendor / search filters currently select.
  function topProducts() {
    const k = st.topMetric;
    return filtered().sort((a, b) => (b[k] ?? -1) - (a[k] ?? -1));
  }
  function renderTop() {
    const k = st.topMetric;
    const all = topProducts();
    const chart = all.slice(0, 12);
    const max = Math.max(1, ...chart.map((i) => i[k] ?? 0));
    $('revTopChart').innerHTML = chart.length ? chart.map((i) => `
      <div class="rev-bar-row" data-code="${esc(i.product_code)}" title="${esc(i.product_code)} · ${esc(i.vendor)}">
        <span class="rev-bar-label">${esc(i.product_name || i.product_code)}</span>
        <span class="rev-bar-track"><i class="seg-oos" style="width:${Math.max(0, i[k] ?? 0) / max * 100}%"></i></span>
        <span class="rev-bar-val">${money(i[k])}</span>
      </div>`).join('') : '<div class="empty-state">No products match.</div>';
    $('revTopBody').innerHTML = all.slice(0, 25).map((i) => `
      <tr data-code="${esc(i.product_code)}">
        <td><div class="rev-prod-name">${esc(i.product_name || '—')}</div><div class="pc-code">${esc(i.product_code)}</div></td>
        <td class="num loss">${money(i.rev_lost)}</td>
        <td class="num">${fmt(i.lost)}</td>
        <td class="num">${i.qty_drop_pct == null ? '—' : (i.qty_drop_pct > 0 ? '−' : '+') + Math.abs(i.qty_drop_pct) + '%'}</td>
        <td class="num">${fmt(i.oos_views)}</td>
      </tr>`).join('') || '<tr><td colspan="5" class="empty-state">Nothing to show.</td></tr>';
    document.querySelectorAll('#revTopChart .rev-bar-row, #revTopBody tr[data-code]').forEach((r) => r.addEventListener('click', () => openTrend(r.dataset.code)));
    document.querySelectorAll('#revTopSeg button').forEach((b) => b.classList.toggle('active', b.dataset.metric === k));
  }
  document.querySelectorAll('#revTopSeg button').forEach((b) => b.addEventListener('click', () => { st.topMetric = b.dataset.metric; st.userMetric = true; renderTop(); }));
  const refresh = () => { renderTop(); renderList(); };

  // Category / vendor dropdowns list only what is in the current results.
  function fillFilters() {
    const fill = (sel, values, all) => {
      const keep = sel.value;
      sel.innerHTML = `<option value="">${all}</option>` + values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
      if (values.includes(keep)) sel.value = keep;
    };
    const uniq = (k) => [...new Set(st.data.items.map((i) => i[k]))].sort((a, b) => a.localeCompare(b));
    fill($('revCatFilter'), uniq('category'), 'All categories');
    fill($('revVendorFilter'), uniq('vendor'), 'All vendors');
    $('revVendorFilter').hidden = !st.data.items.some((i) => i.vendor !== 'Unknown');
  }
  $('revCatFilter').addEventListener('change', (e) => { st.category = e.target.value; st.offset = 0; refresh(); });
  $('revVendorFilter').addEventListener('change', (e) => { st.vendor = e.target.value; st.offset = 0; refresh(); });

  function filtered() {
    const q = st.q.toLowerCase();
    return st.data.items.filter((i) =>
      (!st.vendor || i.vendor === st.vendor) &&
      (!st.category || i.category === st.category) &&
      (!q || i.product_code.toLowerCase().includes(q) || (i.product_name || '').toLowerCase().includes(q)));
  }

  function sorted(list) {
    const k = st.sort, dir = st.dir === 'asc' ? 1 : -1;
    return list.sort((a, b) => {
      const x = a[k] ?? '', y = b[k] ?? '';
      return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
    });
  }

  function renderList() {
    const list = sorted(filtered());
    const page = list.slice(st.offset, st.offset + PAGE);
    $('revBody').innerHTML = page.length ? page.map((i) => `
      <tr data-code="${esc(i.product_code)}">
        <td class="pc-code">${esc(i.product_code)}</td>
        <td>${esc(i.product_name || '—')}</td>
        <td>${esc(i.vendor)}</td>
        <td><span class="category-badge">${esc(i.category)}</span></td>
        <td class="num"><span class="oos-days-badge">${i.oos_days}d</span></td>
        <td class="num">${fmt(i.oos_views)}</td>
        <td class="num">${fmt(i.m1)}</td>
        <td class="num">${fmt(i.m2)}</td>
        <td class="num ${i.qty_drop > 0 ? 'loss' : ''}">${i.qty_drop > 0 ? '−' : i.qty_drop < 0 ? '+' : ''}${fmt(Math.abs(i.qty_drop))}</td>
        <td class="num">${i.qty_drop_pct == null ? '—' : (i.qty_drop_pct > 0 ? '−' : '+') + Math.abs(i.qty_drop_pct) + '%'}</td>
        <td class="num loss">${fmt(i.lost)}</td>
        <td class="num">${money(i.price)}</td>
        <td class="num loss">${i.rev_lost != null ? money(i.rev_lost) : (i.price_failed ? '<span class="muted-note">n/a</span>' : '<span class="muted-note">…</span>')}</td>
      </tr>`).join('')
      : '<tr><td colspan="13" class="empty-state">No products went out of stock with lost sales for these settings.</td></tr>';
    sortMarks('revTable', st.sort, st.dir);
    $('revBody').querySelectorAll('tr[data-code]').forEach((r) => r.addEventListener('click', () => openTrend(r.dataset.code)));

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
    st.userSorted = true;
    if (st.sort === k) st.dir = st.dir === 'asc' ? 'desc' : 'asc'; else { st.sort = k; st.dir = 'desc'; }
    st.offset = 0; renderList();
  }));
  let deb; $('revSearch').addEventListener('input', (e) => { clearTimeout(deb); deb = setTimeout(() => { st.q = e.target.value.trim(); st.offset = 0; refresh(); }, 250); });

  // Client-side CSV of exactly the rows the current filters select.
  $('revExport').addEventListener('click', () => {
    if (!st.data) return;
    const head = ['Product Code', 'Product Name', 'Vendor', 'Category', 'Days OOS', 'Units Lost', 'Price (LKR)', 'Revenue Lost (LKR)',
      'Previous Qty', 'Current Qty', 'Qty Drop', 'Qty Drop %', 'OOS Hits', 'First OOS', 'Last OOS'];
    const q = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
    const lines = [head.map(q).join(',')];
    sorted(filtered()).forEach((i) => {
      lines.push([i.product_code, i.product_name, i.vendor, i.category, i.oos_days, i.lost, i.price, i.rev_lost, i.m1, i.m2, i.qty_drop, i.qty_drop_pct,
        i.oos_views, i.first_oos, i.last_oos].map(q).join(','));
    });
    const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `kapruka-oos-loss-${st.data.periods.cur[0]}_${st.data.periods.cur[1]}.csv`;
    a.click(); URL.revokeObjectURL(a.href);
  });
})();
