"""The Loop A gate verdict — computed, never generated.

`checklist_status` was originally something the model returned. It shouldn't be: it is
measured fields compared against thresholds, which is arithmetic with a right answer. Asking
a model for it put model quality in the path of a safety-relevant field for no benefit, and
made the verdict vary between runs. The model proposes text; this module decides.

Four outcomes, because three could not express the situation this site is actually in:

    green    every gate passes — the loop's stopping condition
    queued   something outstanding that the loop or a human editor can fix
             (Tier 1 metadata, Tier 2 body work)
    blocked  nothing left that anyone in this loop can fix — only infrastructure
             tolerances remain (Lighthouse category score, lab CWV)
    failing  something is broken rather than merely unmet: not indexable, broken
             links, invalid schema

The `blocked` state is the point. Every in-scope page currently fails Lighthouse SEO and
most miss the lab LCP band — neither of which a Yoast metadata edit can move. Without a
terminal state for "the agent has done all it can", the loop's "converge to all-green and
halt" condition is unreachable and the report says `—` for every page forever.
"""
from __future__ import annotations

# Which tier owns each failure — this is what separates queued from blocked.
TIER1 = "tier1"   # Loop A can fix now (Yoast metadata)
TIER2 = "tier2"   # a human editor can fix (Elementor body: alt, headings, links)
BROKEN = "broken"  # an error, not an unmet target
INFRA = "infra"   # outside this loop entirely — hosting, theme, platform

# A failing Lighthouse SEO category is NOT automatically infrastructure, and assuming it was
# is a mistake worth naming: /support/ fails that category solely on `meta-description`, which
# is the exact field Loop A writes. Treating the category as one opaque infra failure would
# have parked pages in `blocked` that the loop can actually finish. Ownership follows the
# individual audit, not the category. Unlisted ids fall through to INFRA.
LIGHTHOUSE_AUDIT_TIER: dict[str, str] = {
    # Loop A writes these directly.
    "document-title": TIER1,
    "meta-description": TIER1,
    # Body-level edits — a human in Elementor.
    "link-text": TIER2,
    "crawlable-anchors": TIER2,
    "image-alt": TIER2,
    "heading-order": TIER2,
    # Genuinely broken rather than merely unmet.
    "http-status-code": BROKEN,
    "is-crawlable": BROKEN,
    "canonical": BROKEN,
    # Platform-level: robots.txt, hreflang, viewport are not content edits.
    "robots-txt": INFRA,
    "hreflang": INFRA,
    "viewport": INFRA,
}


def evaluate(page: dict, cfg: dict) -> dict:
    """Return {"status", "reasons": [{check, tier, detail}]} for one seo_page_state row."""
    th = cfg.get("thresholds", {})
    t = th.get("title_len", {})
    m = th.get("metadesc_len", {})
    cwv = th.get("cwv_lab", {})
    reasons: list[dict] = []

    def fail(check: str, tier: str, detail: str) -> None:
        reasons.append({"check": check, "tier": tier, "detail": detail})

    # --- Tier 1: metadata, what Loop A writes -------------------------------
    tl = page.get("title_len") or 0
    if not (t.get("min", 50) <= tl <= t.get("max", 60)):
        fail("title_len", TIER1, f"{tl} chars, want {t.get('min', 50)}-{t.get('max', 60)}")
    ml = page.get("metadesc_len") or 0
    if not (m.get("min", 150) <= ml <= m.get("max", 160)):
        fail("metadesc_len", TIER1,
             f"{ml} chars, want {m.get('min', 150)}-{m.get('max', 160)}")

    # --- Tier 2: body work, detect + queue in V1 ----------------------------
    if (page.get("h1_count") or 0) != 1:
        fail("h1_count", TIER2, f"{page.get('h1_count')} H1s, want exactly 1")
    if page.get("heading_order_ok") is False:
        fail("heading_order", TIER2, "heading levels skip or nest illogically")
    if (page.get("alt_coverage_pct") or 0) < 100.0:
        fail("alt_coverage", TIER2, f"{page.get('alt_coverage_pct')}% of images have alt text")
    if (page.get("internal_links_out") or 0) < th.get("internal_links_out_min", 2):
        fail("internal_links_out", TIER2, f"{page.get('internal_links_out')} outbound")
    if (page.get("internal_links_in") or 0) < th.get("internal_links_in_min", 2):
        fail("internal_links_in", TIER2, f"{page.get('internal_links_in')} inbound (orphan risk)")
    for aid in (page.get("lighthouse_a11y_failures") or []):
        # Only the four gate audits block; the rest are recorded, not gating.
        if aid in ("image-alt", "heading-order", "target-size", "font-size"):
            fail(f"a11y:{aid}", TIER2, "Lighthouse accessibility gate audit failed")

    # --- Broken: errors, not unmet targets ----------------------------------
    if page.get("indexable") is False:
        fail("indexable", BROKEN, "page is noindex")
    broken_links = page.get("broken_links") or []
    if broken_links:
        fail("broken_links", BROKEN, f"{len(broken_links)} broken link(s)")
    if page.get("schema_valid") is False:
        fail("schema_valid", BROKEN, f"schema present but invalid ({page.get('schema_type')})")

    # --- Infra: real, tracked, but nothing this loop can do -----------------
    if page.get("lighthouse_seo_pass") is False:
        ids = page.get("lighthouse_seo_failures") or []
        if not ids:
            fail("lighthouse_seo", INFRA, "Lighthouse SEO below 100 (audit ids not captured)")
        for aid in ids:
            # Attribute each failing audit to whoever can actually fix it.
            fail(f"lighthouse_seo:{aid}", LIGHTHOUSE_AUDIT_TIER.get(aid, INFRA),
                 "Lighthouse SEO audit failed")
    lab = page.get("cwv_lab") or {}
    lcp_ms, lcp_max = lab.get("lcp_ms"), cwv.get("lcp_s", 2.5) * 1000
    if lcp_ms is not None and lcp_ms > lcp_max:
        # Demoted from a gate: a metadata edit cannot move server and theme render time.
        fail("cwv_lab_lcp", INFRA, f"lab LCP {lcp_ms:.0f}ms, band <={lcp_max:.0f}ms")
    if lab.get("cls") is not None and lab["cls"] > cwv.get("cls", 0.1):
        fail("cwv_lab_cls", INFRA, f"lab CLS {lab['cls']}, band <={cwv.get('cls', 0.1)}")

    tiers = {r["tier"] for r in reasons}
    if not reasons:
        status = "green"
    elif BROKEN in tiers:
        status = "failing"
    elif tiers & {TIER1, TIER2}:
        status = "queued"
    else:
        status = "blocked"
    return {"status": status, "reasons": reasons}


def summarize(reasons: list[dict]) -> str:
    """One-line human summary, for the report and the propose-only diff."""
    if not reasons:
        return "all gates pass"
    by_tier: dict[str, list[str]] = {}
    for r in reasons:
        by_tier.setdefault(r["tier"], []).append(r["check"])
    order = [TIER1, TIER2, BROKEN, INFRA]
    return "; ".join(f"{tier}: {', '.join(by_tier[tier])}"
                     for tier in order if tier in by_tier)
