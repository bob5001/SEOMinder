"""Loop B entrypoint — weekly opportunity sweep (thin orchestrator).

Pipeline:  gsc_pull (measure + persist)  ->  [agent: rank opportunities + editorial gaps]  ->  render.

Fired weekly by the host systemd timer (see deploy/). NOT a convergence loop: one pass,
ranked queue, exit. The run log + seo_weekly writes happen inside gsc_pull.py; the agent
step is deferred (see scripts.agent), so today this runs the deterministic pull + render.
"""
from __future__ import annotations

import argparse
import sys

from . import agent, db, gsc_pull, render_report
from .config import load_site_config, site_slug


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Loop B — GSC pull, rank, render.")
    ap.add_argument("--site", help="Site slug / config (default from SITE_CONFIG).")
    ap.add_argument("--window-days", type=int)
    ap.add_argument("--lag-days", type=int, default=3)
    ap.add_argument("--no-inspect", action="store_true", help="Skip URL Inspection indexing status.")
    ap.add_argument("--no-crux", action="store_true", help="Skip CrUX field CWV.")
    ap.add_argument("--no-agent", action="store_true", help="Skip the claude -p ranking step.")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--no-discord", action="store_true")
    args = ap.parse_args(argv)

    # 1. gsc_pull — measures and upserts seo_weekly (per_url + cwv_field); owns the loop_b run-log.
    gsc_argv: list[str] = []
    if args.site:
        gsc_argv += ["--site", args.site]
    if args.window_days is not None:
        gsc_argv += ["--window-days", str(args.window_days)]
    gsc_argv += ["--lag-days", str(args.lag_days)]
    if args.no_inspect:
        gsc_argv.append("--no-inspect")
    if args.no_crux:
        gsc_argv.append("--no-crux")
    rc = gsc_pull.main(gsc_argv)
    if rc != 0:
        return rc

    # 2. agent — rank opportunities, emit editorial gaps, hand top-N to Loop A (deferred).
    if args.no_agent:
        pass
    elif not agent.WIRED:
        print("[loop_b] agent step not wired yet (deferred) — deterministic pull + render only. "
              "opportunities/editorial_gaps stay empty until the agent runs.", file=sys.stderr)
    elif not agent.agent_available():
        print("[loop_b] agent unavailable (no ANTHROPIC_API_KEY / claude CLI) — skipping ranking step.",
              file=sys.stderr)
    else:
        cfg = load_site_config(f"config/{args.site}.yaml" if args.site else None)
        slug = site_slug(cfg)
        weekly = db.get_latest_weekly(slug)
        if not weekly:
            print("[loop_b] no weekly row to rank (gsc_pull produced nothing) — skipping.",
                  file=sys.stderr)
        else:
            try:
                agent.run_loop_b_ranking(cfg, slug, str(weekly["run_date"]), weekly)
            except Exception as err:
                print(f"[loop_b] ranking step failed, nothing ranked this run: {err}",
                      file=sys.stderr)

    # 3. render — projection of Postgres -> reports/<date>.md (+ Discord).
    if not args.no_render:
        render_argv = (["--site", args.site] if args.site else [])
        if args.no_discord:
            render_argv.append("--no-discord")
        render_report.main(render_argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
