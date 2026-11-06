#!/usr/bin/env python3
"""
Pull daily product-code-wise sales from Ninja and push them to the dashboard.

Ninja (ninja.kapruka.com) is only reachable from this office machine, so the
VPS never calls it - this script does, one day at a time, and POSTs each day
to the dashboard's /api/sales-sync. The Revenue loss tab then sums whichever
date ranges you pick.

Source: Special Sales Monitor report (actions/reports/special_sale_monitor.jsp).
It returns Name (truncated with ".."), Product ID, Sale (units) and Stock for a
date range. No vendor, no selling price - so this feeds a units analysis.

Config: tools/.env.ninja (never committed; see .env.ninja.example)
    NINJA_USER, NINJA_PASS   Ninja basic-auth login
    VPS_URL                  e.g. https://pricelens.lanka.info/oos
    SYNC_TOKEN               must equal SYNC_TOKEN in the VPS's .env

Usage (from this folder or anywhere):
    python tools/ninja_sync.py                      # yesterday only
    python tools/ninja_sync.py --days 7             # last 7 days up to yesterday
    python tools/ninja_sync.py --start 2026-09-01 --end 2026-10-31
    python tools/ninja_sync.py --start 2026-10-15 --end 2026-10-15 --dry-run

Re-syncing a day replaces it on the server, so re-running is safe. Today is
skipped by default because the day isn't finished (use --include-today).
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = "https://ninja.kapruka.com/actions/reports/special_sale_monitor.jsp"

ROW_RE = re.compile(
    r"<td>\s*(?P<name>.*?)\s*</td>\s*<td><a[^>]*>\s*(?P<code>.*?)\s*</a></td>\s*"
    r"<td>\s*(?P<qty>[\d.\-]+)\s*</td>\s*<td>\s*(?P<stock>[\d.\-]+)\s*</td>",
    re.S,
)


def load_config() -> dict:
    path = HERE / ".env.ninja"
    if not path.exists():
        sys.exit(f"Missing {path} - copy .env.ninja.example to .env.ninja and fill it in.")
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    for k in ("NINJA_USER", "NINJA_PASS", "VPS_URL", "SYNC_TOKEN"):
        if not cfg.get(k):
            sys.exit(f"{k} is not set in {path}")
    cfg["VPS_URL"] = cfg["VPS_URL"].rstrip("/")
    return cfg


def fetch_day(cfg: dict, day: date, attempts: int = 3) -> list[dict]:
    url = f"{REPORT}?start_date={day}&end_date={day}&schedule=ALL"
    auth = base64.b64encode(f"{cfg['NINJA_USER']}:{cfg['NINJA_PASS']}".encode()).decode()
    last = None
    for n in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}", "User-Agent": "kapruka-oos-sync"})
            with urllib.request.urlopen(req, timeout=120) as r:
                page = r.read().decode("iso-8859-1")
            break
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(3 * (n + 1))
    else:
        raise RuntimeError(f"{day}: could not fetch Ninja report ({last})")
    if 'id="table1"' not in page:
        raise RuntimeError(f"{day}: unexpected Ninja response (login failed or report changed)")
    # Rows live in table1 only; table2 is a per-category summary.
    t1 = page[page.index('id="table1"'):]
    t1 = t1[:t1.index('id="table2"')] if 'id="table2"' in t1 else t1
    rows = []
    for m in ROW_RE.finditer(t1):
        rows.append({
            "code": html.unescape(m["code"]).strip(),
            "name": html.unescape(m["name"]).strip(),
            "qty": float(m["qty"]),
            "stock": float(m["stock"]),
        })
    return rows


def push(cfg: dict, day: date, rows: list[dict]) -> None:
    body = json.dumps({"days": [{"date": day.isoformat(), "rows": rows}]}).encode()
    req = urllib.request.Request(
        f"{cfg['VPS_URL']}/api/sales-sync", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Sync-Token": cfg["SYNC_TOKEN"], "User-Agent": "kapruka-oos-sync"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{day}: dashboard rejected the upload ({e.code} {e.read().decode(errors='replace')[:200]})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--days", type=int, help="last N days up to yesterday")
    ap.add_argument("--include-today", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="fetch and parse only; do not upload")
    a = ap.parse_args()

    today = date.today()
    last_day = today if a.include_today else today - timedelta(days=1)
    if a.start:
        start = date.fromisoformat(a.start)
        end = date.fromisoformat(a.end) if a.end else last_day
    elif a.days:
        end = last_day
        start = end - timedelta(days=a.days - 1)
    else:
        start = end = last_day
    if end > last_day:
        print(f"note: end {end} is not a finished day; capping at {last_day} (use --include-today to keep it)")
        end = last_day
    if start > end:
        sys.exit("nothing to do: start is after end")

    cfg = load_config()
    day, ok, failed = start, 0, []
    total_days = (end - start).days + 1
    while day <= end:
        idx = (day - start).days + 1
        try:
            rows = fetch_day(cfg, day)
            units = sum(r["qty"] for r in rows)
            if not a.dry_run:
                push(cfg, day, rows)
            print(f"[{idx}/{total_days}] {day}  {len(rows):>5} products  {units:>8,.0f} units  {'(dry run)' if a.dry_run else 'uploaded'}")
            ok += 1
        except Exception as e:  # keep going; report at the end
            print(f"[{idx}/{total_days}] {day}  FAILED: {e}")
            failed.append(day)
        day += timedelta(days=1)
    print(f"\nDone: {ok} day(s) ok, {len(failed)} failed" + (f" ({', '.join(map(str, failed))})" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
