"""Loop A entrypoint — on-page / technical (thin orchestrator).

Pipeline:  audit (measure + persist)  ->  [agent: Tier-1 Yoast fixes + checklist judgment]  ->  render.

Trigger: manual /goal or on-publish (--url for the single-post variant). The run log +
seo_page_state writes happen inside audit.py; the agent step is deferred (see scripts.agent),
so today this runs the deterministic audit + render. Add --no-agent to force-skip the step.
"""
from __future__ import annotations

import argparse
import sys

from . import agent, audit, render_report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Loop A — audit, fix (Tier 1), render.")
    ap.add_argument("--site", help="Site slug / config (default from SITE_CONFIG).")
    ap.add_argument("--url", help="Single URL (on-publish variant) instead of the config scope.")
    ap.add_argument("--runs", type=int, default=3, help="PSI runs to median (default 3).")
    ap.add_argument("--no-psi", action="store_true")
    ap.add_argument("--no-links", action="store_true")
    ap.add_argument("--no-agent", action="store_true", help="Skip the claude -p fix/judgment step.")
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

    # 2. agent — Tier-1 Yoast fixes + authoritative checklist_status (deferred).
    if args.no_agent:
        pass
    elif not agent.WIRED:
        print("[loop_a] agent step not wired yet (deferred) — deterministic audit + render only. "
              "checklist_status stays unset until the agent runs.", file=sys.stderr)
    elif not agent.agent_available():
        print("[loop_a] agent unavailable (no ANTHROPIC_API_KEY / claude CLI) — skipping fix step.",
              file=sys.stderr)
    else:
        agent.run_loop_a_fixes  # wired path lives here

    # 3. render — projection of Postgres -> reports/<date>.md (+ Discord).
    if not args.no_render:
        render_argv = (["--site", args.site] if args.site else [])
        if args.no_discord:
            render_argv.append("--no-discord")
        render_report.main(render_argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
