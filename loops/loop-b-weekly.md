# Loop B — Weekly SEO Opportunity Sweep (Signal Sanctuary)

Type: Scheduled measurement + prioritization (NOT a convergence loop)
Trigger: weekly host systemd timer -> one-shot container -> claude -p
Inputs: Search Console API pull + CodeManager baseline (last week's snapshot + Loop A status)
Output: ranked action queue in CM; targets handed to Loop A; editorial list for human; Discord digest

## Important framing
This does NOT "loop until ranking improves" — you cannot converge a fast loop against a metric that
takes days-to-weeks to move. It runs ONCE per invocation, produces a ranked queue, and exits. The only
until-condition is: every opportunity above threshold has an action logged. The weekly timer IS the
loop; next week's GSC pull is the judge of last week's changes.

## Inputs
- GSC API (last 7d vs prior 7d): query, page, position, CTR, impressions, clicks
- GSC indexing status per URL (indexed / not)
- Field CWV / CrUX buckets (Good / Needs-Improvement / Poor) — signal only
- CodeManager: last week's baseline + Loop A checklist_status per URL

## Analysis — diff vs last week, then rank opportunities
1. Striking-distance queries: avg position ~5–15. Highest ROI; small nudges move these.
2. High-impression / low-CTR pages: title+meta rewrite candidates (no content change needed).
3. Droppers: pages that lost position since last pull. Investigate + queue.
4. Query gaps: queries earning impressions with NO dedicated page -> editorial feedstock for /thesignal.
5. Indexing failures: Loop-A-optimized pages NOT indexed -> HARD FLAG (on-page work isn't landing).
6. Field CWV regressions: pages slipping Good -> Needs-Improvement -> signal, not gate.

## Output / hand-off
- Write ranked action list to CM seo_weekly.opportunities (with rationale + metric deltas per item).
- Hand top N on-page targets (categories 1,2,3,5) to Loop A's queue for next run.
- Emit query gaps (category 4) as a SEPARATE human-review editorial list — Loop B does not write content.
- Post a digest to Discord via webhook (top movers, new gaps, indexing flags).

## Boundaries
- Does NOT write or edit page content or health claims.
- Does NOT auto-apply changes — it prioritizes and queues. Loop A (with its tiers/gates) applies.
- Ranking must cite the metric that justifies each item (position, CTR, impressions, delta), so the
  queue is auditable rather than a black box.

## Orchestration (plain systemd + flock; see deploy/)
Host systemd timer (Mon 06:00, Persistent=true) -> flock -> docker compose run --rm loop-runner
python -m scripts.run_loop_b. State lives in CodeManager, not the scheduler. Revisit OpenClaw only if
this grows to many loops/sites or needs retries + event triggers + channel delivery as first-class.
