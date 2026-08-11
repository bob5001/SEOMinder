"""Loop A entrypoint — on-page / technical (thin orchestrator).

Pipeline:  audit (measure + persist)  ->  [agent: Tier-1 Yoast fixes + checklist judgment]  ->  render.

Trigger: manual /goal or on-publish (--url for the single-post variant). The run log +
seo_page_state writes happen inside audit.py; the agent step is deferred (see scripts.agent),
so today this runs the deterministic audit + render. Add --no-agent to force-skip the step.
"""
from __future__ import annotations

import argparse
import sys

from . import agent, audit, db, render_report
from .config import load_site_config, site_slug


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Loop A — audit, fix (Tier 1), render.")
    ap.add_argument("--site", help="Site slug / config (default from SITE_CONFIG).")
    ap.add_argument("--url", help="Single URL (on-publish variant) instead of the config scope.")
    ap.add_argument("--runs", type=int, default=3, help="PSI runs to median (default 3).")
    ap.add_argument("--no-psi", action="store_true")
    ap.add_argument("--no-links", action="store_true")
    ap.add_argument("--no-agent", action="store_true", help="Skip the metadata proposal step.")
    ap.add_argument("--apply", action="store_true",
                    help="Write accepted Tier-1 changes to WordPress. WITHOUT this the agent "
                         "runs, state is recorded and a diff is printed, but the live site is "
                         "untouched — the intended way to rehearse against production.")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--no-discord", action="store_true")
    args = ap.parse_args(argv)

    # 1. audit — measures and upserts seo_page_state; owns the loop_a run-log entry.
    audit_argv: list[str] = []
    if args.site:
        audit_argv += ["--site", args.site]
    if args.url:
        audit_argv += ["--url", args.url]
    audit_argv += ["--runs", str(args.runs)]
    if args.no_psi:
        audit_argv.append("--no-psi")
    if args.no_links:
        audit_argv.append("--no-links")
    rc = audit.main(audit_argv)
    if rc != 0:
        return rc

    # 2. agent — proposes Tier-1 Yoast metadata. checklist_status is NOT its job: audit.py
    #    already computed it from measured fields via scripts.checklist.
    if args.no_agent:
        pass
    elif not agent.WIRED:
        print("[loop_a] agent step not wired (agent.WIRED is False) — deterministic audit + "
              "render only.", file=sys.stderr)
    elif not agent.agent_available():
        print("[loop_a] routed model unreachable (preflight failed) — skipping proposal step.",
              file=sys.stderr)
    else:
        cfg = load_site_config(f"config/{args.site}.yaml" if args.site else None)
        slug = site_slug(cfg)
        pages = db.list_page_state(slug)
        if args.url:
            pages = [p for p in pages if p["url"] == args.url]
        missing = [p["url"] for p in pages if not p.get("content_excerpt")]
        if missing:
            print(f"[loop_a] {len(missing)} page(s) have no content_excerpt — re-run audit "
                  f"before proposing, or the model writes metadata blind.", file=sys.stderr)
        try:
            summary = agent.run_loop_a_fixes(cfg, slug, pages, apply=args.apply)
            print(agent.render_proposals(summary, pages, cfg))
        except NotImplementedError as err:
            print(f"[loop_a] {err}", file=sys.stderr)
            return 2
        except RuntimeError as err:
            # Reconciliation failure: nothing was written, by design.
            print(f"[loop_a] proposal rejected, nothing written — {err}", file=sys.stderr)
            return 1

    # 3. render — projection of Postgres -> reports/<date>.md (+ Discord).
    if not args.no_render:
        render_argv = (["--site", args.site] if args.site else [])
        if args.no_discord:
            render_argv.append("--no-discord")
        render_report.main(render_argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
