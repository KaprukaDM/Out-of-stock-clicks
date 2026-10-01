# Out of Stock Dashboard

Source backup — the running instance is deployed separately (like
`hygiene-dashboard/` and `daraz-agent/`), not served through this Cloudflare
Pages site.

**Live:** http://23.111.183.110:8091 (open, no login)
**Runs on:** the same VPS as the campaign portal, its own port (8091), own
folder (`C:\apps\kapruka-oos-dashboard` on the VPS). Does not touch the
campaign-portal IIS site on port 80 or hygiene-dashboard on 8090.

Reads the GA4 `out_of_stock_view` event (fired when a shopper lands on a
product page marked out of stock) straight from the GA4 Data API — no daily
ingest script, refresh is on-demand from the dashboard's "Refresh from GA4"
button (pulls the last 30 days).

### Backfilling an explicit window

The button always pulls "the last N days ending yesterday", which can't
reach a month once it has scrolled out of range. `POST /api/refresh` takes
an explicit window for that:

```
curl -X POST "http://localhost:8091/api/refresh?start=2026-09-01&end=2026-09-30"
```

Rows upsert on `(product_code, date)`, so re-pulling a window you already
have overwrites those days rather than double-counting them. `end` is
clamped to yesterday (GA4 has nothing for today yet). A bad or reversed
date range returns 400 with the reason. A full month on this property is
~490k rows and takes about 2¼ minutes.

## Filters
- **Date range** — any window within the synced data
- **Category** — derived from the Partner Central product code prefix
- **Partner code** — Partner Central products embed a partner code in the
  product code, e.g. `ef_pc_home0v2057pod00141p` → partner `v02057`. GA4 has
  no partner *name* dimension, so partners are grouped/labelled by this code.
- **Source** — Partner Central and/or Ecommerce (Kapruka's own catalog)
- **Search** — product code or name

Demand is reported as the raw **OOS Views** count. There is no derived
"Interest" High/Medium/Low column — it was removed, since it only re-bucketed
that same count by percentile and implied more precision than GA4 supports
here (see the note on `totalUsers` in `app.py`'s docstring).

**Always-on recency gate:** a product code only appears in the report if it
had at least one `out_of_stock_view` in the **last 2 calendar days counted
back from today** (i.e. today + yesterday; cutoff shown in the note above the
table). This is anchored to *today*, not to the end of the selected date
range — the range still decides which hits are counted, the gate decides
which products are still worth acting on. Applied once in
`fetch_filtered_products()`, so the table, the pagination total and the
export all agree. Widen it with `RECENCY_GATE_DAYS` in `.env` if GA4 syncs
run less often than daily (GA4 itself lags ~1 day, and the refresh only
pulls up to yesterday, so an un-refreshed dashboard will legitimately show
an empty report — the note turns red and says so).

Each row also shows **days OOS** (days in the selected window with at least
one out_of_stock_view event — a GA4-visit proxy, not a direct inventory
feed) and the first/last date the event was seen. Clicking a row opens a
trend chart of daily OOS views vs. page views for that product.

## Tabs: "Still out of stock" / "Back in stock"

The products the recency gate removes don't just vanish — they move to the
second tab. A product that stops firing `out_of_stock_view` has, in
practice, come back in stock, and that tab answers *how long it was out of
stock* with two different numbers (`/api/back-in-stock`):

| Column | Means |
|---|---|
| **Days OOS** | Days that fired at least one `out_of_stock_view`. Same visit-proxy caveat as above: a day with no traffic to the product isn't counted even if it was genuinely unavailable. |
| **OOS Span** | Calendar days from the first to the last out-of-stock hit, inclusive. Always ≥ Days OOS; the outer bracket of the outage. |
| **Back In Stock** | Days since the last out-of-stock hit. Shown with a `~` — it's accurate to a day or two at best, given daily buckets plus GA4's own lag. |

Both duration figures are counted across **all synced data, not the active
report's date range** — a finished outage can sit entirely outside the
toolbar's window, so that control is hidden on this tab rather than silently
ignored. Category / partner / source / search still apply, and "Export to
Excel" exports whichever tab is open.

### The OOS window (this tab's own date range)

The tab has its own date pair, **OOS window (first → last)**
(`?oos_start=&oos_end=`), which filters on the outage itself rather than on
which hits get counted: a product is listed only if **both** its first and
its last out-of-stock hit fall inside those dates — "outages that happened
entirely in the first half of September". Leave it blank (or hit *Clear*)
for all synced dates.

It is containment, not overlap, on purpose. An outage straddling an edge is
left out rather than trimmed, because the Days OOS / OOS Span figures are
still totalled over all dates — clipping would print a duration that reaches
outside the window the user picked and reads as a bug. The tab badge and the
CSV export apply the same window, so the three never disagree.

Caveat worth repeating: if nobody refreshes from GA4 for a few days,
*everything* looks recovered because the data stopped, not because stock
came back. The note above the table turns red and says exactly that when the
newest synced date is older than the gate's cutoff.

**The gate is tight, and the tab inherits that.** On real September data
(27,847 products; 20,000 still out of stock, 7,847 back in stock), **39% of
the back-in-stock list — 3,090 products — had their last out-of-stock hit on
the second-to-last day**, i.e. they only missed the single final day. A
product with no site traffic for one day is indistinguishable here from one
that was restocked, so treat the short-gap end of this tab as "went quiet",
not "confirmed restocked". Raising `RECENCY_GATE_DAYS` (e.g. to 3–4) trades
some freshness on the main report for far fewer false recoveries.

## Performance notes

A real month is ~490k daily rows across ~28k products, and both tabs run a
GROUP BY over it. `idx_oos_rollup` (`product_code, date, oos_views,
category, partner_code, source_type, product_name`) is a covering index that
lets SQLite group straight off the index instead of building a temp B-tree.
Measured on September 2026 data:

| | before | after |
|---|---|---|
| `/api/products` | 8.2s | 2.5s |
| `/api/back-in-stock` | 5.6s | 2.3s |
| tab badge counts | a second full rollup | 0.7s (`/api/tab-counts`) |

Two things to leave alone unless you re-measure:
- The rollup queries say `INDEXED BY idx_oos_rollup` explicitly. With a date
  filter present the planner otherwise picks `idx_oos_date` and pays for the
  temp B-tree, and which one it picks changes depending on whether `ANALYZE`
  has ever been run on that copy of the DB.
- `active_product_codes()` is pinned to `idx_oos_date` instead — the recency
  cutoff is only ever a 2-day slice, so narrowing by date first wins there
  (0.46s vs 0.76s).

The index is roughly table-sized on disk (~182 MB total for September
alone), and `init_db()` builds it at startup, which takes a few seconds the
first time after deploying onto an existing `data.db`.

## Custom dimensions (GA4 Admin > Custom Definitions)
| Display name in GA4 UI | Event parameter | API name |
|---|---|---|
| Custom Parameter1 (desc: OOS product ID) | `custom_param1` | `customEvent:custom_param1` |
| Custom Param 2 Dimention | `custom_param2` | `customEvent:custom_param2` |

Note: GA4 also has a near-identically-named **"Custom Param 2 Dimension"**
(no typo) mapped to `custom_param3` — a different dimension entirely. The
app matches by exact display-name string and falls back to the hardcoded
`customEvent:custom_param1` / `customEvent:custom_param2` API names if the
Metadata API lookup doesn't find an exact match, so it never silently picks
the wrong one.

## Run locally
```
pip install -r requirements.txt
copy .env.example .env   # fill in GA4 credentials
set PORT=8091
python -m uvicorn app:app --port 8091 --env-file .env
```

Then open http://localhost:8091 and click "Refresh from GA4".
