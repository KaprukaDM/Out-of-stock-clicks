#!/usr/bin/env python3
"""
Kapruka Out-of-Stock Dashboard - backend.

Pulls the GA4 `out_of_stock_view` event (fired when a shopper lands on a
product page that is marked out of stock).

Interest scoring is based on out_of_stock_view event count alone. We looked
for a way to cross-reference regular page-view traffic per product (to tell
"nobody wants this anyway" apart from "people keep showing up for a product
they can't buy") but custom_param1/2 are event-scoped params that GA4 only
ever populates on out_of_stock_view - they come back "(not set)" on
page_view, so there is no per-product page-view figure to join against.
totalUsers on this event is also stuck at 1 even for products with 2000+
events, which points at a client_id/user_id gap in how the event is tagged
rather than 2000 real repeat visits - worth fixing at the source in GTM/GA4,
but out of scope here. Event count is therefore the only real signal
available; the UI surfaces this caveat rather than hiding it behind a score
that looks more precise than the underlying data supports.

Custom dimensions: GA4 only exposes registered custom dimensions by their
*display name* in the UI ("Custom Parameter1", "Custom Param 2 Dimention")
but the Data API needs the underlying `customEvent:<param_name>` API name.
We resolve those once at startup via the Metadata API instead of hardcoding
a guess (see resolve_dimensions()).

Partner detection: Partner Central product codes embed a partner code, e.g.
`ef_pc_home0v2057pod00141p` -> partner `v02057` (the `v` + 5 digits right
after the fixed `ef_pc_home0` prefix). No partner name is available from
GA4, so partners are grouped and labelled by this code alone.

Storage: SQLite (data.db, next to this file), refreshed on demand from GA4 -
no daily ingest script needed, unlike hygiene-dashboard.

`days_oos` is a GA4-visit proxy for time out of stock: the count of days in
the selected window where the product had at least one out_of_stock_view
event. GA4 has no direct inventory feed, so a day with zero site traffic to
an OOS product isn't counted even if it was still unavailable that day.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data.db"
STATIC = ROOT / "static"

GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "")
GA4_CLIENT_ID = os.environ.get("GA4_CLIENT_ID", "")
GA4_CLIENT_SECRET = os.environ.get("GA4_CLIENT_SECRET", "")
GA4_REFRESH_TOKEN = os.environ.get("GA4_REFRESH_TOKEN", "")

# Confirmed from GA4 Admin > Custom Definitions:
#   "Custom Parameter1"          (desc: OOS product ID) -> event param custom_param1 -> customEvent:custom_param1
#   "Custom Param 2 Dimention"   ->  event param custom_param2 -> customEvent:custom_param2
# NOTE: GA4 also has a near-identically-named "Custom Param 2 Dimension" (no
# typo) mapped to custom_param3 - a *different* dimension. Match on the exact
# display name only; never fuzzy-match "Custom Param 2" or you'll silently
# pull the wrong dimension.
PRODUCT_CODE_DISPLAY_NAME = os.environ.get("PRODUCT_CODE_DIMENSION_LABEL", "Custom Parameter1")
PRODUCT_NAME_DISPLAY_NAME = os.environ.get("PRODUCT_NAME_DIMENSION_LABEL", "Custom Param 2 Dimention")
PRODUCT_CODE_API_NAME = os.environ.get("PRODUCT_CODE_API_NAME", "customEvent:custom_param1")
PRODUCT_NAME_API_NAME = os.environ.get("PRODUCT_NAME_API_NAME", "customEvent:custom_param2")
OOS_EVENT_NAME = os.environ.get("OOS_EVENT_NAME", "out_of_stock_view")

# Partner Central product codes look like ef_pc_<category>0v<partner#><pod|p><product#>[p]
# e.g. ef_pc_home0v2057pod00141p -> category "home", partner "v02057" (zero-padded to 5 digits)
PARTNER_CODE_RE = re.compile(r"^ef_pc_[a-z]+0v(\d+)", re.IGNORECASE)
CATEGORY_CODE_RE = re.compile(r"^ef_pc_([a-z]+?)0v\d+", re.IGNORECASE)

app = FastAPI(title="Kapruka Out-of-Stock Dashboard")

_resolved_dims: dict[str, str] = {}
_token_cache = {"token": None, "expiry": 0}


# ------------------------------------------------------------------ auth
async def get_access_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expiry"]:
        return _token_cache["token"]
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GA4_CLIENT_ID,
                "client_secret": GA4_CLIENT_SECRET,
                "refresh_token": GA4_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )
    data = res.json()
    if "access_token" not in data:
        raise RuntimeError(f"Token refresh failed: {data}")
    _token_cache["token"] = data["access_token"]
    _token_cache["expiry"] = time.time() + data.get("expires_in", 3600) - 60
    return _token_cache["token"]


# ------------------------------------------------------------- dimension resolution
async def resolve_dimensions() -> dict[str, str]:
    """Confirm the configured API dimension names actually exist on this GA4
    property (via the Metadata API), matching by *exact* display name only -
    GA4 has near-duplicate labels here ("Custom Param 2 Dimention" vs
    "...Dimension") so a fuzzy match risks silently picking the wrong one.
    Falls back to the hardcoded PRODUCT_*_API_NAME if metadata lookup fails
    or doesn't find an exact label match, rather than blocking startup.
    """
    global _resolved_dims
    if _resolved_dims:
        return _resolved_dims

    resolved = {"product_code": PRODUCT_CODE_API_NAME, "product_name": PRODUCT_NAME_API_NAME}

    try:
        token = await get_access_token()
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(
                f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY_ID}/metadata",
                headers={"Authorization": f"Bearer {token}"},
            )
        dims = res.json().get("dimensions", [])

        def find_exact(display_name: str) -> str | None:
            for d in dims:
                if d.get("uiName", "").strip() == display_name.strip():
                    return d["apiName"]
            return None

        code_dim = find_exact(PRODUCT_CODE_DISPLAY_NAME)
        name_dim = find_exact(PRODUCT_NAME_DISPLAY_NAME)
        if code_dim:
            resolved["product_code"] = code_dim
        if name_dim:
            resolved["product_name"] = name_dim
    except Exception:
        pass  # keep the configured defaults

    _resolved_dims = resolved
    return _resolved_dims


def extract_partner_code(product_code: str) -> str | None:
    if not product_code:
        return None
    m = PARTNER_CODE_RE.match(product_code)
    if not m:
        return None
    return "v" + m.group(1).zfill(5)


def extract_category(product_code: str) -> str | None:
    """Category token out of the PC code, e.g. ef_pc_home0v2057pod00141p -> 'home'."""
    if not product_code:
        return None
    m = CATEGORY_CODE_RE.match(product_code)
    return m.group(1) if m else None


def extract_source_type(product_code: str) -> str:
    """'partner_central' for ef_pc_* codes (third-party sellers via Partner
    Central), 'ecommerce' for everything else (Kapruka's own catalog, e.g.
    grocery001737, pharmacy00844)."""
    if product_code and product_code.lower().startswith("ef_pc_"):
        return "partner_central"
    return "ecommerce"


# ------------------------------------------------------------------ db
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("""
    CREATE TABLE IF NOT EXISTS oos_daily (
        date TEXT NOT NULL,
        product_code TEXT NOT NULL,
        product_name TEXT,
        partner_code TEXT,
        category TEXT,
        source_type TEXT,
        oos_views INTEGER DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY (product_code, date)
    )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_oos_date ON oos_daily(date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_oos_partner ON oos_daily(partner_code)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_oos_category ON oos_daily(category)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_oos_source ON oos_daily(source_type)")
    con.commit()
    con.close()


init_db()


# ------------------------------------------------------------------ GA4 fetch
async def fetch_oos_events(token: str, dims: dict, start_date: str, end_date: str) -> list[dict]:
    body = {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [
            {"name": "date"},
            {"name": dims["product_code"]},
            {"name": dims["product_name"]},
        ],
        "metrics": [{"name": "eventCount"}],
        "dimensionFilter": {
            "filter": {
                "fieldName": "eventName",
                "stringFilter": {"matchType": "EXACT", "value": OOS_EVENT_NAME},
            }
        },
        "limit": 100000,
    }

    rows_out = []
    offset = 0
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            body["offset"] = offset
            res = await client.post(
                f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY_ID}:runReport",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
            )
            data = res.json()
            rows = data.get("rows", [])
            if not rows:
                break
            rows_out.extend(rows)
            if len(rows) < 100000:
                break
            offset += 100000
    return rows_out


async def refresh_from_ga4(days: int = 30) -> dict:
    if not GA4_PROPERTY_ID or not GA4_REFRESH_TOKEN:
        raise RuntimeError("GA4 credentials not configured (set GA4_PROPERTY_ID, GA4_CLIENT_ID, GA4_CLIENT_SECRET, GA4_REFRESH_TOKEN)")

    token = await get_access_token()
    dims = await resolve_dimensions()

    from datetime import datetime, timedelta
    end = datetime.utcnow().date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    start_s, end_s = start.isoformat(), end.isoformat()

    oos_rows = await fetch_oos_events(token, dims, start_s, end_s)

    def parse_date(v: str) -> str:
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"

    merged: dict[tuple, dict] = {}

    for row in oos_rows:
        vals = row["dimensionValues"]
        date_s = parse_date(vals[0]["value"])
        code = vals[1]["value"]
        name = vals[2]["value"]
        count = int(row["metricValues"][0]["value"] or 0)
        key = (date_s, code)
        rec = merged.setdefault(key, {"product_name": name, "oos_views": 0})
        rec["oos_views"] += count
        if name:
            rec["product_name"] = name

    con = db()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    n = 0
    for (date_s, code), rec in merged.items():
        if not code or code == "(not set)":
            continue
        con.execute("""
            INSERT INTO oos_daily (date, product_code, product_name, partner_code, category, source_type, oos_views, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(product_code, date) DO UPDATE SET
                product_name=excluded.product_name,
                partner_code=excluded.partner_code,
                category=excluded.category,
                source_type=excluded.source_type,
                oos_views=excluded.oos_views,
                updated_at=excluded.updated_at
        """, (
            date_s, code, rec.get("product_name"),
            extract_partner_code(code), extract_category(code), extract_source_type(code),
            rec["oos_views"], now,
        ))
        n += 1
    con.commit()
    con.close()

    return {"ok": True, "rows_upserted": n, "date_range": {"start": start_s, "end": end_s}}


# ------------------------------------------------------------------ scoring
def assign_interest_levels(items: list[dict]) -> None:
    """High/medium/low interest = out_of_stock_view demand signal, relative to
    the current result set (top ~20% high, next ~30% medium, rest low).

    Based on event count alone (see module docstring for why page views and
    totalUsers aren't usable here). Percentile thresholds are computed live
    off whatever's in `items` rather than hardcoded absolute counts: OOS
    event volume varies a lot by category/date-range/filter, so a fixed
    "30 events = high" threshold either flags almost everything as high (as
    it did against the full catalog, where the median product already had
    ~44 events) or almost nothing once someone filters down to one category.
    Mutates each item in place, adding an "interest" key.
    """
    if not items:
        return
    views_sorted = sorted(i["oos_views"] for i in items)
    n = len(views_sorted)

    def percentile(p: float) -> float:
        idx = min(n - 1, int(n * p))
        return views_sorted[idx]

    high_cut = percentile(0.80)
    medium_cut = percentile(0.50)

    for item in items:
        v = item["oos_views"]
        if v > high_cut:
            item["interest"] = "high"
        elif v > medium_cut:
            item["interest"] = "medium"
        else:
            item["interest"] = "low"


# ------------------------------------------------------------------ API
@app.post("/api/refresh")
async def api_refresh(days: int = Query(30, ge=1, le=90)):
    try:
        result = await refresh_from_ga4(days=days)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/categories")
async def api_categories():
    con = db()
    rows = con.execute("SELECT DISTINCT category FROM oos_daily WHERE category IS NOT NULL ORDER BY category").fetchall()
    con.close()
    return [r["category"] for r in rows]


@app.get("/api/partners")
async def api_partners():
    con = db()
    rows = con.execute("SELECT DISTINCT partner_code FROM oos_daily WHERE partner_code IS NOT NULL ORDER BY partner_code").fetchall()
    con.close()
    return [r["partner_code"] for r in rows]


@app.get("/api/sources")
async def api_sources():
    return ["partner_central", "ecommerce"]


SORT_KEYS = {
    "product_code": lambda i: (i["product_code"] or ""),
    "product_name": lambda i: (i["product_name"] or ""),
    "partner_code": lambda i: (i["partner_code"] or ""),
    "category": lambda i: (i["category"] or ""),
    "oos_views": lambda i: i["oos_views"],
    "days_oos": lambda i: i["days_oos"],
    "first_oos_date": lambda i: (i["first_oos_date"] or ""),
    "last_oos_date": lambda i: (i["last_oos_date"] or ""),
    "interest": lambda i: {"low": 0, "medium": 1, "high": 2}[i["interest"]],
}


@app.get("/api/products")
async def api_products(
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    partner: str | None = None,
    source: str | None = None,
    interest: str | None = None,
    q: str | None = None,
    sort: str = Query("oos_views", pattern="^(" + "|".join(SORT_KEYS) + ")$"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    con = db()
    query = """SELECT product_code, MAX(product_name) product_name,
                      MAX(partner_code) partner_code, MAX(category) category,
                      MAX(source_type) source_type,
                      SUM(oos_views) oos_views,
                      SUM(CASE WHEN oos_views > 0 THEN 1 ELSE 0 END) days_oos,
                      MIN(CASE WHEN oos_views > 0 THEN date END) first_oos_date,
                      MAX(CASE WHEN oos_views > 0 THEN date END) last_oos_date
               FROM oos_daily WHERE 1=1"""
    params: list = []
    if start:
        query += " AND date >= ?"
        params.append(start)
    if end:
        query += " AND date <= ?"
        params.append(end)
    if category:
        query += " AND category = ?"
        params.append(category)
    if partner:
        query += " AND partner_code = ?"
        params.append(partner)
    if source:
        query += " AND source_type = ?"
        params.append(source)
    if q:
        query += " AND (product_code LIKE ? OR product_name LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    query += " GROUP BY product_code"

    rows = con.execute(query, params).fetchall()
    con.close()

    items = [{
        "product_code": r["product_code"],
        "product_name": r["product_name"],
        "partner_code": r["partner_code"],
        "category": r["category"],
        "source_type": r["source_type"],
        "oos_views": r["oos_views"],
        "days_oos": r["days_oos"],
        "first_oos_date": r["first_oos_date"],
        "last_oos_date": r["last_oos_date"],
    } for r in rows]

    # Interest is percentile-based within this filtered set (category/partner/
    # source/date/search), so it's computed BEFORE the interest filter itself
    # is applied - filtering first would re-percentile an already-filtered
    # set, which is circular (e.g. filtering to "low" would always leave ~50%
    # of what's left relabelled "high").
    assign_interest_levels(items)

    if interest:
        items = [i for i in items if i["interest"] == interest]

    items.sort(key=SORT_KEYS[sort], reverse=(dir == "desc"))
    total = len(items)
    page = items[offset:offset + limit]
    return {"count": total, "returned": len(page), "offset": offset, "limit": limit, "items": page}


@app.get("/api/trend/{product_code}")
async def api_trend(product_code: str, days: int = Query(60, ge=1, le=180)):
    con = db()
    rows = con.execute("""
        SELECT date, oos_views FROM oos_daily
        WHERE product_code = ?
        ORDER BY date ASC
        LIMIT ?
    """, (product_code, days)).fetchall()
    meta = con.execute("""
        SELECT product_name, partner_code, category, source_type FROM oos_daily WHERE product_code = ? LIMIT 1
    """, (product_code,)).fetchone()
    con.close()
    if not meta:
        raise HTTPException(status_code=404, detail="product not found")
    days_oos = sum(1 for r in rows if r["oos_views"] > 0)
    oos_dates = [r["date"] for r in rows if r["oos_views"] > 0]
    return {
        "product_code": product_code,
        "product_name": meta["product_name"],
        "partner_code": meta["partner_code"],
        "category": meta["category"],
        "source_type": meta["source_type"],
        "days_oos": days_oos,
        "first_oos_date": oos_dates[0] if oos_dates else None,
        "last_oos_date": oos_dates[-1] if oos_dates else None,
        "series": [{"date": r["date"], "oos_views": r["oos_views"]} for r in rows],
    }


@app.get("/api/status")
async def api_status():
    con = db()
    row = con.execute("SELECT MAX(updated_at) last_updated, COUNT(DISTINCT product_code) products, MIN(date) min_date, MAX(date) max_date FROM oos_daily").fetchone()
    con.close()
    return dict(row) if row else {}


# ------------------------------------------------------------------ static
@app.get("/")
async def index():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/app.js")
async def app_js():
    return Response((STATIC / "app.js").read_text(encoding="utf-8"), media_type="application/javascript")


@app.get("/style.css")
async def style_css():
    return Response((STATIC / "style.css").read_text(encoding="utf-8"), media_type="text/css")
