// Key products tab: best sellers (from the synced Ninja daily sales) watched
// for stock-outs. The hourly check and the WhatsApp alert run on the server
// (see "key products" in app.py); this tab shows the list, lets you pin
// products, run a check now, and send a test message.
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n) => Math.round(n).toLocaleString();
  const st = { data: null, loaded: false };

  // Other tabs are handled by app.js / revenue.js; this tab only has to show
  // its own panel and hide it again when any other tab is clicked. It is
  // registered last, so its show/hide wins over theirs.
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      const on = tab.dataset.view === 'keyprod';
      $('kpPanel').hidden = !on;
      if (on) {
        ['oosTop', 'oosMain', 'revPanel'].forEach((id) => { $(id).hidden = true; });
        document.querySelectorAll('.tab').forEach((t) => {
          const cur = t === tab;
          t.classList.toggle('active', cur);
          t.setAttribute('aria-selected', String(cur));
        });
        load();
      }
    });
  });

  async function load() {
    try {
      const res = await fetch('api/key-products');
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      st.data = await res.json();
      st.loaded = true;
      render();
    } catch (e) {
      const n = $('kpNotice'); n.hidden = false; n.className = 'recency-note recency-stale';
      n.textContent = 'Could not load key products: ' + e.message;
    }
  }

  function render() {
    const { items, meta, alerts } = st.data;
    const chip = (cls, text) => `<span class="kp-chip ${cls}">${text}</span>`;
    const w = meta.whatsapp, m = meta.monitor;
    $('kpChips').innerHTML = [
      w.configured ? chip('good', `WhatsApp on · ${w.recipients} recipient${w.recipients === 1 ? '' : 's'} · ${w.mode}`) : chip('bad', 'WhatsApp not configured — alerts are NOT being sent'),
      m.enabled ? chip('good', `Hourly check on${m.last_run ? ' · last ' + esc(m.last_run) : ''}`) : chip('bad', 'Hourly check off'),
      meta.sales_through ? chip(meta.sales_age_days > 2 ? 'warn' : '', `Sales through ${esc(meta.sales_through)}`) : chip('warn', 'No sales synced'),
    ].join('');

    const n = $('kpNotice');
    const notes = [];
    if (!meta.sales_through) notes.push('No Ninja sales are synced yet, so only pinned products are listed. Run <code>tools/ninja_sync.py</code> on the office machine.');
    else if (meta.sales_age_days > 2) notes.push(`Sales data is ${meta.sales_age_days} days old, so the key-product ranking may be stale. Run <code>tools/ninja_sync.py</code>.`);
    if (!w.configured) notes.push('Set <code>WHATSAPP_PHONE_ID</code>, <code>WHATSAPP_ACCESS_TOKEN</code> and <code>WHATSAPP_TO</code> in the server <code>.env</code> to start sending alerts.');
    if (m.last_error) notes.push('Last check failed: ' + esc(m.last_error));
    const pull = m.last_result && m.last_result.pull;
    if (pull && pull.error) notes.push('Last GA4 pull failed: ' + esc(pull.error));
    n.hidden = !notes.length;
    n.className = 'recency-note' + (notes.length ? ' recency-stale' : '');
    n.innerHTML = notes.join('<br>');

    const kpi = (label, value, sub, cls = '') => `<div class="rev-kpi ${cls}"><div class="rev-kpi-label">${label}</div><div class="rev-kpi-value">${value}</div><div class="rev-kpi-sub">${sub}</div></div>`;
    $('kpKpis').innerHTML =
      kpi('Key products', fmt(meta.key_count), `top ${meta.top_n} by 7d ∪ 14d units + pinned`) +
      kpi('Out of stock now', fmt(meta.oos_now), 'first hit today', meta.oos_now ? 'danger' : 'accent') +
      kpi('Pinned', fmt(meta.pinned), 'added by hand') +
      kpi('Next check', m.next_run ? esc(m.next_run.slice(11, 16)) : '—', `every ${m.interval_min} min`);

    renderRows();
    $('kpAlerts').innerHTML = alerts.length
      ? alerts.map((a) => `<div class="kp-alert-row">${a.sent ? '✅' : '⚠️'} <strong>${esc(a.ts)}</strong> · ${a.kind === 'test' ? 'test message' : a.count + ' product' + (a.count === 1 ? '' : 's') + ' out of stock'} · <span class="muted-note">${esc(a.detail)}</span></div>`).join('')
      : 'No alerts yet.';
  }

  function visible() {
    const f = $('kpFilter').value, q = $('kpSearch').value.trim().toLowerCase();
    return st.data.items.filter((i) =>
      (f === 'all' || (f === 'oos' && i.status === 'oos') || (f === 'pinned' && i.pinned)) &&
      (!q || i.product_code.toLowerCase().includes(q) || (i.name || '').toLowerCase().includes(q)));
  }

  function renderRows() {
    const rows = visible();
    $('kpBody').innerHTML = rows.length ? rows.map((i) => {
      const status = i.status === 'oos' ? '<span class="oos-days-badge">OUT OF STOCK</span>'
        : i.status === 'recent' ? '<span class="badge-amber">OOS yesterday</span>'
        : '<span class="recovered-badge">In stock</span>';
      return `<tr data-code="${esc(i.product_code)}" class="${i.status === 'oos' ? 'is-oos' : ''}">
        <td class="num">${i.rank ?? '—'}</td>
        <td class="pc-code">${esc(i.product_code)}</td>
        <td>${esc(i.name || '—')}<span class="kp-why">${esc(i.why.join(' · '))}</span></td>
        <td class="num">${fmt(i.u7)}</td><td class="num">${fmt(i.u14)}</td><td class="num">${i.per_day}</td>
        <td>${status}</td>
        <td class="num">${fmt(i.hits_today)}</td><td class="num">${i.days_oos_7d || 0}</td>
        <td class="muted-note">${i.alerted ? '✓ ' + esc((i.last_alert_at || '').slice(5, 16)) : '—'}</td>
        <td><button class="kp-pin ${i.pinned ? 'on' : ''}" data-pin="${esc(i.product_code)}" title="${i.pinned ? 'Unpin' : 'Pin'}">${i.pinned ? '★' : '☆'}</button></td>
      </tr>`;
    }).join('') : '<tr><td colspan="11" class="empty-state">No key products match.</td></tr>';
    $('kpBody').querySelectorAll('tr[data-code]').forEach((r) => r.addEventListener('click', (e) => {
      if (e.target.closest('.kp-pin')) return;
      openTrend(r.dataset.code);
    }));
    $('kpBody').querySelectorAll('.kp-pin').forEach((b) => b.addEventListener('click', () => {
      const item = st.data.items.find((i) => i.product_code === b.dataset.pin);
      pin(b.dataset.pin, !(item && item.pinned));
    }));
  }

  async function pin(code, pinned) {
    const res = await fetch('api/key-products/pin', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code, pinned }) });
    if (!res.ok) { alert((await res.json().catch(() => ({}))).detail || 'Could not pin'); return; }
    await load();
  }

  $('kpFilter').addEventListener('change', renderRows);
  let deb; $('kpSearch').addEventListener('input', () => { clearTimeout(deb); deb = setTimeout(renderRows, 250); });
  $('kpPinBtn').addEventListener('click', () => {
    const code = $('kpPinCode').value.trim();
    if (code) { pin(code, true); $('kpPinCode').value = ''; }
  });
  $('kpPinCode').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('kpPinBtn').click(); });

  $('kpCheck').addEventListener('click', async () => {
    const b = $('kpCheck'); b.disabled = true; b.textContent = 'Checking…';
    try {
      const res = await fetch('api/monitor/check', { method: 'POST' });
      const r = await res.json();
      const n = $('kpNotice'); n.hidden = false; n.className = 'recency-note';
      n.innerHTML = r.skipped ? esc(r.skipped)
        : `Checked ${fmt(r.key_count || 0)} key products · ${fmt(r.oos_now || 0)} out of stock now · ${fmt(r.new_oos || 0)} new${r.alert ? ' · alert ' + (r.alert.sent ? 'sent' : 'NOT sent (' + esc(r.alert.detail) + ')') : ''}`;
      await load();
    } catch (e) { alert('Check failed: ' + e.message); }
    finally { b.disabled = false; b.textContent = '↻ Check now'; }
  });

  $('kpTest').addEventListener('click', async () => {
    const b = $('kpTest'); b.disabled = true;
    try {
      const res = await fetch('api/monitor/test', { method: 'POST' });
      const r = await res.json();
      alert(res.ok ? (r.sent ? 'Test message sent: ' + r.detail : 'Not sent: ' + r.detail) : (r.detail || 'Failed'));
      await load();
    } catch (e) { alert('Failed: ' + e.message); }
    finally { b.disabled = false; }
  });
})();
