"""On-publish trigger for Loop A — polls for newly-published content and runs Loop A once,
unattended (auto-apply), for each item it hasn't seen before.

Deliberately POLLING, not a WordPress webhook. This repo's whole execution model is a host
systemd timer firing a one-shot container (INFRA.md: "No OpenClaw... plain systemd + flock")
— never a listening service. A short-interval timer here (deploy/seo-loop-a-on-publish.timer,
every 10 min) needs nothing on the WordPress side and no public endpoint on ours, unlike a
webhook receiver would.

Discovery has NO separate watermark table: "new" means "a published item of
scope.on_publish_post_type whose id is not yet a post_id in seo_page_state for this site."
seo_page_state IS the watermark. One consequence worth knowing: if the agent/apply step
fails AFTER the audit half has already persisted a row, that item will NOT be retried
automatically on the next poll — process_new_post's failure path says so explicitly in the
Discord alert and names the manual recovery command, rather than silently retrying into a
partially-applied state.

Safety, layered the same as the manual batch runs from the pages/posts sessions:
  - scope.exclude_ids / flag_only_ids are honoured. Auto-discovery is the first path in this
    repo where they actually do anything — the hand-curated in_scope_ids/in_scope_post_ids
    lists made them redundant everywhere else, since something simply not listed there was
    already excluded by omission.
  - A brand-new item whose permalink collides with an ALREADY-KNOWN page's URL is refused and
    Discord-alerted rather than processed. This is exactly the post-2778-vs-page-2183 defect
    found manually earlier — WP's rewrite priority silently shadows the second-created content
    at a colliding slug, so writing Yoast meta to it would be pointless. Automating discovery
    makes this a real, unattended risk instead of a one-off found by inspection.
  - Everything downstream (agent.run_loop_a_fixes with apply=True) already carries the
    sitewide-collision gate, the off-band gate, and per-write read-back verification built for
    the manual runs — this script adds nothing new there, it just calls the same function.

CLI:
    python -m scripts.run_loop_a_on_publish                  # poll once, auto-apply, Discord
    python -m scripts.run_loop_a_on_publish --dry-run         # discover + print only
    python -m scripts.run_loop_a_on_publish --no-discord
"""
from __future__ import annotations

import argparse
import sys

from . import agent, audit, checklist, db, render_report, wp_mcp
from .config import load_site_config, site_slug
from .models import preflight, route_for


def discover_new(cfg: dict, slug: str) -> list[dict]:
    """Published items of scope.on_publish_post_type not yet known for this site, minus
    exclude_ids/flag_only_ids and anything whose permalink collides with a known page.
    Returns [{"post_id", "url"}]."""
    post_type = (cfg.get("scope", {}) or {}).get("on_publish_post_type", "post")
    known = db.list_page_state(slug)
    known_ids = {p["post_id"] for p in known if p.get("post_id")}
    known_urls = {p["url"] for p in known}
    exclude = set((cfg.get("scope", {}) or {}).get("exclude_ids", []) or [])
    flag_only = set((cfg.get("scope", {}) or {}).get("flag_only_ids", []) or [])

    found: list[dict] = []
    offset = 0
    while True:
        batch = wp_mcp.call_tool("wp_get_posts", {
            "post_type": post_type, "post_status": "publish", "limit": 50, "offset": offset})
        if not batch:
            break
        for item in batch:
            pid, url = item.get("ID"), item.get("permalink")
            if pid is None or not url:
                continue
            if pid in known_ids or pid in exclude or pid in flag_only:
                continue
            if url in known_urls:
                print(f"[on_publish] SKIP {url} (id {pid}): permalink already claimed by "
                      f"another known page — slug collision, needs human review before this "
                      f"can be included (see post 2778 / page 2183 for the precedent).",
                      file=sys.stderr)
                continue
            found.append({"post_id": pid, "url": url})
        offset += len(batch)
        if len(batch) < 50:
            break
    return found


def process_new_post(cfg: dict, slug: str, post_id: int, url: str) -> dict:
    """Audit + Loop A (auto-apply) one freshly-discovered item. Isolated — a failure here
    must not crash the rest of the poll, so the caller wraps this in its own try/except."""
    parsed = audit.audit_url(url, None, runs=0, do_psi=False, do_links=True)
    fields = audit.column_fields(parsed, post_id, "post")
    # No sitewide crawl happens for a single new item, so inbound links are measured as 0
    # (accurate for something just published) rather than guessed. The next full Loop A
    # sweep recomputes this properly across the whole known set.
    fields["internal_links_in"] = 0
    verdict = checklist.evaluate(fields, cfg)
    fields["checklist_status"] = verdict["status"]
    try:
        fields["yoast_readability_score"] = wp_mcp.get_yoast_readability_score(post_id)
    except Exception as err:
        print(f"[on_publish] could not fetch Yoast readability for {url}: {err}", file=sys.stderr)
    db.upsert_page_state(slug, url, fields)

    page = db.get_page_state(slug, url)
    try:
        summary = agent.run_loop_a_fixes(cfg, slug, [page], apply=True)
    except Exception as err:
        return {"url": url, "post_id": post_id, "status": "AGENT_FAILED", "error": str(err)}

    proposal = summary["proposals"][0] if summary["proposals"] else {}
    return {"url": url, "post_id": post_id, "status": "OK", "proposal": proposal}


def discord_summary(cfg: dict, results: list[dict]) -> str:
    name = cfg["site"]["name"]
    lines = [f"**Loop A on-publish — {name}**"]
    for r in results:
        if r["status"] == "OK":
            p = r["proposal"]
            if p.get("applied") and p.get("changes"):
                fields = ", ".join(c["field"] for c in p["changes"])
                lines.append(f"✅ wrote {fields} — {r['url']}")
            elif p.get("manual_queue"):
                lines.append(f"⚠️ queued for human review ({len(p['manual_queue'])} item(s)) "
                             f"— {r['url']}")
            else:
                lines.append(f"— audited, no metadata changes proposed — {r['url']}")
        else:
            lines.append(
                f"❌ {r['status']}: {r['url']} — {str(r.get('error', ''))[:150]}\n"
                f"   Will NOT auto-retry. Run manually: "
                f"`python -m scripts.run_loop_a --url {r['url']} --apply`")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Loop A on-publish trigger — poll for new content, run Loop A once each.")
    ap.add_argument("--site", help="Site slug / config (default from SITE_CONFIG).")
    ap.add_argument("--dry-run", action="store_true", help="Discover + print only; touch nothing.")
    ap.add_argument("--no-discord", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_site_config(f"config/{args.site}.yaml" if args.site else None)
    slug = site_slug(cfg)

    if not agent.WIRED:
        print("[on_publish] agent.WIRED is False — nothing to do.", file=sys.stderr)
        return 0
    # Scoped to loop_a_meta only — agent.agent_available() also preflights loop_b_rank, which
    # this script has no business depending on.
    ok, info = preflight(route_for("loop_a_meta"))
    if not ok:
        print(f"[on_publish] loop_a_meta route unreachable ({info}) — skipping this poll.",
              file=sys.stderr)
        return 0

    new_items = discover_new(cfg, slug)
    if not new_items:
        print("[on_publish] nothing new.", file=sys.stderr)
        return 0

    if args.dry_run:
        for item in new_items:
            print(f"[on_publish] would process post_id={item['post_id']} {item['url']}")
        return 0

    run_id = db.start_run(slug, "loop_a_on_publish")
    results = []
    for item in new_items:
        try:
            results.append(process_new_post(cfg, slug, item["post_id"], item["url"]))
        except Exception as err:
            results.append({"url": item["url"], "post_id": item["post_id"],
                            "status": "AUDIT_FAILED", "error": str(err)})
            print(f"[on_publish] FAILED {item['url']}: {err}", file=sys.stderr)

    failed = [r for r in results if r["status"] != "OK"]
    db.end_run(run_id, "partial" if failed else "ok",
               "; ".join(f"{r['url']}: {r.get('error', '')}" for r in failed) if failed else None)
    print(f"[on_publish] processed {len(results)} new item(s), {len(failed)} failed.",
          file=sys.stderr)

    if not args.no_discord and cfg.get("reporting", {}).get("discord"):
        webhook, source = render_report.resolve_webhook(cfg, slug)
        if webhook:
            render_report.post_discord(webhook, discord_summary(cfg, results))
            print(f"[on_publish] posted Discord summary (via {source}).", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
