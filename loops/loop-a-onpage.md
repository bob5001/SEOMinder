# Loop A — On-Page / Technical SEO (Signal Sanctuary)

Type: Verifiable loop · Trigger: manual /goal or on-publish · Blast radius: deliberately small in v1
State store: Neon Postgres (SEO schema, keyed by site) · Reads/writes via: Signal Sanctuary WP MCP + audit.py

## Objective
Bring every in-scope page up to a known-good on-page/technical standard, verified against a
deterministic checklist, WITHOUT manual per-page inspection. The win is automated, repeatable
verification — not clever optimization. Do the minimum edit that turns a red check green, then stop.
This is NOT a "rank higher" or "score as high as possible" loop. It converges on the checklist
going all-green and halts.

## Scope (authoritative list lives in config/<site>.yaml)
IN — informational/content pages: 2384, 2524, 2509, 2183, 2182, 2181, 2180, 2189, 206, 140, 2691
  PLUS any newly published /thesignal post (on-publish trigger).
OUT — do not touch: Cart 1694, Checkout 1718, any product type, quiz-result 2184–2188, UGC 1937.
FLAG do not fix: 2833 "FAQ (Schema Test Copy)" — published indexable test duplicate.

## Gate checklist (stopping condition) — page green only when ALL pass
Metadata (Tier 1, auto-fix, page-level Yoast meta, safe):
  [ ] SEO title set (_yoast_wpseo_title), ~50–60 chars, unique
  [ ] Meta description set (_yoast_wpseo_metadesc), ~150–160 chars, unique
  [ ] Canonical resolves to self/correct; no unintended noindex
On-page body (Tier 2, Elementor JSON — DETECT always, auto-fix only once write path proven):
  [ ] Every substantive image has non-empty descriptive alt
  [ ] Clean heading semantics: exactly one H1, logical H2/H3 order, no empty-<p>-in-heading junk
  [ ] >=2 contextual internal links OUT to siblings, and linked to FROM >=2 siblings (no orphans)
Technical (auto-detect via audit.py):
  [ ] Lighthouse SEO audit: all checks pass (via PSI API)
  [ ] Lighthouse Accessibility audit: alt, heading-order, tap-target, font-size pass
  [ ] Indexable: not noindex, not robots-blocked, present in XML sitemap
  [ ] No broken links (4xx/5xx), no 301 chains, no mixed content
  [ ] Schema present for page type AND validates: FAQPage on FAQ, Organization sitewide
  [ ] Lab CWV within tolerance band — median of 3 runs, never exact (LCP<=2.5s, CLS<=0.1, INP<=200ms)

## Fix tiers (blast-radius control)
Tier 1 auto-fix now: Yoast title/description/canonical via wp_update_post_meta. Independent of Elementor.
Tier 2 detect+queue in v1; auto-fix only after Elementor-edit path validated on a scratch page:
  alt text, heading markup, in-body internal links — all require _elementor_data JSON surgery +
  CSS-cache bust. High fragility; do not blindly rewrite the blob.
Tier 3 flag for human, never auto-fix: any health/medical substance, thin-content rewrite, near-dup merge.
Graduating a check Tier 2 -> auto-fix is an explicit human decision, logged to the CodeManager broker
(a governance action, not page state).

## YMYL boundary (hard rule)
This is a .health domain making health claims. You may NOT rewrite, soften, strengthen, or "optimize"
any health/medical claim. Stay on titles, meta, alt, heading STRUCTURE (not the words' meaning),
internal links, and technical checks. If a fix needs changing a claim's substance, stop and queue it.

## What NOT to do
- Do not gate on / optimize toward the Yoast COMPOSITE score (rewards stuffing). Use sub-checks only.
- Do not gate on readability/Flesch — this audience needs the domain vocabulary.
- Do not chase field CWV / position / CTR / indexing confirmation here — those are Loop B signals.
- Do not add links to out-of-scope commercial pages to inflate counts.
- Do not touch the FAQ test-copy page.

## Workflow per page
1. Read: wp_get_post_snapshot (source-of-truth meta) + audit.py (rendered H1, alt, link graph, CWV,
   schema, status codes).
2. Compare against checklist; record each pass/fail.
3. Fix Tier 1 via MCP meta writes. Detect Tier 2/3 and queue.
4. Re-verify by re-reading (writes bust caches).
5. Write per-URL result to Postgres (seo_page_state): status, changes, timestamp, queued items.
6. Next page. Loop until all in-scope pages green (or queued where auto-fix not allowed).

## On-publish variant
New /thesignal post -> run the same checklist against that single post so new content ships green.
Same Tier rules, same YMYL boundary.
