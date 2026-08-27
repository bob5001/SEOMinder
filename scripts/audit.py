"""Loop A technical audit.

Two deterministic sources, no browser:
  1. PageSpeed Insights API  -> Lighthouse SEO + Accessibility pass/fail + lab CWV.
  2. A plain HTTP fetch of the rendered HTML, parsed with BeautifulSoup -> headings,
     alt coverage, internal-link graph, canonical/indexability, JSON-LD schema, broken
     links, mixed content.

Elementor renders server-side, so the fetched HTML already contains the content; no JS
execution is needed. PSI runs Lighthouse on Google's side, so there is no local Chromium.

Output: measured fields upserted into seo_page_state via db.py (the single writer). The
Yoast-meta *writes* are the loop agent's job, not this script's — audit only measures.

CLI (handy for dry runs):
    python -m scripts.audit --site signalsanctuary                 # full in-scope set
    python -m scripts.audit --url https://example.com/ --no-psi --dry-run
    python -m scripts.audit --site signalsanctuary --runs 1 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import checklist, db, wp_mcp
from .config import env, load_site_config, site_slug

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
USER_AGENT = "SEOMinder-audit/0.1 (+https://github.com/bob5001/SEOMinder)"
TIMEOUT = 30


# --- HTTP -------------------------------------------------------------------

def fetch(url: str, method: str = "GET") -> requests.Response:
    return requests.request(
        method, url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, allow_redirects=True
    )


def registrable(host: str) -> str:
    """Cheap same-site check: compare the last two labels (example.co.uk edge cases ignored for V1)."""
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


# --- HTML parse -------------------------------------------------------------

EXCERPT_CHARS = 1400


def extract_prose(soup: BeautifulSoup) -> str:
    """Visible body prose — what the page actually claims, minus the chrome.

    Persisted with the audit for two reasons. The Loop A agent needs it to write a meta
    description that is about the page (and to quote the claim it must preserve), and keeping
    a snapshot per audit means a ranking slide can be checked against whether the copy itself
    changed. Chrome carries no claims, so nav/header/footer/forms are dropped.
    """
    body = BeautifulSoup(str(soup), "lxml")
    for tag in body(["script", "style", "nav", "header", "footer", "noscript", "form"]):
        tag.decompose()
    root = body.find("main") or body.find("article") or body.body or body
    parts: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "p", "li"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        if len(text) > 25:                      # skip nav crumbs and one-word list items
            parts.append(text)
        if sum(len(p) for p in parts) > EXCERPT_CHARS:
            break
    return " ".join(parts)[:EXCERPT_CHARS]


def parse_html(html: str, final_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    page_host = registrable(urlparse(final_url).netloc)

    # title + meta description as rendered (canonical source for meta is Yoast via MCP;
    # this is what actually ships in the HTML, which is what we audit).
    title = (soup.title.string or "").strip() if soup.title else ""
    md_tag = soup.find("meta", attrs={"name": "description"})
    metadesc = (md_tag.get("content", "").strip() if md_tag else "")

    # headings
    headings = [(int(h.name[1]), h.get_text(strip=True)) for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])]
    h1_count = sum(1 for lvl, _ in headings if lvl == 1)
    heading_order_ok = _heading_order_ok(headings, h1_count)

    # alt coverage over substantive images (skip presentational / tracking pixels)
    imgs = [i for i in soup.find_all("img") if not _is_decorative(i)]
    with_alt = [i for i in imgs if (i.get("alt") or "").strip()]
    alt_coverage_pct = round(100.0 * len(with_alt) / len(imgs), 1) if imgs else 100.0

    # link graph
    internal_targets, external = _classify_links(soup, final_url, page_host)

    # canonical + indexability
    canonical_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
    canonical = canonical_tag.get("href") if canonical_tag else None
    robots = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "robots"})
    robots_content = (robots.get("content", "").lower() if robots else "")
    indexable = "noindex" not in robots_content

    # schema (JSON-LD); keep all @types so the gate can test membership (Organization/FAQPage present)
    schema_types, schema_valid = _parse_jsonld(soup)
    schema_type = ", ".join(dict.fromkeys(schema_types)) or None

    # mixed content: http:// subresources on an https page
    mixed = _mixed_content(soup, final_url)

    # body prose + a hash of it, so content drift between audits is detectable
    prose = extract_prose(soup)

    return {
        "title": title,
        "title_len": len(title),
        "metadesc": metadesc,
        "metadesc_len": len(metadesc),
        "content_excerpt": prose,
        "content_sha": hashlib.sha256(prose.encode()).hexdigest()[:16] if prose else None,
        "h1_count": h1_count,
        "heading_order_ok": heading_order_ok,
        "alt_coverage_pct": alt_coverage_pct,
        "internal_links_out": len(internal_targets),
        "schema_type": schema_type,
        "schema_valid": schema_valid,
        "indexable": indexable,
        "_canonical": canonical,          # underscored = not a column; used by run logic
        "_internal_targets": sorted(internal_targets),
        "_external_links": sorted(external),
        "_mixed_content": mixed,
        "_missing_alt_count": len(imgs) - len(with_alt),
    }


def _heading_order_ok(headings: list[tuple[int, str]], h1_count: int) -> bool:
    if h1_count != 1:
        return False
    prev = None
    for lvl, _ in headings:
        if prev is not None and lvl > prev + 1:  # skipped a level (e.g. h2 -> h4)
            return False
        prev = lvl
    return True


def _is_decorative(img) -> bool:
    if (img.get("role") or "").lower() == "presentation":
        return True
    if img.get("aria-hidden", "").lower() == "true":
        return True
    if img.get("alt") is not None and img.get("alt").strip() == "" and "wp-image" not in (img.get("class") or []):
        # explicit empty alt is a deliberate "decorative" signal
        return True
    return False


def _classify_links(soup, base_url: str, page_host: str) -> tuple[set[str], set[str]]:
    internal, external = set(), set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        host = urlparse(absolute).netloc
        if not host:
            continue
        (internal if registrable(host) == page_host else external).add(absolute.split("#")[0])
    return internal, external


def _parse_jsonld(soup) -> tuple[list[str], bool]:
    """Collect every @type across all JSON-LD blocks, descending into Yoast-style @graph
    arrays and nested nodes. `valid` = at least one block that parses as JSON is present."""
    types: list[str] = []
    valid = True
    blocks = soup.find_all("script", attrs={"type": "application/ld+json"})
    for b in blocks:
        try:
            data = json.loads(b.string or "")
        except (json.JSONDecodeError, TypeError):
            valid = False
            continue
        _collect_types(data, types)
    return types, (valid and bool(blocks))


def _collect_types(node, out: list[str]) -> None:
    if isinstance(node, list):
        for n in node:
            _collect_types(n, out)
    elif isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, list):
            out.extend(map(str, t))
        elif t:
            out.append(str(t))
        if "@graph" in node:
            _collect_types(node["@graph"], out)


def _mixed_content(soup, final_url: str) -> list[str]:
    """Insecure subresources on an https page. Only true subresources count — an http://
    canonical or alternate <link> is not mixed content, so <link> is limited to stylesheets."""
    if urlparse(final_url).scheme != "https":
        return []
    bad = []
    for tag, attr in (("img", "src"), ("script", "src"), ("iframe", "src")):
        for el in soup.find_all(tag):
            if el.get(attr, "").startswith("http://"):
                bad.append(el[attr])
    for el in soup.find_all("link"):
        rel = " ".join(el.get("rel") or []).lower()
        if "stylesheet" in rel and el.get("href", "").startswith("http://"):
            bad.append(el["href"])
    return bad


# --- broken links -----------------------------------------------------------

def check_links(urls: list[str], limit: int = 50) -> list[dict]:
    """HEAD each internal target; record 4xx/5xx and 301 chains. Capped to keep runs bounded."""
    broken = []
    for u in urls[:limit]:
        try:
            r = fetch(u, method="HEAD")
            if r.status_code in (403, 405, 501):  # server dislikes HEAD — confirm with GET
                r = fetch(u, method="GET")
            if r.status_code >= 400:
                broken.append({"url": u, "status": r.status_code})
            elif len(r.history) > 1:  # multi-hop redirect chain
                broken.append({"url": u, "status": f"chain:{len(r.history)}"})
        except requests.RequestException as e:
            broken.append({"url": u, "status": f"error:{type(e).__name__}"})
    return broken


# --- PageSpeed Insights -----------------------------------------------------

PSI_RETRIES = 3
PSI_BACKOFF_S = 4


def _scrub(text: str) -> str:
    """Remove the PSI key from anything we log or raise.

    requests puts the full request URL in HTTPError, so an upstream 500 wrote the API key
    into the run log in clear text. Logs outlive the incident that produced them.
    """
    key = env("PSI_API_KEY") or ""
    return text.replace(key, "<PSI_API_KEY>") if key else text


def _psi_get(params: dict) -> requests.Response:
    """PSI call with backoff on 5xx.

    Google returns transient 500s under load. Without a retry, one of them anywhere in an
    11-page sweep aborted the entire weekly run — including the pages that had already
    succeeded, since persistence happens after the full pass.
    """
    last = ""
    for attempt in range(PSI_RETRIES):
        try:
            r = requests.get(PSI_ENDPOINT, params=params, timeout=90)
            if r.status_code < 400:
                return r
            if r.status_code < 500 and r.status_code != 429:
                # A 4xx is a verdict, not weather. Retrying a malformed request or a rejected
                # key just burns three attempts and delays the real error.
                raise RuntimeError(_scrub(f"PSI rejected the request: HTTP {r.status_code} "
                                          f"{r.text[:200]}"))
            last = f"HTTP {r.status_code}"
        except requests.RequestException as err:
            last = _scrub(f"{type(err).__name__}: {err}")
        if attempt < PSI_RETRIES - 1:
            time.sleep(PSI_BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"PSI failed after {PSI_RETRIES} attempts: {last}")


# loop-a-onpage.md gates accessibility on FOUR named audits, not the whole category score.
# Gating on a perfect category score is both stricter than the spec and unreachable on a real
# Elementor site, which is how every page sat at "never green" regardless of Tier-1 work.
A11Y_GATE_AUDITS = ("image-alt", "heading-order", "target-size", "font-size")


def _failed_audit_ids(audits: dict, category: dict) -> list[str]:
    """IDs of the audits this category actually failed, so a failure is actionable.

    Storing only a pass/fail boolean meant nobody — human or agent — could see WHY a page
    failed, which made the gate impossible to act on or to argue with.
    """
    failed: list[str] = []
    for ref in (category or {}).get("auditRefs", []):
        audit = audits.get(ref.get("id"), {})
        score = audit.get("score")
        # score is None for informational/manual audits — those are not failures.
        if score is not None and score < 1.0:
            failed.append(ref["id"])
    return sorted(failed)


def run_psi(url: str, api_key: str, strategy: str = "mobile", runs: int = 3) -> dict:
    """runs× PSI calls -> median lab CWV + SEO/A11y pass. Median of an odd N per INFRA.md."""
    seo_scores, a11y_scores = [], []
    lcp, cls, tbt = [], [], []
    seo_failed: list[str] = []
    a11y_failed: list[str] = []
    a11y_gate_failed: list[str] = []
    for _ in range(max(1, runs)):
        params = {"url": url, "strategy": strategy,
                  "category": ["SEO", "ACCESSIBILITY", "PERFORMANCE"]}
        if api_key:
            params["key"] = api_key
        r = _psi_get(params)
        lh = r.json().get("lighthouseResult", {})
        cats = lh.get("categories", {})
        audits = lh.get("audits", {})
        if "seo" in cats and cats["seo"].get("score") is not None:
            seo_scores.append(cats["seo"]["score"])
            seo_failed = _failed_audit_ids(audits, cats["seo"])
        if "accessibility" in cats and cats["accessibility"].get("score") is not None:
            a11y_scores.append(cats["accessibility"]["score"])
            a11y_failed = _failed_audit_ids(audits, cats["accessibility"])
            a11y_gate_failed = [a for a in a11y_failed if a in A11Y_GATE_AUDITS]
        _push(lcp, audits.get("largest-contentful-paint"))
        _push(cls, audits.get("cumulative-layout-shift"))
        _push(tbt, audits.get("total-blocking-time"))

    return {
        "lighthouse_seo_pass": bool(seo_scores) and statistics.median(seo_scores) >= 1.0,
        # Narrowed to the four audits the loop spec actually names.
        "lighthouse_a11y_pass": bool(a11y_scores) and not a11y_gate_failed,
        "lighthouse_seo_failures": seo_failed,
        "lighthouse_a11y_failures": a11y_failed,
        "cwv_lab": {
            "lcp_ms": _median(lcp),
            "cls": _median(cls),
            "tbt_ms": _median(tbt),   # lab proxy; true INP is field-only (Loop B carries it)
            "inp_ms": None,
            "runs": len(lcp),
            "strategy": strategy,
            "note": "INP is a field metric; TBT is the lab proxy. Field CWV lives in seo_weekly.",
        },
    }


def _push(bucket: list[float], audit: dict | None) -> None:
    if audit and audit.get("numericValue") is not None:
        bucket.append(audit["numericValue"])


def _median(vals: list[float]) -> float | None:
    return round(statistics.median(vals), 3) if vals else None


# --- target resolution ------------------------------------------------------

def resolve_targets(cfg: dict) -> list[dict]:
    """Resolve config post IDs to URLs via the WP REST API. `in_scope_urls` in the yaml,
    if present, is used as-is (bypasses resolution). Unresolved IDs are reported, not fatal.

    `in_scope_ids` (pages) and `in_scope_post_ids` (WP posts) are resolved identically — each
    id is tried against both /pages/ and /posts/ regardless of which list it came from, so the
    split is purely for readability/config-authoring, not a different code path.
    """
    base = cfg["site"]["base_url"].rstrip("/")
    scope = cfg.get("scope", {})
    targets: list[dict] = []

    for url in scope.get("in_scope_urls", []) or []:
        targets.append({"url": url, "post_id": None, "page_type": None})

    ids = list(scope.get("in_scope_ids", []) or []) + list(scope.get("in_scope_post_ids", []) or [])
    unresolved = []
    for pid in ids:
        hit = None
        for rest_type in ("pages", "posts"):
            try:
                r = fetch(f"{base}/wp-json/wp/v2/{rest_type}/{pid}")
            except requests.RequestException:
                continue
            if r.status_code == 200:
                data = r.json()
                hit = {"url": data.get("link"), "post_id": pid, "page_type": data.get("type")}
                break
        if hit and hit["url"]:
            targets.append(hit)
        else:
            unresolved.append(pid)

    if unresolved:
        print(f"[audit] could not resolve {len(unresolved)} post ID(s) via WP REST: "
              f"{unresolved} — add explicit URLs under scope.in_scope_urls if these are a custom type.",
              file=sys.stderr)
    return targets


def resolve_url_post_id(base: str, url: str) -> tuple[int | None, str | None]:
    """Resolve a single known URL to its WP post_id/type by slug — what `--url` was missing
    to ever be able to `--apply`. Without a post_id, the WP MCP write path has nothing to
    write to and every change gets queued for a human instead (see agent.run_loop_a_fixes),
    so `--url ... --apply` on an existing page silently never wrote anything until this."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    for rest_type in ("pages", "posts"):
        try:
            r = fetch(f"{base}/wp-json/wp/v2/{rest_type}?slug={slug}")
        except requests.RequestException:
            continue
        if r.status_code == 200:
            for item in r.json():
                if item.get("link", "").rstrip("/") == url.rstrip("/"):
                    return item["id"], item.get("type")
    return None, None


# --- orchestration ----------------------------------------------------------

def audit_url(url: str, psi_key: str | None, runs: int, do_psi: bool, do_links: bool) -> dict:
    resp = fetch(url)
    parsed = parse_html(resp.text, resp.url)
    parsed["broken_links"] = check_links(parsed["_internal_targets"]) if do_links else []
    if do_psi:
        parsed.update(run_psi(url, psi_key or "", runs=runs))
    return parsed


def column_fields(parsed: dict, post_id, page_type) -> dict:
    """Keep only real seo_page_state columns (drop the underscored working values)."""
    cols = {k: v for k, v in parsed.items() if not k.startswith("_")}
    if post_id is not None:
        cols["post_id"] = post_id
    if page_type is not None:
        cols["page_type"] = page_type
    return cols


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Loop A technical audit (PSI + HTML parse).")
    ap.add_argument("--site", help="Site slug / config to load (default from SITE_CONFIG).")
    ap.add_argument("--url", help="Audit a single explicit URL instead of the config scope.")
    ap.add_argument("--runs", type=int, default=3, help="PSI runs to median (default 3).")
    ap.add_argument("--no-psi", action="store_true", help="Skip PageSpeed Insights (HTML parse only).")
    ap.add_argument("--no-links", action="store_true", help="Skip broken-link checking.")
    ap.add_argument("--dry-run", action="store_true", help="Print JSON; do not write to Postgres.")
    args = ap.parse_args(argv)

    cfg = load_site_config(f"config/{args.site}.yaml" if args.site else None)
    slug = site_slug(cfg)
    psi_key = env("PSI_API_KEY")
    do_psi = not args.no_psi and bool(psi_key)
    if not args.no_psi and not psi_key:
        print("[audit] PSI_API_KEY not set — skipping Lighthouse/CWV (HTML parse only).", file=sys.stderr)

    if args.url:
        pid, ptype = resolve_url_post_id(cfg["site"]["base_url"].rstrip("/"), args.url)
        if pid is None:
            print(f"[audit] could not resolve a post_id for {args.url} by slug — "
                  f"--apply will have nothing to write to.", file=sys.stderr)
        targets = [{"url": args.url, "post_id": pid, "page_type": ptype}]
    else:
        targets = resolve_targets(cfg)
    if not targets:
        print("[audit] no targets resolved.", file=sys.stderr)
        return 1

    run_id = None if args.dry_run else db.start_run(slug, "loop_a")
    results: dict[str, dict] = {}
    failures: list[tuple[str, str]] = []
    try:
        # first pass: audit every page, collect outbound internal targets for the graph.
        # Per-page isolation matters because persistence happens in the SECOND pass: an
        # exception here used to discard every page already audited, so one transient PSI
        # error mid-sweep cost the whole weekly run.
        for t in targets:
            try:
                parsed = audit_url(t["url"], psi_key, args.runs, do_psi, not args.no_links)
                results[t["url"]] = {"parsed": parsed, "target": t}
            except Exception as err:
                failures.append((t["url"], _scrub(f"{type(err).__name__}: {err}")[:300]))
                print(f"[audit] FAILED {t['url']}: {failures[-1][1]}", file=sys.stderr)
        if not results:
            raise RuntimeError(f"every target failed; first error: {failures[0][1]}")

        # prior state, for content-drift detection (one query, not one per page)
        prior = {} if args.dry_run else {r["url"]: r for r in db.list_page_state(slug)}

        # second pass: inbound internal-link counts across the in-scope set (orphan check)
        for url, r in results.items():
            inbound = sum(
                1 for other, o in results.items()
                if other != url and url in set(o["parsed"]["_internal_targets"])
            )
            fields = column_fields(r["parsed"], r["target"]["post_id"], r["target"]["page_type"])
            fields["internal_links_in"] = inbound

            # Supplementary, not authoritative: Yoast's own readability score, read for free
            # where a human already produced one. Isolated in its own try — a WP MCP hiccup
            # here should not cost the page its real, measured fields below.
            if r["target"].get("post_id"):
                try:
                    fields["yoast_readability_score"] = wp_mcp.get_yoast_readability_score(
                        r["target"]["post_id"])
                except Exception as err:
                    print(f"[audit] could not fetch Yoast readability for {url}: {err}",
                          file=sys.stderr)

            # The verdict is arithmetic over measured fields, so it is settled here rather
            # than asked of a model — same inputs always give the same answer.
            verdict = checklist.evaluate(fields, cfg)
            fields["checklist_status"] = verdict["status"]

            was = prior.get(url) or {}
            # Record body-copy drift. Cheap to store, and the reason it earns its place is
            # diagnostic: when rankings slide, the first question is whether the page changed.
            if was.get("content_sha") and fields.get("content_sha") != was["content_sha"]:
                entry = {"ts": datetime.now(timezone.utc).isoformat(), "field": "content",
                         "old": was["content_sha"], "new": fields["content_sha"], "by": "audit"}
                fields["changelog"] = (was.get("changelog") or []) + [entry]
                print(f"[audit] content changed: {url} ({was['content_sha']} -> "
                      f"{fields['content_sha']})", file=sys.stderr)

            if args.dry_run:
                print(json.dumps({"url": url, "fields": fields,
                                  "verdict": verdict}, indent=2, default=str))
            else:
                db.upsert_page_state(slug, url, fields)

        status = "partial" if failures else "ok"
        if run_id:
            db.end_run(run_id, status,
                       "; ".join(f"{u}: {e}" for u, e in failures) if failures else None)
        print(f"[audit] {'(dry-run) ' if args.dry_run else ''}audited {len(results)}/"
              f"{len(targets)} page(s) for '{slug}'"
              f"{f' — {len(failures)} FAILED' if failures else ''}.", file=sys.stderr)
        return 0
    except Exception as e:
        if run_id:
            db.end_run(run_id, "failed", _scrub(str(e)))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
