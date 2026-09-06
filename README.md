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

## Filters
- **Date range** — any window within the synced data
- **Category** — derived from the Partner Central product code prefix
- **Partner code** — Partner Central products embed a partner code in the
  product code, e.g. `ef_pc_home0v2057pod00141p` → partner `v02057`. GA4 has
  no partner *name* dimension, so partners are grouped/labelled by this code.
- **Interest level** — 🔴 High / 🟠 Medium / ⚪ Low, based on out-of-stock view
  volume plus the product's regular page-view volume (a product with heavy
  OOS hits *and* a history of real traffic is "high interest"; a couple of
  stray OOS hits on a page nobody visits isn't)
- **Search** — product code or name

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
