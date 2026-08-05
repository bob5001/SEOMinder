"""Presentation layer — a projection of Postgres state, never a parallel truth.

Reads seo_page_state + the latest seo_weekly for a site, renders a structured markdown
report into reports/YYYY-MM-DD.md, and posts a short summary to Discord via webhook.
Writes nothing back to Postgres. This is the ONLY module that talks to Discord (webhook,
one-way notify — a bot is a V2 concern).

The report shows measured values with light threshold annotations from config, but it does
NOT re-compute the gate: the authoritative per-page verdict is seo_page_state.checklist_status,
which the Loop A agent sets. Empty means "not yet judged".

CLI:
    python -m scripts.render_report --site signalsanctuary            # write file + Discord
    python -m scripts.render_report --site signalsanctuary --dry-run  # print md, no file/Discord
    python -m scripts.render_report --site signalsanctuary --no-discord
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import requests

from . import db
from .config import REPO_ROOT, env, load_site_config, site_slug

TIMEOUT = 20


def _slug(url: str) -> str:
    path = url.split("://", 1)[-1].split("/", 1)
    return "/" + (path[1].rstrip("/") if len(path) > 1 else "")


def _flag(ok: bool | None) -> str:
    return {True: "ok", False: "⚠️", None: "—"}[ok]


def _band(val, lo, hi) -> bool | None:
    return None if val is None else (lo <= val <= hi)


def _atleast(val, floor) -> bool | None:
    return None if val is None else (val >= floor)


# --- markdown ---------------------------------------------------------------

def render_md(cfg: dict, pages: list[dict], weekly: dict | None) -> str:
    th = cfg.get("thresholds", {})
    t_lo, t_hi = th.get("title_len", {}).get("min", 50), th.get("title_len", {}).get("max", 60)
    m_lo, m_hi = th.get("metadesc_len", {}).get("min", 150), th.get("metadesc_len", {}).get("max", 160)
    li_min = th.get("internal_links_in_min", 2)
    lo_min = th.get("internal_links_out_min", 2)
    name = cfg["site"]["name"]
    today = dt.date.today().isoformat()

    L = [f"# SEO report — {name}", f"_{today} · projection of Postgres state_", ""]

    # --- Loop A: on-page state ---
    L += ["## Loop A — on-page / technical", ""]
    if not pages:
        L += ["_No page state recorded yet. Run `scripts.audit` first._", ""]
    else:
        green = sum(1 for p in pages if p.get("checklist_status") == "green")
        L += [f"**{len(pages)} pages audited · {green} green · "
              f"{sum(1 for p in pages if p.get('broken_links'))} with broken links**", ""]
        L += ["| Page | Title | Meta | H1 | Head | Alt% | In | Out | Idx | Schema | SEO | A11y | Status |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in sorted(pages, key=lambda x: x["url"]):
            L.append("| " + " | ".join([
                _slug(p["url"]),
                f"{p.get('title_len')}{_ann(_band(p.get('title_len'), t_lo, t_hi))}",
                f"{p.get('metadesc_len')}{_ann(_band(p.get('metadesc_len'), m_lo, m_hi))}",
                _flag(p.get("h1_count") == 1 if p.get("h1_count") is not None else None),
                _flag(p.get("heading_order_ok")),
                f"{p.get('alt_coverage_pct')}",
                f"{p.get('internal_links_in')}{_ann(_atleast(p.get('internal_links_in'), li_min))}",
                f"{p.get('internal_links_out')}{_ann(_atleast(p.get('internal_links_out'), lo_min))}",
                _flag(p.get("indexable")),
                _flag(p.get("schema_valid")),
                _flag(p.get("lighthouse_seo_pass")),
                _flag(p.get("lighthouse_a11y_pass")),
                p.get("checklist_status") or "—",
            ]) + " |")
        L.append("")
        broken = [(p["url"], p["broken_links"]) for p in pages if p.get("broken_links")]
        if broken:
            L += ["**Broken links:**"] + [f"- `{_slug(u)}` → {bl}" for u, bl in broken] + [""]

    # --- Loop B: weekly opportunity sweep ---
    L += ["## Loop B — weekly opportunity sweep", ""]
    if not weekly:
        L += ["_No weekly run recorded yet. Run `scripts.gsc_pull` + the Loop B agent._", ""]
    else:
        L += [f"_Window: {weekly.get('gsc_window', 'n/a')} · status: {weekly.get('run_status', 'n/a')}_", ""]
        opps = weekly.get("opportunities") or []
        if opps:
            L += ["### Ranked opportunities"]
            for o in opps:
                L.append(f"- **{o.get('type', '?')}** · `{o.get('url_or_query', '')}` "
                         f"· {o.get('metric', '')} Δ{o.get('delta', '')} · priority {o.get('priority', '')}")
            L.append("")
        movers = _top_movers(weekly.get("per_url") or [])
        if movers:
            L += ["### Top movers (by position Δ)"]
            for r in movers:
                L.append(f"- `{_slug(r['url'])}` pos {r.get('position')} (Δ{r.get('delta_position')}), "
                         f"{r.get('impressions')} impr")
            L.append("")
        flags = [r for r in (weekly.get("per_url") or []) if r.get("indexed") is False]
        if flags:
            L += ["### ⚠️ Indexing flags (in-scope, not indexed)"] + \
                 [f"- `{_slug(r['url'])}` — {r.get('coverage', 'not indexed')}" for r in flags] + [""]
        gaps = weekly.get("editorial_gaps") or []
        if gaps:
            L += ["### Editorial gaps (human review)"] + \
                 [f"- \"{g.get('query')}\" — {g.get('impressions')} impr → {g.get('suggested_page', 'new post')}"
                  for g in gaps] + [""]

    return "\n".join(L) + "\n"


def _ann(ok: bool | None) -> str:
    return "" if ok in (True, None) else " ⚠️"


def _top_movers(per_url: list[dict], n: int = 5) -> list[dict]:
    scored = [r for r in per_url if r.get("delta_position") is not None]
    return sorted(scored, key=lambda r: abs(r["delta_position"]), reverse=True)[:n]


# --- Discord ----------------------------------------------------------------

def discord_summary(cfg: dict, pages: list[dict], weekly: dict | None) -> str:
    name = cfg["site"]["name"]
    green = sum(1 for p in pages if p.get("checklist_status") == "green")
    lines = [f"**SEO digest — {name}** ({dt.date.today().isoformat()})",
             f"Loop A: {len(pages)} pages · {green} green · "
             f"{sum(1 for p in pages if p.get('broken_links'))} with broken links"]
    if weekly:
        opps = weekly.get("opportunities") or []
        flags = [r for r in (weekly.get("per_url") or []) if r.get("indexed") is False]
        lines.append(f"Loop B: {len(weekly.get('per_url') or [])} pages · "
                     f"{len(opps)} opportunities · {len(flags)} indexing flag(s)")
    return "\n".join(lines)


def webhook_env_name(slug: str) -> str:
    """Conventional per-site env var, e.g. signalsanctuary -> DISCORD_WEBHOOK_URL_SIGNALSANCTUARY."""
    return "DISCORD_WEBHOOK_URL_" + re.sub(r"[^A-Z0-9]", "_", slug.upper())


def resolve_webhook(cfg: dict, slug: str) -> tuple[str | None, str]:
    """Find this site's webhook URL. URLs are secret-ish, so they live in .env — never in the
    committed yaml, which only names *which* env var to read. Returns (url, source) where source
    names where it came from (handy for logs). Resolution order:
      1. env var named by reporting.discord_webhook_env in the config
      2. convention:  DISCORD_WEBHOOK_URL_<SLUG>
      3. global:      DISCORD_WEBHOOK_URL   (single-site / back-compat)
    """
    named = (cfg.get("reporting", {}) or {}).get("discord_webhook_env")
    if named and env(named):
        return env(named), named
    conv = webhook_env_name(slug)
    if env(conv):
        return env(conv), conv
    if env("DISCORD_WEBHOOK_URL"):
        return env("DISCORD_WEBHOOK_URL"), "DISCORD_WEBHOOK_URL"
    return None, ""


def post_discord(webhook: str, content: str) -> None:
    r = requests.post(webhook, json={"content": content}, timeout=TIMEOUT)
    r.raise_for_status()


# --- orchestration ----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render the SEO report (projection of Postgres).")
    ap.add_argument("--site", help="Site slug / config (default from SITE_CONFIG).")
    ap.add_argument("--dry-run", action="store_true", help="Print md to stdout; no file, no Discord.")
    ap.add_argument("--no-discord", action="store_true", help="Write the file but skip the Discord post.")
    args = ap.parse_args(argv)

    cfg = load_site_config(f"config/{args.site}.yaml" if args.site else None)
    slug = site_slug(cfg)
    pages = db.list_page_state(slug)
    weekly = db.get_latest_weekly(slug)
    md = render_md(cfg, pages, weekly)

    if args.dry_run:
        print(md)
        return 0

    reports_dir = Path(cfg.get("reporting", {}).get("reports_dir", "reports"))
    if not reports_dir.is_absolute():
        reports_dir = REPO_ROOT / reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / f"{dt.date.today().isoformat()}.md"
    out.write_text(md)
    print(f"[render_report] wrote {out}", file=sys.stderr)

    webhook, source = resolve_webhook(cfg, slug)
    if args.no_discord or not cfg.get("reporting", {}).get("discord"):
        print("[render_report] Discord disabled by flag/config.", file=sys.stderr)
    elif not webhook:
        print(f"[render_report] no webhook set (tried {webhook_env_name(slug)} then "
              f"DISCORD_WEBHOOK_URL) — skipping Discord post.", file=sys.stderr)
    else:
        post_discord(webhook, discord_summary(cfg, pages, weekly))
        print(f"[render_report] posted Discord summary (via {source}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
