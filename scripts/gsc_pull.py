"""Loop B input: Google Search Console + CrUX pull.

Pure API client (no browser). Service-account auth, read-only scope. Produces the
measured half of a weekly row:
  - per_url:   Search Analytics by page (current window vs the prior window), with the
               position/CTR/impression/click deltas that Loop B ranks on, plus each page's
               top queries and its indexing status (URL Inspection API).
  - cwv_field: CrUX field Core Web Vitals buckets (Good / Needs-Improvement / Poor) per URL.

Writes those into the current seo_weekly row via db.upsert_weekly (partial upsert — the
Loop B agent later fills opportunities / editorial_gaps on the same row). The agent never
touches Postgres directly; db.py stays the single writer.

CLI:
    python -m scripts.gsc_pull --site signalsanctuary --dry-run
    python -m scripts.gsc_pull --site signalsanctuary --window-days 7 --lag-days 3
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

from . import db
from .audit import resolve_targets
from .config import env, load_site_config, site_slug

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
CRUX_ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"

# p75 thresholds (ms except CLS) → Good / Needs-Improvement / Poor
CWV_THRESHOLDS = {
    "largest_contentful_paint": (2500, 4000),
    "cumulative_layout_shift": (0.10, 0.25),
    "interaction_to_next_paint": (200, 500),
}


# --- auth / service ---------------------------------------------------------

def get_service():
    sa_path = env("GSC_SERVICE_ACCOUNT_JSON", required=True)
    creds = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


# --- date windows -----------------------------------------------------------

def windows(window_days: int, lag_days: int) -> dict[str, tuple[str, str]]:
    """Current N-day window (ending `lag_days` back for GSC freshness) and the prior N days."""
    today = dt.date.today()
    cur_end = today - dt.timedelta(days=lag_days)
    cur_start = cur_end - dt.timedelta(days=window_days - 1)
    prior_end = cur_start - dt.timedelta(days=1)
    prior_start = prior_end - dt.timedelta(days=window_days - 1)
    iso = lambda d: d.isoformat()
    return {
        "current": (iso(cur_start), iso(cur_end)),
        "prior": (iso(prior_start), iso(prior_end)),
    }


# --- Search Analytics -------------------------------------------------------

def search_analytics(service, prop: str, start: str, end: str) -> list[dict]:
    rows: list[dict] = []
    start_row = 0
    while True:
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": ["page", "query"],
            "rowLimit": 25000,
            "startRow": start_row,
        }
        resp = service.searchanalytics().query(siteUrl=prop, body=body).execute()
        batch = resp.get("rows", [])
        for r in batch:
            page, query = r["keys"]
            rows.append({
                "page": page, "query": query,
                "clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0),
                "ctr": r.get("ctr", 0.0), "position": r.get("position", 0.0),
            })
        if len(batch) < 25000:
            break
        start_row += 25000
    return rows


def _agg_page(rows: list[dict]) -> dict[str, dict]:
    """Collapse page+query rows to per-page totals (impression-weighted position) + top queries."""
    by_page: dict[str, dict] = defaultdict(
        lambda: {"clicks": 0, "impressions": 0, "pos_num": 0.0, "queries": []}
    )
    for r in rows:
        p = by_page[r["page"]]
        p["clicks"] += r["clicks"]
        p["impressions"] += r["impressions"]
        p["pos_num"] += r["position"] * r["impressions"]  # weight by impressions
        p["queries"].append((r["query"], r["impressions"], r["position"]))
    out = {}
    for page, p in by_page.items():
        impr = p["impressions"]
        out[page] = {
            "clicks": p["clicks"],
            "impressions": impr,
            "ctr": round(p["clicks"] / impr, 4) if impr else 0.0,
            "position": round(p["pos_num"] / impr, 2) if impr else None,
            "top_queries": [
                {"query": q, "impressions": i, "position": round(pos, 2)}
                for q, i, pos in sorted(p["queries"], key=lambda x: -x[1])[:5]
            ],
        }
    return out


def build_per_url(cur_rows: list[dict], prior_rows: list[dict]) -> list[dict]:
    cur, prior = _agg_page(cur_rows), _agg_page(prior_rows)
    per_url = []
    for page in sorted(set(cur) | set(prior)):
        c = cur.get(page, {})
        p = prior.get(page, {})
        cur_pos, prior_pos = c.get("position"), p.get("position")
        per_url.append({
            "url": page,
            "position": cur_pos,
            "ctr": c.get("ctr"),
            "impressions": c.get("impressions", 0),
            "clicks": c.get("clicks", 0),
            # position: lower is better, so improvement is prior - current
            "delta_position": (round(prior_pos - cur_pos, 2)
                               if cur_pos is not None and prior_pos is not None else None),
            "delta_clicks": c.get("clicks", 0) - p.get("clicks", 0),
            "delta_impressions": c.get("impressions", 0) - p.get("impressions", 0),
            "top_queries": c.get("top_queries", []),
            "indexed": None,   # filled by add_index_status for in-scope URLs
        })
    return per_url


# --- URL Inspection (indexing status) --------------------------------------

def index_status(service, prop: str, url: str) -> dict:
    try:
        resp = service.urlInspection().index().inspect(
            body={"inspectionUrl": url, "siteUrl": prop}
        ).execute()
        res = resp.get("inspectionResult", {}).get("indexStatusResult", {})
        verdict = res.get("verdict")
        return {
            "indexed": verdict == "PASS",
            "coverage": res.get("coverageState"),
            "verdict": verdict,
        }
    except Exception as e:  # inspection quota/permission issues shouldn't fail the whole run
        return {"indexed": None, "coverage": f"error:{type(e).__name__}", "verdict": None}


def add_index_status(service, prop: str, per_url: list[dict], in_scope: set[str]) -> None:
    by_url = {row["url"]: row for row in per_url}
    for url in in_scope:
        st = index_status(service, prop, url)
        row = by_url.get(url)
        if row is None:  # in-scope URL with no GSC clicks/impressions still needs a record
            row = {"url": url, "position": None, "ctr": None, "impressions": 0,
                   "clicks": 0, "delta_position": None, "top_queries": []}
            per_url.append(row)
        row["indexed"] = st["indexed"]
        row["coverage"] = st["coverage"]


# --- CrUX field CWV ---------------------------------------------------------

def _bucket(metric: str, p75: float | None) -> str:
    if p75 is None:
        return "no_data"
    good, poor = CWV_THRESHOLDS[metric]
    if p75 <= good:
        return "good"
    return "poor" if p75 > poor else "needs_improvement"


def crux_buckets(url: str, origin: str, key: str) -> dict:
    """Try URL-level CrUX, fall back to origin-level. No key / no data → all 'no_data'."""
    if not key:
        return {"lcp": "no_data", "cls": "no_data", "inp": "no_data", "source": "no_key"}
    for scope_key, val, src in (("url", url, "url"), ("origin", origin, "origin")):
        try:
            r = requests.post(CRUX_ENDPOINT, params={"key": key},
                              json={scope_key: val}, timeout=30)
            if r.status_code != 200:
                continue
            metrics = r.json().get("record", {}).get("metrics", {})
            p75 = lambda m: metrics.get(m, {}).get("percentiles", {}).get("p75")
            return {
                "lcp": _bucket("largest_contentful_paint", _num(p75("largest_contentful_paint"))),
                "cls": _bucket("cumulative_layout_shift", _num(p75("cumulative_layout_shift"))),
                "inp": _bucket("interaction_to_next_paint", _num(p75("interaction_to_next_paint"))),
                "source": src,
            }
        except requests.RequestException:
            continue
    return {"lcp": "no_data", "cls": "no_data", "inp": "no_data", "source": "no_data"}


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_cwv_field(urls: list[str], origin: str, key: str) -> list[dict]:
    out = []
    for url in urls:
        b = crux_buckets(url, origin, key)
        out.append({"url": url, "lcp_bucket": b["lcp"], "cls_bucket": b["cls"],
                    "inp_bucket": b["inp"], "source": b["source"], "trend": None})
    return out


# --- orchestration ----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Loop B GSC + CrUX pull.")
    ap.add_argument("--site", help="Site slug / config (default from SITE_CONFIG).")
    ap.add_argument("--window-days", type=int, help="Window length; default from config.gsc.window_days.")
    ap.add_argument("--lag-days", type=int, default=3, help="Days to lag for GSC data freshness (default 3).")
    ap.add_argument("--no-inspect", action="store_true", help="Skip URL Inspection indexing status.")
    ap.add_argument("--no-crux", action="store_true", help="Skip CrUX field CWV.")
    ap.add_argument("--dry-run", action="store_true", help="Print the weekly payload; do not write.")
    args = ap.parse_args(argv)

    cfg = load_site_config(f"config/{args.site}.yaml" if args.site else None)
    slug = site_slug(cfg)
    prop = cfg["gsc"]["property"]
    origin = cfg["site"]["base_url"].rstrip("/")
    n = args.window_days or cfg.get("gsc", {}).get("window_days", 7)
    win = windows(n, args.lag_days)
    run_date = dt.date.today().isoformat()

    service = get_service()
    run_id = None if args.dry_run else db.start_run(slug, "loop_b")
    try:
        cur_rows = search_analytics(service, prop, *win["current"])
        prior_rows = search_analytics(service, prop, *win["prior"])
        per_url = build_per_url(cur_rows, prior_rows)

        in_scope = [t["url"] for t in resolve_targets(cfg)]
        if not args.no_inspect:
            add_index_status(service, prop, per_url, set(in_scope))
        cwv_field = [] if args.no_crux else build_cwv_field(
            in_scope, origin, env("CRUX_API_KEY") or env("PSI_API_KEY") or ""
        )

        gsc_window = f"{win['current'][0]}..{win['current'][1]} vs {win['prior'][0]}..{win['prior'][1]}"
        fields = {
            "gsc_window": gsc_window,
            "per_url": per_url,
            "cwv_field": cwv_field,
            "run_status": "partial",   # agent completes opportunities/editorial_gaps
        }
        if args.dry_run:
            import json
            print(json.dumps({"site": slug, "run_date": run_date, **fields}, indent=2, default=str))
        else:
            db.upsert_weekly(slug, run_date, fields)
            db.end_run(run_id, "ok")
        print(f"[gsc_pull] {'(dry-run) ' if args.dry_run else ''}{len(per_url)} page(s), "
              f"{len(cwv_field)} CrUX record(s), window {gsc_window}.", file=sys.stderr)
        return 0
    except Exception as e:
        if run_id:
            db.end_run(run_id, "failed", str(e))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
