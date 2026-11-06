#!/usr/bin/env python3
"""
Kapruka Out-of-Stock Dashboard - backend.

Pulls the GA4 `out_of_stock_view` event (fired when a shopper lands on a
product page that is marked out of stock).

Demand signal: out_of_stock_view event count, and only that. We looked for a
way to cross-reference regular page-view traffic per product (to tell
"nobody wants this anyway" apart from "people keep showing up for a product
they can't buy") but custom_param1/2 are event-scoped params that GA4 only
ever populates on out_of_stock_view - they come back "(not set)" on
page_view, so there is no per-product page-view figure to join against.
totalUsers on this event is also stuck at 1 even for products with 2000+
events, which points at a client_id/user_id gap in how the event is tagged
rather than 2000 real repeat visits - worth fixing at the source in GTM/GA4,
but out of scope here. Event count is therefore the only real signal
available, and it is reported raw (the OOS Views column). There used to be a
derived High/Medium/Low "Interest" column bucketing products by percentile
of that same count; it was removed on request, since it added no information
the raw count didn't already carry. Don't reintroduce a score - and
especially not a page-view ratio - the data can't support.

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

Caching: every read endpoint is a pure function of (the rows in oos_daily,
the filter arguments, today's recency cutoff), because the rows only change
when someone clicks "Refresh from GA4" (or runs the category backfill). So
results are memoised in-process (see the cache section below) and the cache
is dropped wholesale on any write, rather than guessed at with a short TTL.
On a real month (~490k rows / ~28k products) the per-product rollup is
~2.5s of SQLite work, and the UI fires it on every filter change, so without
this each filter touch cost a couple of seconds. Two details worth keeping:
the cached rollup is stored *unsorted* and sorted per request, so changing
sort or page is a cache hit; and cache keys carry both a data version
counter and the cutoff date, so no entry can outlive a refresh or a
midnight rollover. Entries are evicted by total rows held, not just entry
count - one unfiltered month rollup is ~20MB, so a plain 24-entry LRU of
those would be ~0.5GB on the VPS.

`days_oos` is a GA4-visit proxy for time out of stock: the count of days in
the selected window where the product had at least one out_of_stock_view
event. GA4 has no direct inventory feed, so a day with zero site traffic to
an OOS product isn't counted even if it was still unavailable that day.

Recency gate: the main report only ever lists products that are *still*
going out of stock. A product code needs at least one out_of_stock_view
dated within the last RECENCY_GATE_DAYS (default 2) calendar days, counted
back from TODAY (server local date) - NOT from the end of the selected date
range. The date range still governs which hits are counted/aggregated; the
gate only decides which product codes survive into the report at all. See
recency_cutoff_date() / fetch_filtered_products() for the implementation and
the GA4-lag caveat.

Back-in-stock view (the second dashboard tab, /api/back-in-stock): the
product codes the recency gate *drops* - i.e. they stopped firing
out_of_stock_view, which in practice means they came back in stock. Its
headline figure is how long each one was out of stock. Two different numbers
are reported and they are not interchangeable:
  - days_oos      - days with at least one out_of_stock_view event. Same
                    GA4-visit proxy caveat as above: a day with no traffic
                    to the product doesn't count even if it was unavailable.
  - oos_span_days - calendar days from first to last OOS hit, inclusive.
                    Always >= days_oos; the outer bracket of the outage.
That tab has its own date range (oos_start/oos_end), separate from the
active report's. With no range set, every figure is counted across ALL dates
held in the DB - a recovered product's outage can sit entirely outside any
recent window, so "all dates" is the only default that shows it at all. With
a range set, the range is what gets counted: days_oos, oos_span_days,
oos_views and the displayed first/last OOS dates are all computed from rows
inside those dates only, and a product is listed if it had any OOS hit in
there (overlap, not containment - an outage running past either edge is
trimmed to the range rather than dropped). This used to be containment on
first/last date with the figures still summed over all dates; it was changed
on request - a selected range now means "only these dates", which is what
reading a date filter naturally implies.
days_since_last_oos is the one figure that deliberately ignores the range:
it answers "how long has this been back in stock", so it is measured from
the product's true most recent OOS hit, not the last one inside the window
(which would say a product recovered weeks ago when the window simply ended
weeks ago).

days_since_last_oos ("back in stock for ~N days") is counted from today, so
it inherits the same caveat as the gate itself: if nobody has refreshed from
GA4 in a while, a product looks recovered simply because the data stopped.
The meta block returns data_stale for exactly that case and the UI warns on
it - don't read this tab as an inventory feed.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from pydantic import BaseModel

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

# Recency gate (see module docstring): how many calendar days back from today
# a product must have had an out_of_stock_view hit to appear in the report at
# all. 2 = today + yesterday. Env-overridable so the VPS can widen it without
# a code change if GA4 refreshes fall behind.
RECENCY_GATE_DAYS = max(1, int(os.environ.get("RECENCY_GATE_DAYS", "2")))

# Partner Central product codes look like ef_pc_<category>0v<partner#><pod|p><product#>[p]
# e.g. ef_pc_home0v2057pod00141p -> category "home", partner "v02057" (zero-padded to 5 digits)
PARTNER_CODE_RE = re.compile(r"^ef_pc_[a-z]+0v(\d+)", re.IGNORECASE)
CATEGORY_CODE_RE = re.compile(r"^ef_pc_([a-z]+?)0v\d+", re.IGNORECASE)

# Ecommerce (native Kapruka catalog) codes have no ef_pc_ prefix and no
# partner segment - just a category/merchant word directly followed by
# digits, e.g. grocery001737, pharmacy00844, pizzahut00114_tc1. The leading
# alphabetic run is the closest thing to a category GA4 gives us for these.
ECOMMERCE_CATEGORY_RE = re.compile(r"^([a-z]+)\d", re.IGNORECASE)

# Partner Central codes only ever carry the SHORT abbreviated form of a
# category (groc, phar, cosm, ...); ecommerce codes carry the full word
# (grocery, pharmacy, cosmetics, ...). Without this map the two sides never
# collide even though they're the same category - "groc" and "grocery" would
# sit as two separate filter options. Maps every known PC abbreviation to
# the canonical (ecommerce-style) full word so both sources merge into one
# category. Extend this as new ef_pc_ abbreviations show up.
CATEGORY_ALIASES = {
    "groc": "grocery",
    "phar": "pharmacy",
    "cosm": "cosmetics",
    "jewe": "jewellery",
    "jewllery": "jewellery",
    "spor": "sportsitem",
    "clot": "clothing",
    "bicy": "bicycle",
    "flow": "flowers",
    "perf": "perfume",
    "gift": "giftset",
    "hamp": "cphamper",
    "choc": "chocolates",
    "auto": "automobile",
}
# flow -> flowers, gift -> giftset, jewe -> jewellery are deliberate general
# merges even though other ecommerce variants exist (flowerswre, giftv/
# giftvoucher/giftwrap, jewelleryf/mh/rj/s/w) - those look like sub-brands
# or seller-specific codes under the same umbrella, not different
# categories, so they're left unmapped rather than guessed into one bucket.


def normalize_category(category: str | None) -> str | None:
    if not category:
        return category
    return CATEGORY_ALIASES.get(category.lower(), category.lower())

app = FastAPI(title="Kapruka Out-of-Stock Dashboard")

_resolved_dims: dict[str, str] = {}
_token_cache = {"token": None, "expiry": 0}

# A 30-day GA4 pull (paginated runReport calls, 60s httpx timeout each) can
# run past Cloudflare's edge timeout (~100s) or the origin IIS/ARR reverse
# proxy's own timeout - the browser then sees a bare 502/524 with no detail,
# even though the backend keeps going and finishes the work seconds later
# (confirmed live: /api/status showed fresh data right after a "failed"
# refresh, and a direct curl reproduced a 524 after ~130s). Running the pull
# as a background asyncio task and having the frontend poll
# /api/refresh/status instead of awaiting one long request sidesteps both
# proxies' timeouts - the GA4 pull itself is no faster, but nothing kills
# the connection out from under it mid-flight anymore.
_refresh_state: dict = {"status": "idle", "result": None, "error": None, "started_at": None, "finished_at": None}


# ------------------------------------------------------------------ cache
# See the "Caching" paragraph in the module docstring for why a plain
# in-process memo is the right shape here (data changes only on an explicit
# refresh, so there is nothing to poll for).
#
# TTL is a backstop, not the invalidation mechanism: correctness comes from
# _data_version (bumped on every write) and the cutoff date being part of
# every key. Set CACHE_TTL_SECONDS=0 to disable expiry entirely.
CACHE_TTL_SECONDS = float(os.environ.get("CACHE_TTL_SECONDS", "43200"))
CACHE_MAX_ENTRIES = max(1, int(os.environ.get("CACHE_MAX_ENTRIES", "48")))
# Row budget across the whole cache. A full unfiltered month rollup is ~28k
# product dicts (~20MB), so entry count alone is a bad size proxy.
CACHE_MAX_ROWS = max(1000, int(os.environ.get("CACHE_MAX_ROWS", "300000")))

_MISS = object()
_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_cache_rows = 0
_cache_lock = threading.Lock()  # these endpoints run in FastAPI's threadpool
_cache_stats = {"hits": 0, "misses": 0, "evictions": 0, "invalidations": 0}
# Bumped by invalidate_cache() on every write to oos_daily. Part of every
# cache key, so even a missed clear can't serve pre-refresh data.
_data_version = 0


def _drop(key: tuple) -> None:
    """Remove one entry and give its rows back to the budget. Lock held."""
    global _cache_rows
    entry = _cache.pop(key, None)
    if entry:
        _cache_rows -= entry["rows"]


def cache_get(key: tuple):
    """Cached value for `key`, or the _MISS sentinel (None is a valid value)."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            _cache_stats["misses"] += 1
            return _MISS
        if CACHE_TTL_SECONDS and (time.time() - entry["at"]) > CACHE_TTL_SECONDS:
            _drop(key)
            _cache_stats["misses"] += 1
            return _MISS
        _cache.move_to_end(key)
        _cache_stats["hits"] += 1
        return entry["value"]


def cache_put(key: tuple, value, rows: int = 1):
    """Store `value`, evicting least-recently-used entries until both the
    entry count and the row budget fit. `rows` is how many result rows the
    value holds, for the size budget."""
    global _cache_rows
    with _cache_lock:
        _drop(key)  # replacing: don't double-count its rows
        _cache[key] = {"value": value, "rows": rows, "at": time.time()}
        _cache_rows += rows
        while _cache and (len(_cache) > CACHE_MAX_ENTRIES or _cache_rows > CACHE_MAX_ROWS):
            oldest = next(iter(_cache))
            if oldest == key:
                break  # never evict the entry just written, even if oversized
            _drop(oldest)
            _cache_stats["evictions"] += 1
    return value


def invalidate_cache() -> int:
    """Drop everything and bump the data version. Called after any write to
    oos_daily (GA4 refresh, category backfill) - the data those entries were
    computed from no longer exists."""
    global _cache_rows, _data_version
    with _cache_lock:
        dropped = len(_cache)
        _cache.clear()
        _cache_rows = 0
        _data_version += 1
        _cache_stats["invalidations"] += 1
    return dropped


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


def _load_partner_names() -> dict[str, str]:
    """partner code (v04625) -> vendor name, from the Product-List report."""
    try:
        import json
        with open(Path(__file__).with_name("partner_names.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


PARTNER_NAMES = _load_partner_names()


def extract_category(product_code: str) -> str | None:
    """Category token out of the product code.

    Partner Central: ef_pc_home0v2057pod00141p -> 'home'.
    Ecommerce: grocery001737 -> 'grocery', pizzahut00114_tc1 -> 'pizzahut'.
    Ecommerce codes carry no ef_pc_ prefix or partner segment, so the leading
    alphabetic run before the first digit is the best category signal GA4
    gives us - for merchant-branded codes (pizzahut, kingsburyf) that ends up
    being the merchant name rather than a true category, but it's still a
    meaningful, filterable grouping.
    """
    if not product_code:
        return None
    m = CATEGORY_CODE_RE.match(product_code)
    if m:
        return normalize_category(m.group(1))
    m = ECOMMERCE_CATEGORY_RE.match(product_code)
    return normalize_category(m.group(1)) if m else None


def extract_source_type(product_code: str) -> str:
    """'partner_central' for ef_pc_* codes (third-party sellers via Partner
    Central), 'ecommerce' for everything else (Kapruka's own catalog, e.g.
    grocery001737, pharmacy00844)."""
    if product_code and product_code.lower().startswith("ef_pc_"):
        return "partner_central"
    return "ecommerce"


# ------------------------------------------------------------------ db
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    # WAL lets readers keep serving while a refresh writes. In the default
    # rollback-journal mode a big upsert locked the DB for minutes, so every
    # page load failed or hung until it finished. Persistent once set.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
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
    # Daily product-code-wise sales, pushed from the office machine by
    # tools/ninja_sync.py (see the "synced sales" section below).
    con.execute("""
    CREATE TABLE IF NOT EXISTS daily_sales (
        date TEXT NOT NULL,
        product_code TEXT NOT NULL,
        product_name TEXT,
        qty REAL DEFAULT 0,
        stock REAL,
        synced_at TEXT,
        PRIMARY KEY (product_code, date)
    )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_sales_date ON daily_sales(date)")
    # Days the dashboard needs but has no Ninja sales for yet. The office
    # machine's watcher (tools/ninja_sync.py --watch) polls these and fills them.
    con.execute("CREATE TABLE IF NOT EXISTS sales_requests (date TEXT PRIMARY KEY, requested_at TEXT)")
    # Key-product monitor: hand-pinned products, per-product alert state (so one
    # stock-out = one alert), and a log of every digest attempted.
    con.execute("CREATE TABLE IF NOT EXISTS key_pins (product_code TEXT PRIMARY KEY, added_at TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS key_state (
        product_code TEXT PRIMARY KEY, state TEXT NOT NULL, since TEXT, last_alert_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS alert_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT, count INTEGER,
        sent INTEGER, detail TEXT, products TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_oos_date ON oos_daily(date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_oos_partner ON oos_daily(partner_code)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_oos_category ON oos_daily(category)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_oos_source ON oos_daily(source_type)")
    # Covering index for the per-product rollup both tabs run. Leading
    # product_code means SQLite can GROUP BY straight off the index instead of
    # building a temp B-tree, and every column the rollup reads is in the
    # index, so it never touches the table. On a real month of data (~490k
    # rows / ~28k products) that took the rollup from 6.3s to 2.6s. It is
    # roughly table-sized on disk, which is the trade. Queries must say
    # INDEXED BY to get it - with a date filter present the planner otherwise
    # prefers idx_oos_date and pays for the temp B-tree.
    con.execute("""CREATE INDEX IF NOT EXISTS idx_oos_rollup
                   ON oos_daily(product_code, date, oos_views, category,
                                partner_code, source_type, product_name)""")
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


async def refresh_from_ga4(days: int = 30, start: str | None = None, end: str | None = None, intraday: bool = False) -> dict:
    """Pull out_of_stock_view rows from GA4 and upsert them into oos_daily.

    Default (no start/end): the last `days` days ending yesterday - what the
    dashboard's "Refresh Data" button does. Pass an explicit start/end
    (YYYY-MM-DD) to backfill a specific past window instead, e.g. a whole
    calendar month that has already ended; "last N days" can't address one
    of those once it has scrolled out of range. Upserts by (product_code,
    date), so re-pulling a window you already have is safe - it overwrites
    those days rather than double-counting them.
    """
    if not GA4_PROPERTY_ID or not GA4_REFRESH_TOKEN:
        raise RuntimeError("GA4 credentials not configured (set GA4_PROPERTY_ID, GA4_CLIENT_ID, GA4_CLIENT_SECRET, GA4_REFRESH_TOKEN)")

    token = await get_access_token()
    dims = await resolve_dimensions()

    from datetime import date, datetime, timedelta
    yesterday = datetime.utcnow().date() - timedelta(days=1)

    if start or end:
        # Explicit backfill window. GA4 has nothing for today yet (its own
        # ~1-day processing lag), so an end past yesterday is clamped rather
        # than silently returning a short window.
        try:
            start_d = date.fromisoformat(start) if start else None
            end_d = date.fromisoformat(end) if end else yesterday
        except ValueError:
            raise ValueError("start/end must be YYYY-MM-DD dates")
        if end_d > yesterday:
            end_d = yesterday
        if start_d is None:
            start_d = end_d - timedelta(days=days - 1)
        if start_d > end_d:
            raise ValueError(f"start ({start_d}) is after end ({end_d})")
        start_s, end_s = start_d.isoformat(), end_d.isoformat()
    else:
        end_d = yesterday
        start_d = end_d - timedelta(days=days - 1)
        start_s, end_s = start_d.isoformat(), end_d.isoformat()

    if intraday:
        # Hourly monitor: yesterday + today so far. GA4 accepts the literal
        # "today" and returns the hours processed so far; rows are upserted by
        # (product_code, date), so the partial day is simply overwritten by the
        # next pull and by the normal daily refresh.
        start_s, end_s = yesterday.isoformat(), "today"

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

    def write_rows() -> int:
        con = db()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        params = [
            (date_s, code, rec.get("product_name"),
             extract_partner_code(code), extract_category(code), extract_source_type(code),
             rec["oos_views"], now)
            for (date_s, code), rec in merged.items()
            if code and code != "(not set)"
        ]
        sql = """
            INSERT INTO oos_daily (date, product_code, product_name, partner_code, category, source_type, oos_views, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(product_code, date) DO UPDATE SET
                product_name=excluded.product_name,
                partner_code=excluded.partner_code,
                category=excluded.category,
                source_type=excluded.source_type,
                oos_views=excluded.oos_views,
                updated_at=excluded.updated_at
            WHERE oos_daily.oos_views IS NOT excluded.oos_views
               OR oos_daily.product_name IS NOT excluded.product_name
               OR oos_daily.category IS NOT excluded.category
               OR oos_daily.partner_code IS NOT excluded.partner_code
               OR oos_daily.source_type IS NOT excluded.source_type
        """
        # Re-pulling a window is mostly unchanged rows; skipping them avoids
        # rewriting six indexes for each (the 14-minute refresh).
        # Chunked commits keep each write transaction short.
        for i in range(0, len(params), 20000):
            con.executemany(sql, params[i:i + 20000])
            con.commit()
        con.close()
        return len(params)

    # Off the event loop: a big upsert used to freeze the whole server, so the
    # status polls timed out at the proxy (502/524) mid-refresh.
    n = await asyncio.to_thread(write_rows)

    # Everything cached was computed from the rows we just overwrote.
    invalidate_cache()
    threading.Thread(target=warm_cache, daemon=True).start()

    return {"ok": True, "rows_upserted": n, "date_range": {"start": start_s, "end": end_s}}


async def _run_refresh_in_background(days: int, start: str | None, end: str | None) -> None:
    _refresh_state["status"] = "running"
    _refresh_state["result"] = None
    _refresh_state["error"] = None
    _refresh_state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _refresh_state["finished_at"] = None
    try:
        result = await refresh_from_ga4(days=days, start=start, end=end)
        _refresh_state["status"] = "done"
        _refresh_state["result"] = result
    except Exception as e:
        # Covers both the ValueError the sync caller used to turn into a 400
        # (bad start/end) and anything else - there's no HTTP response left
        # to attach a status code to once this is running in the background,
        # so the message itself is what the frontend has to show.
        _refresh_state["status"] = "error"
        _refresh_state["error"] = str(e)
    finally:
        _refresh_state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------ API
@app.post("/api/refresh")
async def api_refresh(
    days: int = Query(30, ge=1, le=90),
    start: str | None = Query(None, description="Backfill window start, YYYY-MM-DD"),
    end: str | None = Query(None, description="Backfill window end, YYYY-MM-DD (clamped to yesterday)"),
):
    """Starts the GA4 pull in the background and returns immediately - see
    _refresh_state's comment above for why. Default pull is the last `days`
    days ending yesterday (the dashboard button). Pass start/end to backfill
    an explicit past window, e.g. ?start=2026-09-01&end=2026-09-30 for all of
    September - needed because "last N days" can't reach a month once it has
    scrolled past. Poll GET /api/refresh/status for progress/outcome."""
    if _refresh_state["status"] == "running":
        return {"ok": True, "status": "running", "note": "A refresh is already in progress."}
    asyncio.create_task(_run_refresh_in_background(days, start, end))
    return {"ok": True, "status": "started"}


@app.get("/api/refresh/status")
async def api_refresh_status():
    return _refresh_state


@app.post("/api/backfill-categories")
def api_backfill_categories():
    """Recompute category/source_type for every row already in the DB from
    extract_category()/extract_source_type() alone - no GA4 call. Lets a
    fixed extraction rule (e.g. the ecommerce category fallback) apply to
    rows synced before the fix, without waiting on a full GA4 re-pull."""
    con = db()
    codes = [r["product_code"] for r in con.execute("SELECT DISTINCT product_code FROM oos_daily").fetchall()]
    updated = 0
    for code in codes:
        con.execute(
            "UPDATE oos_daily SET category = ?, partner_code = ?, source_type = ? WHERE product_code = ?",
            (extract_category(code), extract_partner_code(code), extract_source_type(code), code),
        )
        updated += 1
    con.commit()
    con.close()
    invalidate_cache()  # category/partner/source on cached rows just changed
    return {"ok": True, "products_updated": updated}


@app.get("/api/categories")
def api_categories():
    key = ("categories", _data_version)
    hit = cache_get(key)
    if hit is not _MISS:
        return hit
    con = db()
    rows = con.execute("SELECT DISTINCT category FROM oos_daily WHERE category IS NOT NULL ORDER BY category").fetchall()
    con.close()
    return cache_put(key, [r["category"] for r in rows], rows=len(rows))


@app.get("/api/partners")
def api_partners():
    key = ("partners", _data_version)
    hit = cache_get(key)
    if hit is not _MISS:
        return hit
    con = db()
    rows = con.execute("SELECT DISTINCT partner_code FROM oos_daily WHERE partner_code IS NOT NULL ORDER BY partner_code").fetchall()
    con.close()
    return cache_put(key, [r["partner_code"] for r in rows], rows=len(rows))


@app.get("/api/partner-names")
def api_partner_names():
    return PARTNER_NAMES


@app.get("/api/sources")
def api_sources():
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
    "oos_span_days": lambda i: i["oos_span_days"],
}

# Back-in-stock tab carries two extra sortable columns the active report has
# no equivalent for (the span of the outage, and how long it's been over).
RECOVERED_SORT_KEYS = {
    **SORT_KEYS,
    "days_since_last_oos": lambda i: (i["days_since_last_oos"] if i["days_since_last_oos"] is not None else -1),
}


def recency_cutoff_date(days: int = RECENCY_GATE_DAYS) -> str:
    """Earliest date that still counts as "recent", anchored to TODAY.

    days=2 -> today and yesterday (2 calendar days, inclusive of today), i.e.
    cutoff = today - 1 day. Deliberately anchored to the server's current
    date, not to the selected range's end date: the point of the gate is
    "is this product still going out of stock right now", so browsing an
    older date range must not resurrect products that went quiet since.
    Calendar days rather than a rolling 48h because oos_daily only stores
    date-granularity rows - there are no event timestamps to do better.
    """
    from datetime import date, timedelta
    return (date.today() - timedelta(days=max(1, days) - 1)).isoformat()


def active_product_codes(con: sqlite3.Connection, cutoff: str) -> set[str]:
    """Product codes with at least one out_of_stock_view on/after `cutoff`.

    Pinned to the date index on purpose: the cutoff is only ever a 2-day
    slice, so narrowing by date first beats scanning the product-ordered
    covering index (0.46s vs 0.76s on a real month). The hint also stops the
    plan flip-flopping depending on whether ANALYZE has ever been run.

    Cached: every endpoint needs this set, it depends on nothing but the
    cutoff, and it is the same ~0.5s query each time. Callers must treat the
    returned set as read-only - it is the cached object itself.
    """
    key = ("active_codes", _data_version, cutoff)
    hit = cache_get(key)
    if hit is not _MISS:
        return hit
    rows = con.execute(
        "SELECT DISTINCT product_code FROM oos_daily INDEXED BY idx_oos_date"
        " WHERE date >= ? AND oos_views > 0",
        (cutoff,),
    ).fetchall()
    codes = {r["product_code"] for r in rows}
    return cache_put(key, codes, rows=len(codes))


def latest_data_date(con: sqlite3.Connection) -> str | None:
    """Newest date held in oos_daily - the "is this data stale?" reference."""
    key = ("latest_date", _data_version)
    hit = cache_get(key)
    if hit is not _MISS:
        return hit
    row = con.execute("SELECT MAX(date) d FROM oos_daily").fetchone()
    return cache_put(key, row["d"] if row else None)


def fetch_filtered_products(
    start: str | None, end: str | None, category: str | None, partner: str | None,
    source: str | None, q: str | None,
    sort: str, dir: str,
) -> tuple[list[dict], dict]:
    """Shared by /api/products (paginated) and /api/export (full CSV) so the
    export always matches exactly what the table's current filters show.

    Returns (items, recency_meta). Every product code in `items` has passed
    the recency gate, so both the table and the export apply it identically.

    The rollup itself is cached by filter args only (see _active_rollup);
    sorting happens here on a fresh list, so flipping a column or a page
    never re-runs the ~2.5s query. meta carries `cached`/`query_ms` for the
    UI's "served from cache" note.
    """
    t0 = time.perf_counter()
    cached_items, cached_meta, hit = _active_rollup(start, end, category, partner, source, q)
    items = sorted(cached_items, key=SORT_KEYS[sort], reverse=(dir == "desc"))
    meta = {**cached_meta, "cached": hit, "query_ms": round((time.perf_counter() - t0) * 1000, 1)}
    return items, meta


def matching_codes(q: str) -> set[str]:
    """Product codes whose code or name contains `q` (case-insensitive).

    Search used to push `LIKE '%q%'` into every rollup, which defeats the
    index and re-ran the full multi-second scan for every keystroke-distinct
    query. Instead the (code, name) list is read once per data version and
    the substring match runs in memory; the rollups are then filtered from
    their cached no-search results.
    """
    ql = q.lower()
    idx_key = ("product_index", _data_version)
    index = cache_get(idx_key)
    if index is _MISS:
        con = db()
        rows = con.execute(
            "SELECT product_code, MAX(product_name) n FROM oos_daily GROUP BY product_code"
        ).fetchall()
        con.close()
        index = cache_put(idx_key, [(r["product_code"], (r["n"] or "").lower()) for r in rows], rows=len(rows))
    return {c for c, n in index if ql in c.lower() or ql in n}


def _active_rollup(
    start: str | None, end: str | None, category: str | None, partner: str | None,
    source: str | None, q: str | None,
) -> tuple[list[dict], dict, bool]:
    """Unsorted, gated per-product rollup for the active report, memoised.

    Returns (items, recency_meta, was_cache_hit). The returned list and meta
    are the cached objects - callers must copy before mutating or sorting in
    place.
    """
    key = ("active_rollup", _data_version, recency_cutoff_date(),
           start, end, category, partner, source, q)
    hit = cache_get(key)
    if hit is not _MISS:
        return hit[0], hit[1], True

    if q:
        base_items, base_meta, base_hit = _active_rollup(start, end, category, partner, source, None)
        m = matching_codes(q)
        items = [i for i in base_items if i["product_code"] in m]
        meta = {**base_meta, "hidden_stale": 0, "matched_before_gate": len(items)}
        cache_put(key, (items, meta), rows=len(items))
        return items, meta, False

    con = db()
    query = """SELECT product_code, MAX(product_name) product_name,
                      MAX(partner_code) partner_code, MAX(category) category,
                      MAX(source_type) source_type,
                      SUM(oos_views) oos_views,
                      SUM(CASE WHEN oos_views > 0 THEN 1 ELSE 0 END) days_oos,
                      MIN(CASE WHEN oos_views > 0 THEN date END) first_oos_date,
                      MAX(CASE WHEN oos_views > 0 THEN date END) last_oos_date
               FROM oos_daily INDEXED BY idx_oos_rollup WHERE 1=1"""
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

    cutoff = recency_cutoff_date()
    active = active_product_codes(con, cutoff)
    newest = latest_data_date(con)
    con.close()

    def span_days(r) -> int:
        # Same rule as the back-in-stock tab: inclusive first->last OOS hit.
        span = _days_between(r["first_oos_date"], r["last_oos_date"])
        return (span + 1) if span is not None else (r["days_oos"] or 0)

    items = [{
        "product_code": r["product_code"],
        "product_name": r["product_name"],
        "partner_code": r["partner_code"],
        "partner_name": PARTNER_NAMES.get(r["partner_code"] or ""),
        "category": r["category"],
        "source_type": r["source_type"],
        "oos_views": r["oos_views"],
        "days_oos": r["days_oos"],
        "oos_span_days": span_days(r),
        "first_oos_date": r["first_oos_date"],
        "last_oos_date": r["last_oos_date"],
    } for r in rows]

    # Recency gate: a stale product isn't part of the report at all. The date
    # range still decides which hits were summed above - the gate only drops
    # whole product codes that have gone quiet in the last RECENCY_GATE_DAYS.
    matched_before_gate = len(items)
    items = [i for i in items if i["product_code"] in active]
    recency = {
        "days": RECENCY_GATE_DAYS,
        "cutoff": cutoff,
        "hidden_stale": matched_before_gate - len(items),
        "matched_before_gate": matched_before_gate,
        "latest_data_date": newest,
        # True when the DB itself has no data as recent as the cutoff (e.g.
        # nobody has hit "Refresh from GA4" for a few days, or GA4's own
        # ~1-day reporting lag) - in that case the report is empty because
        # the data is stale, not because every product came back in stock.
        "data_stale": bool(newest and newest < cutoff),
    }

    cache_put(key, (items, recency), rows=len(items))
    return items, recency, False


def _days_between(earlier: str | None, later: str | None) -> int | None:
    """Inclusive calendar-day difference between two YYYY-MM-DD strings."""
    if not earlier or not later:
        return None
    from datetime import date
    try:
        a = date.fromisoformat(earlier)
        b = date.fromisoformat(later)
    except ValueError:
        return None
    return (b - a).days


def validate_window(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    """Sanity-check the back-in-stock tab's outage window.

    Blank strings from an emptied date input mean "no bound", not a bad date.
    A non-date string would otherwise compare as a plain string against the
    YYYY-MM-DD dates in SQLite and silently return the wrong set, so it's a
    400 instead.
    """
    from datetime import date
    out: list[str | None] = []
    for v in (start, end):
        v = (v or "").strip()
        if not v:
            out.append(None)
            continue
        try:
            date.fromisoformat(v)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"oos window dates must be YYYY-MM-DD (got {v!r})")
        out.append(v)
    if out[0] and out[1] and out[0] > out[1]:
        raise HTTPException(status_code=400, detail=f"oos window start ({out[0]}) is after end ({out[1]})")
    return out[0], out[1]


def fetch_recovered_products(
    category: str | None, partner: str | None, source: str | None, q: str | None,
    sort: str, dir: str,
    oos_start: str | None = None, oos_end: str | None = None,
) -> tuple[list[dict], dict]:
    """The inverse of fetch_filtered_products(): product codes the recency
    gate excludes, i.e. no out_of_stock_view since the cutoff, which in
    practice means they're back in stock.

    Deliberately ignores the toolbar's date range and aggregates over every
    date in the DB. A recovered product's whole point is the outage that
    already ended, so clipping it to the last 30 days would under-report
    days_oos (and would hide any product whose outage ended before the
    window entirely). Both duration figures are returned - days_oos (days
    that actually fired events) and oos_span_days (first->last calendar
    span) - because they differ whenever the product had a quiet day, and
    only the span brackets the real outage.

    oos_start/oos_end are this tab's own date range, and when set they scope
    every figure: days_oos, oos_span_days, oos_views and the first/last OOS
    dates are all counted from rows inside the range only, and a product
    qualifies if it had any OOS hit in there. The window is applied inside
    the aggregates rather than as a WHERE, so the same single pass can also
    carry last_oos_any - the product's true most recent OOS date across all
    dates - which days_since_last_oos needs ("back in stock for N days" has
    to be measured from the real last hit, not from wherever the window was
    cut).

    Cached the same way as the active report: the rollup is memoised unsorted
    by filter args (see _recovered_rollup) and sorted per request here, so
    re-sorting or paging costs nothing.
    """
    t0 = time.perf_counter()
    cached_items, cached_meta, hit = _recovered_rollup(category, partner, source, q,
                                                       oos_start, oos_end)
    items = sorted(cached_items, key=RECOVERED_SORT_KEYS[sort], reverse=(dir == "desc"))
    meta = {**cached_meta, "cached": hit, "query_ms": round((time.perf_counter() - t0) * 1000, 1)}
    return items, meta


def _recovered_rollup(
    category: str | None, partner: str | None, source: str | None, q: str | None,
    oos_start: str | None, oos_end: str | None,
) -> tuple[list[dict], dict, bool]:
    """Unsorted back-in-stock rollup, memoised. Returns the cached list/meta
    objects - copy before mutating."""
    key = ("recovered_rollup", _data_version, recency_cutoff_date(),
           category, partner, source, q, oos_start, oos_end)
    hit = cache_get(key)
    if hit is not _MISS:
        return hit[0], hit[1], True

    if q:
        base_items, base_meta, base_hit = _recovered_rollup(category, partner, source, None, oos_start, oos_end)
        m = matching_codes(q)
        items = [i for i in base_items if i["product_code"] in m]
        meta = {**base_meta, "recovered": len(items)}
        cache_put(key, (items, meta), rows=len(items))
        return items, meta, False

    con = db()
    # Window predicate, repeated inside each aggregate. Empty when no range is
    # set, in which case every figure covers all synced dates as before.
    in_window = ""
    window_params: list = []
    if oos_start:
        in_window += " AND date >= ?"
        window_params.append(oos_start)
    if oos_end:
        in_window += " AND date <= ?"
        window_params.append(oos_end)

    query = f"""SELECT product_code, MAX(product_name) product_name,
                      MAX(partner_code) partner_code, MAX(category) category,
                      MAX(source_type) source_type,
                      SUM(CASE WHEN oos_views > 0{in_window} THEN oos_views ELSE 0 END) oos_views,
                      SUM(CASE WHEN oos_views > 0{in_window} THEN 1 ELSE 0 END) days_oos,
                      MIN(CASE WHEN oos_views > 0{in_window} THEN date END) first_oos_date,
                      MAX(CASE WHEN oos_views > 0{in_window} THEN date END) last_oos_date,
                      MAX(CASE WHEN oos_views > 0 THEN date END) last_oos_any
               FROM oos_daily INDEXED BY idx_oos_rollup WHERE 1=1"""
    # SQLite binds `?` by position in the SQL text, so the four windowed
    # aggregates above consume window_params four times, before any WHERE arg.
    params: list = window_params * 4
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
    # Products with no OOS day inside the window drop out here rather than in
    # Python, so a narrow range doesn't carry ~28k empty rollups back.
    query += " GROUP BY product_code HAVING days_oos > 0"

    rows = con.execute(query, params).fetchall()

    cutoff = recency_cutoff_date()
    active = active_product_codes(con, cutoff)
    newest = latest_data_date(con)
    con.close()

    from datetime import date
    today = date.today().isoformat()

    items = []
    for r in rows:
        if r["product_code"] in active:
            continue  # still going out of stock - belongs on the main report
        if not r["days_oos"]:
            continue  # only ever recorded zero-view rows; nothing to report
        span = _days_between(r["first_oos_date"], r["last_oos_date"])
        items.append({
            "product_code": r["product_code"],
            "product_name": r["product_name"],
            "partner_code": r["partner_code"],
            "partner_name": PARTNER_NAMES.get(r["partner_code"] or ""),
            "category": r["category"],
            "source_type": r["source_type"],
            "oos_views": r["oos_views"],
            "days_oos": r["days_oos"],
            # Inclusive of both ends: a single-day outage spans 1 day, not 0.
            "oos_span_days": (span + 1) if span is not None else r["days_oos"],
            "first_oos_date": r["first_oos_date"],
            "last_oos_date": r["last_oos_date"],
            # From the real last hit, not the window's last hit - see the
            # function docstring. Falls back to the in-window date if the
            # unwindowed aggregate is somehow NULL.
            "days_since_last_oos": _days_between(r["last_oos_any"] or r["last_oos_date"], today),
        })

    meta = {
        "days": RECENCY_GATE_DAYS,
        "cutoff": cutoff,
        "recovered": len(items),
        "latest_data_date": newest,
        # Echoed back so the UI can say which dates the figures were counted
        # over (all synced dates when both are null).
        "oos_start": oos_start,
        "oos_end": oos_end,
        # Same trap as the main report, pointed the other way: if the DB
        # hasn't been refreshed past the cutoff, EVERY product looks
        # recovered because the data stops, not because stock came back.
        "data_stale": bool(newest and newest < cutoff),
    }

    cache_put(key, (items, meta), rows=len(items))
    return items, meta, False


@app.get("/api/products")
def api_products(
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    partner: str | None = None,
    source: str | None = None,
    q: str | None = None,
    sort: str = Query("oos_views", pattern="^(" + "|".join(SORT_KEYS) + ")$"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    items, recency = fetch_filtered_products(start, end, category, partner, source, q, sort, dir)
    # `count` is the post-gate total, so pagination and any "N products"
    # summary reflect exactly the rows the report actually contains.
    total = len(items)
    page = items[offset:offset + limit]
    return {
        "count": total, "returned": len(page), "offset": offset, "limit": limit,
        "items": page, "recency": recency,
    }


@app.get("/api/tab-counts")
def api_tab_counts(
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    partner: str | None = None,
    source: str | None = None,
    q: str | None = None,
    oos_start: str | None = None,
    oos_end: str | None = None,
    need: str = Query("both", pattern="^(both|active|recovered)$"),
):
    """How many products sit on each tab under the current filters, for the
    tab badges.

    Deliberately not "call the other tab's endpoint and read its count": that
    re-runs the full per-product rollup (~2.4s on a real month) just to get
    one number. These are DISTINCT-product_code queries the indexes satisfy
    outright. `need` skips the half you already know - the tab you're on
    reports its own exact total from its own response, so the UI only ever
    asks for the other one (~1.3s instead of ~2.2s).

    `start`/`end` scope the active count only - matching the active tab,
    which honours the date range. The recovered count ignores them and
    honours `oos_start`/`oos_end` (the back-in-stock tab's own date range)
    instead, counting any product with an OOS hit inside those dates, so the
    badge never disagrees with the row count on the tab itself.

    Each half is cached separately under its own filter key, because the UI
    asks for them at different times (`need`) and with different date args -
    one shared key would miss constantly.
    """
    oos_start, oos_end = validate_window(oos_start, oos_end)
    con = db()

    def filter_sql() -> tuple[str, list]:
        sql, params = "", []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if partner:
            sql += " AND partner_code = ?"
            params.append(partner)
        if source:
            sql += " AND source_type = ?"
            params.append(source)
        return sql, params

    shared_sql, shared_params = filter_sql()
    cutoff = recency_cutoff_date()
    active = active_product_codes(con, cutoff)

    out: dict = {"cutoff": cutoff}
    cached = True  # flipped false as soon as either half has to hit SQLite

    if need in ("both", "active"):
        # Active tab: codes with a hit inside the date range that also pass the gate.
        key = ("count_active", _data_version, cutoff, start, end, category, partner, source, q)
        hit = cache_get(key)
        if hit is not _MISS:
            out["active"] = hit
        else:
            set_key = ("count_active_set", _data_version, start, end, category, partner, source)
            in_range = cache_get(set_key)
            cached = False
            sql = "SELECT DISTINCT product_code FROM oos_daily INDEXED BY idx_oos_rollup WHERE oos_views > 0"
            params: list = []
            if start:
                sql += " AND date >= ?"
                params.append(start)
            if end:
                sql += " AND date <= ?"
                params.append(end)
            sql += shared_sql
            params += shared_params
            if in_range is _MISS:
                in_range = cache_put(set_key, {r["product_code"] for r in con.execute(sql, params).fetchall()}, rows=1000)
            out["active"] = cache_put(key, len(in_range & active & (matching_codes(q) if q else in_range)))

    if need in ("both", "recovered"):
        # Back-in-stock tab: codes out of stock at some point inside that tab's
        # date range (all synced dates when it's unset) that no longer pass the
        # gate. A plain date-bounded DISTINCT, matching the tab's own overlap
        # rule - products are trimmed to the range now, not dropped for
        # straddling its edges.
        key = ("count_recovered", _data_version, cutoff, oos_start, oos_end,
               category, partner, source, q)
        hit = cache_get(key)
        if hit is not _MISS:
            out["recovered"] = hit
        else:
            cached = False
            rec_sql = ("SELECT DISTINCT product_code FROM oos_daily INDEXED BY idx_oos_rollup"
                       " WHERE oos_views > 0")
            rec_params: list = []
            if oos_start:
                rec_sql += " AND date >= ?"
                rec_params.append(oos_start)
            if oos_end:
                rec_sql += " AND date <= ?"
                rec_params.append(oos_end)
            rec_sql += shared_sql
            rec_params += shared_params
            set_key = ("count_ever_set", _data_version, oos_start, oos_end, category, partner, source)
            ever = cache_get(set_key)
            if ever is _MISS:
                ever = cache_put(set_key, {r["product_code"] for r in con.execute(rec_sql, rec_params).fetchall()}, rows=1000)
            if q:
                ever = ever & matching_codes(q)
            out["recovered"] = cache_put(key, len(ever - active))
        out["oos_start"] = oos_start
        out["oos_end"] = oos_end

    con.close()
    out["cached"] = cached
    return out


@app.get("/api/back-in-stock")
def api_back_in_stock(
    category: str | None = None,
    partner: str | None = None,
    source: str | None = None,
    q: str | None = None,
    oos_start: str | None = Query(None, description="Count OOS days from this date, YYYY-MM-DD"),
    oos_end: str | None = Query(None, description="Count OOS days up to this date, YYYY-MM-DD"),
    sort: str = Query("days_oos", pattern="^(" + "|".join(RECOVERED_SORT_KEYS) + ")$"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """Products the recency gate removed from the main report - they stopped
    firing out_of_stock_view, so they're presumed back in stock - with how
    long each was out of stock while it lasted.

    oos_start/oos_end restrict every figure to those dates: Days OOS, OOS
    Span, OOS Views and the first/last dates shown are all counted from hits
    inside the range only, and any product with a hit in the range is listed.
    Unset = all synced dates. "Back in stock for N days" is the exception and
    always runs off the product's true last OOS hit."""
    oos_start, oos_end = validate_window(oos_start, oos_end)
    items, meta = fetch_recovered_products(category, partner, source, q, sort, dir,
                                          oos_start=oos_start, oos_end=oos_end)
    total = len(items)
    page = items[offset:offset + limit]
    return {
        "count": total, "returned": len(page), "offset": offset, "limit": limit,
        "items": page, "recency": meta,
    }


@app.get("/api/export")
def api_export(
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    partner: str | None = None,
    source: str | None = None,
    q: str | None = None,
    oos_start: str | None = None,
    oos_end: str | None = None,
    sort: str = Query("oos_views"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    view: str = Query("active", pattern="^(active|back_in_stock)$"),
):
    """CSV of whichever tab the user is on: the active report (default) or the
    back-in-stock list. Same fetch call as the matching API endpoint, so the
    CSV is always exactly the filtered rows on screen."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    source_labels = {"partner_central": "Partner Central", "ecommerce": "Ecommerce"}

    if view == "back_in_stock":
        if sort not in RECOVERED_SORT_KEYS:
            sort = "days_oos"
        # Not the active tab's date range: this tab has its own
        # oos_start/oos_end, which scopes every figure to those dates (all
        # synced dates when unset), so the CSV matches the rows on screen.
        oos_start, oos_end = validate_window(oos_start, oos_end)
        items, _ = fetch_recovered_products(category, partner, source, q, sort, dir,
                                            oos_start=oos_start, oos_end=oos_end)
        writer.writerow([
            "Product Code", "Product Name", "Partner Code", "Partner Name", "Category", "Source",
            "Days OOS (days with events)", "OOS Span (calendar days)", "OOS Views",
            "Back In Stock For (days)",
        ])
        for i in items:
            writer.writerow([
                i["product_code"], i["product_name"] or "",
                i["partner_code"] or "", i.get("partner_name") or "", i["category"] or "",
                source_labels.get(i["source_type"], i["source_type"] or ""),
                i["days_oos"], i["oos_span_days"], i["oos_views"],
                i["days_since_last_oos"] if i["days_since_last_oos"] is not None else "",
            ])
        filename = f"kapruka-back-in-stock-{time.strftime('%Y%m%d')}.csv"
    else:
        if sort not in SORT_KEYS:
            sort = "oos_views"
        # Same call as /api/products, so the CSV is exactly the gated+filtered
        # set the table shows; the recency meta itself is UI-only, hence dropped.
        items, _ = fetch_filtered_products(start, end, category, partner, source, q, sort, dir)
        # Column order mirrors the dashboard table exactly.
        writer.writerow([
            "Product Code", "Product Name", "Partner Code", "Partner Name", "Category",
            "Source", "OOS Views", "Days OOS", "OOS Span (calendar days)",
        ])
        for i in items:
            writer.writerow([
                i["product_code"], i["product_name"] or "",
                i["partner_code"] or "", i.get("partner_name") or "", i["category"] or "",
                source_labels.get(i["source_type"], i["source_type"] or ""),
                i["oos_views"], i["days_oos"], i["oos_span_days"],
            ])
        filename = f"kapruka-out-of-stock-{time.strftime('%Y%m%d')}.csv"

    return Response(
        content="﻿" + buf.getvalue(),  # BOM so Excel opens UTF-8 correctly
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/trend/{product_code}")
def api_trend(product_code: str, days: int = Query(60, ge=1, le=180)):
    """Daily out_of_stock_view counts for one product, for the modal's chart.

    The series is zero-filled across every calendar day in the window, not
    just the days that have rows. oos_daily only stores days GA4 returned
    events for, so the raw rows skip quiet days entirely - plotted as-is they
    put 08-01 next to 08-10 as if they were consecutive, which reads as a
    continuous run and is the whole reason the chart is per-day bars now. A
    zero day means "no out_of_stock_view recorded", which is either back in
    stock or simply no traffic - the same GA4-visit-proxy caveat as days_oos.

    The window is the last `days` calendar days ending at the product's own
    last OOS date, not at today: a recovered product's outage can be weeks
    old, and anchoring to today would show it an empty chart. (This also
    replaces a `LIMIT days` on the rows, which silently returned the product's
    *oldest* `days` rows once it had more than that.)
    """
    con = db()
    rows = con.execute("""
        SELECT date, oos_views FROM oos_daily
        WHERE product_code = ? AND oos_views > 0
        ORDER BY date ASC
    """, (product_code,)).fetchall()
    meta = con.execute("""
        SELECT product_name, partner_code, category, source_type FROM oos_daily WHERE product_code = ? LIMIT 1
    """, (product_code,)).fetchone()
    con.close()
    if not meta:
        raise HTTPException(status_code=404, detail="product not found")

    from datetime import date, timedelta
    by_date = {r["date"]: r["oos_views"] for r in rows}
    oos_dates = sorted(by_date)
    series = []
    if oos_dates:
        last_d = date.fromisoformat(oos_dates[-1])
        first_d = date.fromisoformat(oos_dates[0])
        # Never start before the first hit - no point padding a product's
        # pre-history with zeros - but cap the span at `days` bars.
        start_d = max(first_d, last_d - timedelta(days=days - 1))
        d = start_d
        while d <= last_d:
            iso = d.isoformat()
            series.append({"date": iso, "oos_views": by_date.get(iso, 0)})
            d += timedelta(days=1)

    return {
        "product_code": product_code,
        "product_name": meta["product_name"],
        "partner_code": meta["partner_code"],
        "category": meta["category"],
        "source_type": meta["source_type"],
        # Counted over the product's whole history, matching the table's
        # Days OOS; the series may be clipped to the last `days` days.
        "days_oos": len(oos_dates),
        "first_oos_date": oos_dates[0] if oos_dates else None,
        "last_oos_date": oos_dates[-1] if oos_dates else None,
        "window_days": len(series),
        "series": series,
    }


@app.get("/api/status")
def api_status():
    cutoff = recency_cutoff_date()
    key = ("status", _data_version, cutoff)
    hit = cache_get(key)
    if hit is not _MISS:
        return hit
    con = db()
    row = con.execute("SELECT MAX(updated_at) last_updated, COUNT(DISTINCT product_code) products, MIN(date) min_date, MAX(date) max_date FROM oos_daily").fetchone()
    active = len(active_product_codes(con, cutoff))
    con.close()
    out = dict(row) if row else {}
    # `products` is every code ever synced; `active_products` is the subset
    # that passes the recency gate and can therefore appear in the report.
    out["active_products"] = active
    out["recency_days"] = RECENCY_GATE_DAYS
    out["recency_cutoff"] = cutoff
    return cache_put(key, out)


def warm_cache() -> None:
    """Pre-run the queries the page fires on load (default last-30-day
    window), so the first visitor after a restart or refresh doesn't wait
    10s+ on cold queries. Best effort."""
    try:
        from datetime import date, timedelta
        end = (date.today() - timedelta(days=1)).isoformat()
        start = (date.today() - timedelta(days=30)).isoformat()
        api_status()
        api_categories()
        api_partners()
        matching_codes("zzzz")
        _active_rollup(start, end, None, None, None, None)
        _recovered_rollup(None, None, None, None, None, None)
        api_tab_counts(start=start, end=end, need="both")
    except Exception:
        pass


@app.on_event("startup")
async def _warm_on_startup():
    threading.Thread(target=warm_cache, daemon=True).start()


@app.get("/api/cache")
async def api_cache():
    """What the in-process cache currently holds. Diagnostic only - useful for
    checking a slow dashboard is actually missing rather than re-querying
    because something bumped the data version."""
    with _cache_lock:
        return {
            "entries": len(_cache),
            "rows_held": _cache_rows,
            "max_entries": CACHE_MAX_ENTRIES,
            "max_rows": CACHE_MAX_ROWS,
            "ttl_seconds": CACHE_TTL_SECONDS,
            "data_version": _data_version,
            **_cache_stats,
        }


@app.post("/api/cache/clear")
async def api_cache_clear():
    """Force a cold read on the next request. Only needed if data.db is
    modified out-of-band (e.g. a manual SQLite edit on the VPS) - the GA4
    refresh and the category backfill already clear it themselves."""
    return {"ok": True, "entries_dropped": invalidate_cache()}


# ------------------------------------------------------------------ synced sales
# Ninja (the order-admin site) is only reachable from the office machine, so
# the VPS never calls it. tools/ninja_sync.py runs there, pulls the Special
# Sales Monitor report one day at a time, and POSTs each day here. The revenue
# tab then adds up whatever two date ranges the user picks. The report gives
# product code, a (truncated) name and the quantity sold - no vendor and no
# selling price - so analyses built on it are in units, not revenue.
#
# Writes need SYNC_TOKEN (.env on the VPS, same value in the sync script's
# .env.ninja). With no SYNC_TOKEN set the endpoint is disabled, not open.
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "")


class SalesDay(BaseModel):
    date: str
    rows: list[dict]


class SalesSyncRequest(BaseModel):
    days: list[SalesDay]


@app.post("/api/sales-sync")
async def api_sales_sync(req: SalesSyncRequest, x_sync_token: str | None = Header(None)):
    import hmac
    from datetime import date as _date
    if not SYNC_TOKEN:
        raise HTTPException(status_code=503, detail="sales sync is disabled (SYNC_TOKEN is not set on the server)")
    if not x_sync_token or not hmac.compare_digest(x_sync_token, SYNC_TOKEN):
        raise HTTPException(status_code=401, detail="bad sync token")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con = db()
    out = []
    try:
        for d in req.days:
            try:
                _date.fromisoformat(d.date)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"bad date {d.date!r}")
            recs = {}
            for r in d.rows:
                code = str(r.get("code", "")).strip()
                if not code:
                    continue
                try:
                    qty = float(r.get("qty", 0))
                except (TypeError, ValueError):
                    continue
                prev = recs.get(code)
                recs[code] = (d.date, code, (str(r.get("name") or "").strip() or None),
                              (prev[3] if prev else 0) + qty, r.get("stock"), now)
            # A re-sync of a day replaces it, so a corrected report never doubles up.
            con.execute("DELETE FROM daily_sales WHERE date = ?", (d.date,))
            con.execute("DELETE FROM sales_requests WHERE date = ?", (d.date,))
            con.executemany("INSERT INTO daily_sales (date, product_code, product_name, qty, stock, synced_at) VALUES (?,?,?,?,?,?)",
                            [(a, b, c, q, st, n) for (a, b, c, q, st, n) in recs.values()])
            out.append({"date": d.date, "products": len(recs)})
        con.commit()
    finally:
        con.close()
    return {"ok": True, "days": out}


@app.get("/api/sales-coverage")
async def api_sales_coverage():
    con = db()
    row = con.execute("SELECT MIN(date) AS lo, MAX(date) AS hi, COUNT(DISTINCT date) AS days, MAX(synced_at) AS last FROM daily_sales").fetchone()
    pending = con.execute("SELECT COUNT(*) AS n FROM sales_requests").fetchone()["n"]
    con.close()
    return {"from": row["lo"], "to": row["hi"], "days": row["days"], "last_synced": row["last"],
            "pending_requests": pending, "watcher_last_seen": _watcher_seen.get("ts"),
            "watcher_age_s": int(time.time() - _watcher_seen["t"]) if _watcher_seen.get("t") else None}


_watcher_seen: dict = {}


class SalesRangeRequest(BaseModel):
    start: str
    end: str


@app.post("/api/sales-request")
async def api_sales_request(req: SalesRangeRequest):
    """The picked date range needs sales days we don't have yet: queue them for
    the office machine's watcher. Today and later are never requested (the day
    isn't finished). Returns the missing days so the page can wait on them."""
    from datetime import date as _d, timedelta
    start, end = validate_window(req.start, req.end)
    if not start or not end:
        raise HTTPException(status_code=400, detail="start and end are required")
    last = min(_d.fromisoformat(end), local_today() - timedelta(days=1))
    first = _d.fromisoformat(start)
    if (last - first).days > 400:
        raise HTTPException(status_code=400, detail="range too long (max 400 days)")
    con = db()
    try:
        have = {r["date"] for r in con.execute("SELECT DISTINCT date FROM daily_sales WHERE date BETWEEN ? AND ?", (start, last.isoformat()))}
        missing, d = [], first
        while d <= last:
            if d.isoformat() not in have:
                missing.append(d.isoformat())
            d += timedelta(days=1)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        con.executemany("INSERT OR IGNORE INTO sales_requests (date, requested_at) VALUES (?, ?)", [(m, now) for m in missing])
        con.commit()
    finally:
        con.close()
    return {"missing": missing, "watcher_age_s": int(time.time() - _watcher_seen["t"]) if _watcher_seen.get("t") else None}


@app.get("/api/sales-requests")
async def api_sales_requests(x_sync_token: str | None = Header(None)):
    """What the watcher should fetch next (oldest first). Polling also acts as
    its heartbeat, so the page can tell whether anyone is listening."""
    import hmac
    if not SYNC_TOKEN:
        raise HTTPException(status_code=503, detail="sales sync is disabled (SYNC_TOKEN is not set on the server)")
    if not x_sync_token or not hmac.compare_digest(x_sync_token, SYNC_TOKEN):
        raise HTTPException(status_code=401, detail="bad sync token")
    _watcher_seen["t"] = time.time()
    _watcher_seen["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    con = db()
    rows = con.execute("SELECT date FROM sales_requests ORDER BY date LIMIT 60").fetchall()
    con.close()
    return {"dates": [r["date"] for r in rows]}


@app.post("/api/ga4-ensure")
async def api_ga4_ensure(req: SalesRangeRequest):
    """Make sure GA4 out-of-stock data covers the picked range. If it doesn't,
    start a background backfill of the range (the VPS can reach GA4 itself) and
    let the page poll /api/refresh/status. Coverage is judged on the first/last
    synced date; today is never pulled here (the hourly monitor owns that)."""
    from datetime import date as _d, timedelta
    start, end = validate_window(req.start, req.end)
    if not start or not end:
        raise HTTPException(status_code=400, detail="start and end are required")
    end_eff = min(_d.fromisoformat(end), _d.today() - timedelta(days=1)).isoformat()
    if start > end_eff:
        return {"covered": True, "started": False}
    con = db()
    cov = con.execute("SELECT MIN(date) AS lo, MAX(date) AS hi FROM oos_daily").fetchone()
    con.close()
    if cov["lo"] and start >= cov["lo"] and end_eff <= cov["hi"]:
        return {"covered": True, "started": False}
    if _refresh_state["status"] == "running":
        return {"covered": False, "started": False, "running": True}
    if not (GA4_PROPERTY_ID and GA4_REFRESH_TOKEN):
        raise HTTPException(status_code=503, detail="GA4 is not configured on the server")
    asyncio.create_task(_run_refresh_in_background(30, start, end_eff))
    return {"covered": False, "started": True, "range": [start, end_eff]}


@app.get("/api/sales-range")
async def api_sales_range(start: str, end: str):
    """Quantity sold per product code between two dates (inclusive), summed
    over the synced days, plus how many of those days actually have data."""
    from datetime import date as _date
    start, end = validate_window(start, end)
    if not start or not end:
        raise HTTPException(status_code=400, detail="start and end are required")
    con = db()
    rows = con.execute("""
        SELECT product_code AS code, SUM(qty) AS qty, MAX(product_name) AS name
        FROM daily_sales WHERE date BETWEEN ? AND ? GROUP BY product_code
    """, (start, end)).fetchall()
    have = con.execute("SELECT COUNT(DISTINCT date) AS n FROM daily_sales WHERE date BETWEEN ? AND ?", (start, end)).fetchone()["n"]
    con.close()
    return {
        "start": start, "end": end,
        "days_requested": (_date.fromisoformat(end) - _date.fromisoformat(start)).days + 1,
        "days_synced": have,
        "rows": [dict(r) for r in rows],
    }


# ------------------------------------------------------------------ revenue loss
# Third tab. The user uploads two monthly sales extracts (Product Code, Product
# Name, Vendor, Product Qty, Total Selling price) and we answer: of the
# products that sold in month 1, which were out of stock for at least N days in month 2, and how much revenue did
# that cost?
#
#   revenue lost = month-1 total selling price - month-2 total selling price
#
# (qty drop if the files carry no revenue column). Only positive losses are
# listed. The OOS evidence is the GA4 out_of_stock_view data over the chosen
# date range and is NOT recency-gated: a product restocked since then still
# lost sales while it was out. It is an upper bound on what OOS cost - a
# product can fall for other reasons and also be OOS briefly - so the days
# out of stock are shown beside every figure.
#
# Files are parsed in the browser and arrive as JSON rows, so no multipart or
# openpyxl dependency. Nothing is stored.
class RevRow(BaseModel):
    code: str
    name: str | None = None
    vendor: str | None = None
    m1: float = 0
    m2: float = 0
    r1: float | None = None
    r2: float | None = None


class RevenueLossRequest(BaseModel):
    rows: list[RevRow]
    oos_start: str
    oos_end: str
    min_oos_days: int = 1


EXCLUDED_FILE = ROOT / "excluded_products.json"


def load_excluded_products() -> set[str]:
    """Codes/names listed in excluded_products.json are dropped from the
    revenue analysis entirely (not ranked, not counted, not exported). Read per
    request so a git pull takes effect without a restart."""
    try:
        import json
        data = json.loads(EXCLUDED_FILE.read_text(encoding="utf-8"))
        return {str(c).strip().lower() for c in data.get("codes", []) if str(c).strip()}
    except (OSError, ValueError):
        return set()


@app.post("/api/revenue-loss")
async def api_revenue_loss(req: RevenueLossRequest):
    from datetime import date
    oos_start, oos_end = validate_window(req.oos_start, req.oos_end)
    if not oos_start or not oos_end:
        raise HTTPException(status_code=400, detail="OOS date range (oos_start and oos_end) is required")
    if len(req.rows) > 300_000:
        raise HTTPException(status_code=400, detail="too many rows (max 300,000)")
    min_oos_days = max(1, req.min_oos_days)

    # The same code can repeat in an extract - sum it, keep the first name/vendor.
    excluded = load_excluded_products()
    merged: dict[str, dict] = {}
    has_rev = False
    for r in req.rows:
        code = r.code.strip()
        if not code:
            continue
        if code.lower() in excluded or (r.name or "").strip().lower() in excluded:
            continue
        m = merged.setdefault(code, {"name": None, "vendor": None, "m1": 0.0, "m2": 0.0, "r1": 0.0, "r2": 0.0})
        m["m1"] += r.m1
        m["m2"] += r.m2
        if r.r1 is not None or r.r2 is not None:
            has_rev = True
            m["r1"] += r.r1 or 0
            m["r2"] += r.r2 or 0
        m["name"] = m["name"] or (r.name or "").strip() or None
        m["vendor"] = m["vendor"] or (r.vendor or "").strip() or None

    # Every product that sold in month 1 is "active" and gets checked for OOS.
    cand = []
    for code, m in merged.items():
        base1, base2 = (m["r1"], m["r2"]) if has_rev else (m["m1"], m["m2"])
        if base1 <= 0:
            continue
        lost = base1 - base2
        cand.append((code, m, base1, base2, lost, lost / base1 * 100))

    con = db()
    evidence: dict[str, sqlite3.Row] = {}
    codes = [c[0] for c in cand]
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in con.execute(f"""
            SELECT product_code, MAX(product_name) AS product_name,
                   COUNT(DISTINCT date) AS oos_days, SUM(oos_views) AS oos_views,
                   MIN(date) AS first_oos, MAX(date) AS last_oos
            FROM oos_daily
            WHERE date BETWEEN ? AND ? AND oos_views > 0 AND product_code IN ({marks})
            GROUP BY product_code
        """, [oos_start, oos_end, *chunk]):
            evidence[row["product_code"]] = row
    cov = con.execute("SELECT MIN(date) AS lo, MAX(date) AS hi FROM oos_daily").fetchone()
    con.close()

    window_days = (date.fromisoformat(oos_end) - date.fromisoformat(oos_start)).days + 1
    data_lo, data_hi = cov["lo"], cov["hi"]
    # How much of the range the synced GA4 data spans - OOS evidence outside it
    # is simply absent, which would read as "never out of stock".
    if data_lo and data_hi:
        lo, hi = max(oos_start, data_lo), min(oos_end, data_hi)
        covered_days = max(0, (date.fromisoformat(hi) - date.fromisoformat(lo)).days + 1)
    else:
        covered_days = 0

    items = []
    went_oos = 0
    ranked_base1 = sum(c[2] for c in cand)
    for code, m, base1, base2, lost, drop_pct in cand:
        ev = evidence.get(code)
        oos_days = ev["oos_days"] if ev else 0
        if oos_days < min_oos_days:
            continue
        went_oos += 1
        if lost <= 0:
            continue    # went out of stock but sold as much or more - no loss
        items.append({
            "product_code": code,
            # Ninja truncates long names with ".."; GA4's copy is the full one.
            "product_name": (ev["product_name"] if ev and ev["product_name"] and (not m["name"] or m["name"].endswith(".."))
                             else m["name"]),
            "vendor": m["vendor"] or "Unknown",
            "category": extract_category(code) or "other",
            "qty_drop": m["m1"] - m["m2"],
            "qty_drop_pct": round((m["m1"] - m["m2"]) / m["m1"] * 100, 1) if m["m1"] > 0 else None,
            "oos_days": oos_days,
            "oos_views": ev["oos_views"] if ev else 0,
            "lost": lost,
            "drop_pct": round(drop_pct, 1),
            "m1": m["m1"], "m2": m["m2"], "r1": m["r1"] if has_rev else None, "r2": m["r2"] if has_rev else None,
            "first_oos": ev["first_oos"] if ev else None,
            "last_oos": ev["last_oos"] if ev else None,
        })
    items.sort(key=lambda i: -i["lost"])
    return {
        "window": {"start": oos_start, "end": oos_end, "days": window_days,
                   "data_from": data_lo, "data_to": data_hi, "covered_days": covered_days},
        "params": {"min_oos_days": min_oos_days},
        "has_revenue": has_rev,
        "totals": {"uploaded": len(merged), "ranked": len(cand), "went_oos": went_oos,
                   "with_loss": len(items), "lost": sum(i["lost"] for i in items),
                   "ranked_base1": ranked_base1},
        "items": items,
    }


# ------------------------------------------------------------------ key products
# Key products = the best sellers in the synced Ninja daily sales: the union of
# the top KEY_TOP_N by units over the last 7 days and over the last 14 days
# (ending at the latest synced day), plus anything pinned by hand. Every hour
# the monitor pulls today's GA4 out_of_stock_view events and, when a key
# product gets its first out-of-stock hit of the day, sends ONE WhatsApp digest
# to the admin(s). Detection is "shoppers landed on its out-of-stock page" -
# the same signal as the rest of this dashboard - so it is only as fast as the
# product has traffic, which a top seller does.
#
# One stock-out = one alert: key_state remembers a product as 'oos' once it was
# alerted, and re-arms it after no hits for today and yesterday. If the send
# fails (or WhatsApp is not configured) the state is NOT advanced, so the same
# products are retried next hour instead of being silently dropped.
MONITOR_ENABLED = os.environ.get("MONITOR_ENABLED", "1") != "0"
MONITOR_INTERVAL_MIN = max(5, int(os.environ.get("MONITOR_INTERVAL_MIN", "60")))
KEY_TOP_N = max(1, int(os.environ.get("KEY_TOP_N", "100")))
# GA4 reports dates in the property's timezone; Sri Lanka is UTC+5:30.
ALERT_TZ_HOURS = float(os.environ.get("ALERT_TZ_OFFSET_HOURS", "5.5"))
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "https://pricelens.lanka.info/oos/")

# WhatsApp Cloud API (Meta). WHATSAPP_TO may hold several numbers, comma-
# separated, in international format without "+". Free text only reaches a
# number that messaged the business in the last 24h; for a dependable alert
# create an approved template with one body variable and set WHATSAPP_TEMPLATE.
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_TO = os.environ.get("WHATSAPP_TO", "")
WHATSAPP_TEMPLATE = os.environ.get("WHATSAPP_TEMPLATE", "")
WHATSAPP_TEMPLATE_LANG = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en_US")
WHATSAPP_GRAPH_VERSION = os.environ.get("WHATSAPP_GRAPH_VERSION", "v21.0")

_monitor_state: dict = {"running": False, "last_run": None, "last_result": None,
                        "last_error": None, "next_run": None}
_last_test_sent = 0.0


def local_today():
    from datetime import datetime, timedelta
    return (datetime.utcnow() + timedelta(hours=ALERT_TZ_HOURS)).date()


def whatsapp_configured() -> bool:
    return bool(WHATSAPP_PHONE_ID and WHATSAPP_ACCESS_TOKEN and WHATSAPP_TO)


def compute_key_products(con: sqlite3.Connection, top_n: int | None = None):
    """-> (items, latest_sales_date). Items are sorted by 7-day units."""
    from datetime import date, timedelta
    top_n = top_n or KEY_TOP_N
    pins = {r["product_code"] for r in con.execute("SELECT product_code FROM key_pins")}
    latest = con.execute("SELECT MAX(date) AS d FROM daily_sales").fetchone()["d"]
    rows = []
    if latest:
        ld = date.fromisoformat(latest)
        d7 = (ld - timedelta(days=6)).isoformat()
        d14 = (ld - timedelta(days=13)).isoformat()
        rows = con.execute("""
            SELECT product_code, MAX(product_name) AS name,
                   SUM(CASE WHEN date >= ? THEN qty ELSE 0 END) AS u7, SUM(qty) AS u14
            FROM daily_sales WHERE date BETWEEN ? AND ? GROUP BY product_code
        """, (d7, d14, latest)).fetchall()
        excluded = load_excluded_products()
        rows = [r for r in rows if r["product_code"].lower() not in excluded]
    by7 = sorted(rows, key=lambda r: -r["u7"])
    top7 = {r["product_code"] for r in by7[:top_n] if r["u7"] > 0}
    top14 = {r["product_code"] for r in sorted(rows, key=lambda r: -r["u14"])[:top_n] if r["u14"] > 0}
    rank = {r["product_code"]: i + 1 for i, r in enumerate(by7)}
    byc = {r["product_code"]: r for r in rows}
    items = []
    for code in top7 | top14 | pins:
        r = byc.get(code)
        why = [w for w, ok in (("top 7d", code in top7), ("top 14d", code in top14), ("pinned", code in pins)) if ok]
        items.append({
            "product_code": code, "name": r["name"] if r else None,
            "u7": r["u7"] if r else 0, "u14": r["u14"] if r else 0,
            "per_day": round(r["u7"] / 7, 1) if r else 0,
            "rank": rank.get(code), "pinned": code in pins, "why": why,
        })
    items.sort(key=lambda i: -i["u7"])
    return items, latest


def oos_status_for(con: sqlite3.Connection, codes: list[str], today) -> dict[str, dict]:
    """Out-of-stock evidence for specific codes over the last 7 days."""
    from datetime import timedelta
    d7 = (today - timedelta(days=6)).isoformat()
    t = today.isoformat()
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in con.execute(f"""
            SELECT product_code, MAX(product_name) AS pname,
                   SUM(CASE WHEN date = ? THEN oos_views ELSE 0 END) AS hits_today,
                   MAX(date) AS last_hit, COUNT(DISTINCT date) AS days7
            FROM oos_daily WHERE date >= ? AND oos_views > 0 AND product_code IN ({marks})
            GROUP BY product_code
        """, [t, d7, *chunk]):
            out[r["product_code"]] = dict(r)
    return out


async def send_whatsapp(single_line: str, multi_line: str) -> tuple[bool, str]:
    if not whatsapp_configured():
        return False, "WhatsApp is not configured on the server"
    recipients = [x.strip() for x in WHATSAPP_TO.split(",") if x.strip()]
    url = f"https://graph.facebook.com/{WHATSAPP_GRAPH_VERSION}/{WHATSAPP_PHONE_ID}/messages"
    problems, ids = [], []
    async with httpx.AsyncClient(timeout=30) as c:
        for to in recipients:
            if WHATSAPP_TEMPLATE:
                # Template variables may not contain newlines, hence the one-liner.
                body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                        "template": {"name": WHATSAPP_TEMPLATE, "language": {"code": WHATSAPP_TEMPLATE_LANG},
                                     "components": [{"type": "body", "parameters": [{"type": "text", "text": single_line[:900]}]}]}}
            else:
                body = {"messaging_product": "whatsapp", "to": to, "type": "text",
                        "text": {"body": multi_line[:3900], "preview_url": False}}
            try:
                r = await c.post(url, headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}, json=body)
                if r.status_code >= 300:
                    problems.append(f"{to}: {r.status_code} {r.text[:200]}")
                else:
                    try:
                        ids.append(r.json()["messages"][0]["id"][-14:])
                    except (ValueError, KeyError, IndexError):
                        pass
            except Exception as e:
                problems.append(f"{to}: {e}")
    return (not problems), ("; ".join(problems) if problems else f"accepted by Meta for {len(recipients)} recipient(s)" + (f" (id …{ids[0]})" if ids else ""))


def current_oos_key_items() -> list[dict]:
    """Key products with an out-of-stock hit today, biggest sellers first."""
    today = local_today()
    con = db()
    try:
        items, _ = compute_key_products(con)
        status = oos_status_for(con, [i["product_code"] for i in items], today)
    finally:
        con.close()
    out = []
    for it in items:
        st = status.get(it["product_code"])
        if st and st["hits_today"] > 0:
            if st.get("pname") and (not it["name"] or it["name"].endswith("..")):
                it["name"] = st["pname"]
            out.append({**it, "hits_today": st["hits_today"]})
    return sorted(out, key=lambda i: -i["u7"])


def _evaluate_keys() -> dict:
    """Work out which key products newly went out of stock / recovered. Does not
    write the 'oos' state - the caller does that only once the alert was sent."""
    from datetime import timedelta
    today = local_today()
    yesterday = (today - timedelta(days=1)).isoformat()
    con = db()
    try:
        items, latest = compute_key_products(con)
        status = oos_status_for(con, [i["product_code"] for i in items], today)
        state = {r["product_code"]: r["state"] for r in con.execute("SELECT product_code, state FROM key_state")}
        new_oos, recovered = [], []
        for it in items:
            st = status.get(it["product_code"])
            if st and st.get("pname") and (not it["name"] or it["name"].endswith("..")):
                it["name"] = st["pname"]
            hit_today = bool(st and st["hits_today"] > 0)
            prev = state.get(it["product_code"])
            if hit_today and prev != "oos":
                new_oos.append({**it, "hits_today": st["hits_today"]})
            elif prev == "oos" and not (st and st["last_hit"] >= yesterday):
                recovered.append(it["product_code"])
        if recovered:
            con.executemany("UPDATE key_state SET state='ok', since=? WHERE product_code=?",
                            [(today.isoformat(), c) for c in recovered])
            con.commit()
        oos_now = sum(1 for it in items if status.get(it["product_code"], {}).get("hits_today", 0) > 0)
        return {"key_count": len(items), "oos_now": oos_now, "new_oos": new_oos,
                "recovered": len(recovered), "sales_through": latest}
    finally:
        con.close()


def _commit_alerted(codes: list[str]) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    today = local_today().isoformat()
    con = db()
    try:
        con.executemany("""INSERT INTO key_state (product_code, state, since, last_alert_at) VALUES (?, 'oos', ?, ?)
                           ON CONFLICT(product_code) DO UPDATE SET state='oos', since=excluded.since, last_alert_at=excluded.last_alert_at""",
                        [(c, today, now) for c in codes])
        con.commit()
    finally:
        con.close()


def _log_alert(kind: str, count: int, sent: bool, detail: str, products: list[str]) -> None:
    import json
    con = db()
    try:
        con.execute("INSERT INTO alert_log (ts, kind, count, sent, detail, products) VALUES (?,?,?,?,?,?)",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), kind, count, 1 if sent else 0, detail[:500], json.dumps(products[:50])))
        con.commit()
    finally:
        con.close()


def build_digest(new_oos: list[dict], prefix: str = "") -> tuple[str, str]:
    shown = sorted(new_oos, key=lambda i: -i["u7"])[:15]
    more = len(new_oos) - len(shown)
    def label(i):
        return f"{i['name'] or i['product_code']} ({i['product_code']}) - {i['per_day']:g}/day"
    multi = [f"{prefix}⚠️ {len(new_oos)} key product{'s' if len(new_oos) != 1 else ''} out of stock"]
    multi += [f"{n}. {label(i)}" for n, i in enumerate(shown, 1)]
    if more > 0:
        multi.append(f"+{more} more")
    multi.append(APP_PUBLIC_URL)
    single = f"{prefix}{len(new_oos)} key product{'s' if len(new_oos) != 1 else ''} out of stock: " + " | ".join(label(i) for i in shown) + (f" | +{more} more" if more > 0 else "")
    return single, "\n".join(multi)


async def monitor_check(pull: bool = True, notify: bool = True) -> dict:
    if _monitor_state["running"]:
        return {"skipped": "a check is already running"}
    _monitor_state["running"] = True
    result: dict = {}
    try:
        if pull:
            if _refresh_state["status"] == "running":
                result["pull"] = "skipped (a full refresh is running)"
            elif not (GA4_PROPERTY_ID and GA4_REFRESH_TOKEN):
                result["pull"] = "skipped (GA4 not configured)"
            else:
                try:
                    r = await refresh_from_ga4(intraday=True)
                    result["pull"] = {"rows": r["rows_upserted"], "range": r["date_range"]}
                except Exception as e:
                    result["pull"] = {"error": str(e)}
        ev = await asyncio.to_thread(_evaluate_keys)
        new_oos = ev.pop("new_oos")
        result.update(ev)
        result["new_oos"] = len(new_oos)
        if new_oos and notify:
            single, multi = build_digest(new_oos)
            ok, detail = await send_whatsapp(single, multi)
            codes = [i["product_code"] for i in new_oos]
            await asyncio.to_thread(_log_alert, "oos", len(new_oos), ok, detail, codes)
            if ok:
                await asyncio.to_thread(_commit_alerted, codes)
            result["alert"] = {"sent": ok, "detail": detail}
        _monitor_state["last_error"] = None
    except Exception as e:
        _monitor_state["last_error"] = str(e)
        result["error"] = str(e)
    finally:
        _monitor_state["running"] = False
        _monitor_state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _monitor_state["last_result"] = result
    return result


async def _monitor_loop() -> None:
    from datetime import datetime, timedelta
    await asyncio.sleep(90)   # let the server finish starting and warm its cache
    while True:
        await monitor_check(pull=True, notify=True)
        _monitor_state["next_run"] = (datetime.now() + timedelta(minutes=MONITOR_INTERVAL_MIN)).strftime("%Y-%m-%d %H:%M:%S")
        await asyncio.sleep(MONITOR_INTERVAL_MIN * 60)


@app.on_event("startup")
async def _start_monitor():
    if MONITOR_ENABLED and GA4_PROPERTY_ID:
        asyncio.create_task(_monitor_loop())


@app.get("/api/key-products")
async def api_key_products(top_n: int | None = Query(None, ge=1, le=1000)):
    from datetime import timedelta
    today = local_today()
    yesterday = (today - timedelta(days=1)).isoformat()
    con = db()
    try:
        items, latest = compute_key_products(con, top_n)
        status = oos_status_for(con, [i["product_code"] for i in items], today)
        state = {r["product_code"]: dict(r) for r in con.execute("SELECT * FROM key_state")}
        alerts = [dict(r) for r in con.execute("SELECT id, ts, kind, count, sent, detail FROM alert_log ORDER BY id DESC LIMIT 10")]
    finally:
        con.close()
    for it in items:
        st = status.get(it["product_code"]) or {}
        it["hits_today"] = st.get("hits_today", 0)
        it["days_oos_7d"] = st.get("days7", 0)
        it["last_hit"] = st.get("last_hit")
        if st.get("pname") and (not it["name"] or it["name"].endswith("..")):
            it["name"] = st["pname"]   # GA4 has the full name; Ninja truncates with ".."
        it["status"] = "oos" if it["hits_today"] > 0 else ("recent" if st.get("last_hit") == yesterday else "ok")
        ks = state.get(it["product_code"])
        it["alerted"] = bool(ks and ks["state"] == "oos")
        it["last_alert_at"] = ks["last_alert_at"] if ks else None
    stale_days = None
    if latest:
        from datetime import date as _d
        stale_days = (today - _d.fromisoformat(latest)).days
    return {
        "items": items,
        "meta": {
            "top_n": top_n or KEY_TOP_N, "sales_through": latest, "sales_age_days": stale_days,
            "today": today.isoformat(), "key_count": len(items),
            "oos_now": sum(1 for i in items if i["status"] == "oos"),
            "pinned": sum(1 for i in items if i["pinned"]),
            "whatsapp": {"configured": whatsapp_configured(),
                         "mode": "template" if WHATSAPP_TEMPLATE else "text",
                         "recipients": len([x for x in WHATSAPP_TO.split(",") if x.strip()])},
            "monitor": {"enabled": MONITOR_ENABLED and bool(GA4_PROPERTY_ID), "interval_min": MONITOR_INTERVAL_MIN,
                        "running": _monitor_state["running"], "last_run": _monitor_state["last_run"],
                        "next_run": _monitor_state["next_run"], "last_error": _monitor_state["last_error"],
                        "last_result": _monitor_state["last_result"]},
        },
        "alerts": alerts,
    }


class PinRequest(BaseModel):
    code: str
    pinned: bool = True


@app.post("/api/key-products/pin")
async def api_key_pin(req: PinRequest):
    code = req.code.strip()
    if not code or len(code) > 200:
        raise HTTPException(status_code=400, detail="product code required")
    con = db()
    try:
        if req.pinned:
            con.execute("INSERT OR IGNORE INTO key_pins (product_code, added_at) VALUES (?, ?)", (code, time.strftime("%Y-%m-%d %H:%M:%S")))
        else:
            con.execute("DELETE FROM key_pins WHERE product_code = ?", (code,))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "code": code, "pinned": req.pinned}


@app.post("/api/monitor/check")
async def api_monitor_check(pull: bool = True):
    """Run the hourly check now. Idempotent: it only alerts for key products
    that have not already been alerted for this stock-out."""
    return await monitor_check(pull=pull, notify=True)


@app.post("/api/monitor/test")
async def api_monitor_test():
    """Send a TEST message with the key products that are out of stock right
    now (same format as a real alert), so the WhatsApp setup and the message
    can be checked. It does not mark anything as alerted. Rate-limited,
    because this dashboard has no login."""
    global _last_test_sent
    if time.time() - _last_test_sent < 60:
        raise HTTPException(status_code=429, detail="a test message was just sent - wait a minute")
    _last_test_sent = time.time()
    items = await asyncio.to_thread(current_oos_key_items)
    if items:
        single, multi = build_digest(items, prefix="🧪 TEST - ")
    else:
        single = multi = "🧪 TEST - no key products are out of stock right now. Alerts are set up."
    ok, detail = await send_whatsapp(single, multi)
    await asyncio.to_thread(_log_alert, "test", len(items), ok, detail, [i["product_code"] for i in items])
    return {"sent": ok, "detail": detail, "products": len(items)}


# ------------------------------------------------------------------ static
@app.get("/")
async def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # revenue.js is served by the /static mount, which sets no Cache-Control, so
    # Cloudflare caches it for hours. A hand-bumped ?v=N went stale once (new
    # page + cached old script = a crashed upload box), so stamp the URL with
    # the file's mtime instead: a new deploy always means a new URL.
    rev = STATIC / "revenue.js"
    if rev.exists():
        html = re.sub(r"static/revenue\.js\?v=\w+", f"static/revenue.js?v={int(rev.stat().st_mtime)}", html)
    kp = STATIC / "keyprod.js"
    if kp.exists():
        html = re.sub(r"static/keyprod\.js\?v=\w+", f"static/keyprod.js?v={int(kp.stat().st_mtime)}", html)
    css = STATIC / "style.css"
    if css.exists():
        html = re.sub(r"style\.css\?v=\w+", f"style.css?v={int(css.stat().st_mtime)}", html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/app.js")
async def app_js():
    return Response((STATIC / "app.js").read_text(encoding="utf-8"), media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/style.css")
async def style_css():
    return Response((STATIC / "style.css").read_text(encoding="utf-8"), media_type="text/css", headers={"Cache-Control": "no-cache"})
