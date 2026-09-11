"""Backlink tracking — manual snapshots, logged by a human.

Referring-domain/backlink counts have no free API: GSC's own Links report is free but UI/export
-only (no public API endpoint), and the providers that DO have an API (Ahrefs, Moz, Semrush) are
paid tiers not worth it for a site this early. So this is deliberately NOT another gsc_pull-style
automated puller — it's a thin CLI over one Postgres table, filled in whenever you look at GSC's
Links report (or any other source) yourself.

CLI:
    python -m scripts.backlinks log --referring-domains 3 --total-backlinks 5 \\
        --new-domain example.com --notes "found via GSC Links report"
    python -m scripts.backlinks show
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from . import db
from .config import load_site_config, site_slug


def log_snapshot(site: str, snapshot_date: str, referring_domains: int | None,
                 total_backlinks: int | None, new_domains: list[str] | None,
                 notes: str | None) -> None:
    fields: dict = {}
    if referring_domains is not None:
        fields["referring_domains"] = referring_domains
    if total_backlinks is not None:
        fields["total_backlinks"] = total_backlinks
    if new_domains:
        fields["new_domains"] = [{"domain": d} for d in new_domains]
    if notes:
        fields["notes"] = notes
    db.upsert_backlinks(site, snapshot_date, fields)


def render_trend(site: str, limit: int = 12) -> str:
    """Newest-first snapshots with the delta against the PRIOR (older) snapshot — so a positive
    number always means "gained since last time you checked", regardless of how irregularly
    you log."""
    rows = db.list_backlinks(site, limit=limit)
    if not rows:
        return "No backlink snapshots logged yet. `python -m scripts.backlinks log --help`"

    lines = [f"Backlink snapshots — {site} (newest first)", ""]
    for i, row in enumerate(rows):
        older = rows[i + 1] if i + 1 < len(rows) else None
        rd, tb = row.get("referring_domains"), row.get("total_backlinks")

        def _delta(cur, prev) -> str:
            if cur is None or prev is None:
                return ""
            d = cur - prev
            return f" ({'+' if d >= 0 else ''}{d})"

        rd_delta = _delta(rd, older.get("referring_domains") if older else None)
        tb_delta = _delta(tb, older.get("total_backlinks") if older else None)
        lines.append(
            f"  {row['snapshot_date']}  referring_domains={rd}{rd_delta}  "
            f"total_backlinks={tb}{tb_delta}"
        )
        if row.get("new_domains"):
            domains = ", ".join(d.get("domain", "") for d in row["new_domains"])
            lines.append(f"    new: {domains}")
        if row.get("notes"):
            lines.append(f"    note: {row['notes']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backlink snapshots — manual log, no auto-pull.")
    ap.add_argument("--site", help="Site slug / config (default from SITE_CONFIG).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_log = sub.add_parser("log", help="Record a snapshot (upserts by date — safe to correct "
                                       "today's entry by re-running with the same --date).")
    p_log.add_argument("--date", default=dt.date.today().isoformat(),
                       help="Snapshot date, YYYY-MM-DD (default: today).")
    p_log.add_argument("--referring-domains", type=int)
    p_log.add_argument("--total-backlinks", type=int)
    p_log.add_argument("--new-domain", action="append", dest="new_domains",
                       help="A newly-seen referring domain. Repeatable.")
    p_log.add_argument("--notes", help="Free text — where you saw this / anything worth flagging.")

    sub.add_parser("show", help="Print the trend (most recent snapshots, newest first).")

    args = ap.parse_args(argv)
    cfg = load_site_config(f"config/{args.site}.yaml" if args.site else None)
    slug = site_slug(cfg)

    if args.cmd == "log":
        if args.referring_domains is None and args.total_backlinks is None \
                and not args.new_domains and not args.notes:
            print("[backlinks] nothing to log — pass at least one of --referring-domains, "
                  "--total-backlinks, --new-domain, --notes.", file=sys.stderr)
            return 1
        log_snapshot(slug, args.date, args.referring_domains, args.total_backlinks,
                    args.new_domains, args.notes)
        print(f"[backlinks] logged {args.date} for {slug}.", file=sys.stderr)
        return 0

    if args.cmd == "show":
        print(render_trend(slug))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
