"""Agent step for the loops — model-agnostic, structured-JSON generation (NOT an agentic loop).

The model is handed the state we already gathered and asked to return JSON matching a schema;
the orchestrator (db.py / the WP MCP write path) acts on it. Because we validate the JSON and
gate every write, model quality affects output *quality*, never *safety* — which is why any
model, including local ones, can run these tasks, and why the bake-off is meaningful.

The provider/model for each task comes from config/models.yaml via scripts.models. Nothing
here is bound to a specific provider.

Contract:
  * The agent NEVER writes to Postgres. It returns JSON; the orchestrator persists via db.py.
  * The agent does NOT judge the checklist either. `checklist_status` is measured fields versus
    thresholds — arithmetic with a right answer — so scripts.checklist computes it and audit.py
    persists it. The model proposes text; nothing safety-relevant depends on its judgement.
  * The only live CONTENT writes are Yoast meta via WP MCP on Loop A, Tier-gated: Tier 1
    auto-fix; Tier 2/3 detect + queue for a human. WP backs up daily (7 days) — rollback net.
  * Out-of-band, non-content work (redirects, hosting/CWV, Tier-3) is recorded in Postgres with
    the rest of the state. CodeManager stays a project-level agent-knowledge layer; this tool
    has to run without it.

Two gates, deliberately separate (measure twice, cut once):
  WIRED     the agent step runs at all.
  --apply   proposals are actually written to WordPress. Off by default, so a wired run
            proposes, persists state, and prints a diff without touching the live site.

--apply writes through scripts.wp_mcp (JSON-RPC 2.0 over the site's WP MCP plugin) — plain WP
REST cannot do this write, Yoast's fields aren't `show_in_rest`. Loop B has no content write, so
`run_loop_b_ranking` needed no gate on this at all.

Loop A runs as ONE call over the whole in-scope set. Titles and descriptions have to be
unique, and uniqueness is a property of the set — a model shown one page at a time cannot
satisfy it. The on-publish variant is the same call with a batch of one plus the metadata
already in use elsewhere (`taken`), so a new post cannot collide with the existing pages.

Bake-off interface (scripts.bakeoff scores; it calls these):
  from scripts.agent import LOOP_A_BATCH_SCHEMA, LOOP_A_SYSTEM, loop_a_prompt
  from scripts.models import generate_json, candidates_for
  prompt = loop_a_prompt(fixture_pages, cfg)          # a LIST of pages
  for cand in candidates_for("loop_a_meta"):
      gen = generate_json(cand, prompt, LOOP_A_BATCH_SCHEMA, system=LOOP_A_SYSTEM)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import checklist, db, wp_mcp
from .models import Generation, generate_json, preflight, route_for

# Gate 1: does the agent step run at all?
WIRED = True


def agent_available() -> bool:
    """Whether the routed provider for the agent step can actually be reached.

    Previously this checked for ANTHROPIC_API_KEY, which is now the wrong signal twice over:
    production routes through the Claude CLI on the subscription (no API key), and local
    models need no credential at all. It also silently disappeared in an earlier refactor
    while both orchestrators kept calling it — masked only because `not WIRED` short-circuits
    first, so flipping WIRED would have raised AttributeError on the first real run.
    """
    for task in ("loop_a_meta", "loop_b_rank"):
        try:
            ok, _ = preflight(route_for(task))
        except Exception:
            return False
        if not ok:
            return False
    return True

# The hard YMYL rule, injected into every content-generation prompt.
YMYL_BOUNDARY = (
    "This is a .health domain making health claims (YMYL). You may NOT invent, alter, "
    "strengthen, soften, or reinterpret any health/medical claim. Work only with wording of "
    "titles, meta descriptions, and alt text — never change what a claim asserts. If a good "
    "result would require changing a claim's substance, do not; queue it for a human instead."
)

# --- Loop A -----------------------------------------------------------------

LOOP_A_SYSTEM = (
    "You are an on-page SEO editor. You propose Tier-1 metadata fixes (Yoast SEO title and meta "
    "description) and judge a per-page checklist. You return only JSON matching the schema. "
    + YMYL_BOUNDARY
)

# Canonical Loop A contract — one page in, proposed fixes + verdict out.
LOOP_A_PAGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    # No checklist_status here on purpose: the verdict is computed by scripts.checklist from
    # measured fields, not generated. Asking a model to judge it made a safety-relevant field
    # vary between runs for no gain.
    "required": ["url", "changes", "manual_queue"],
    "properties": {
        "url": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                # PROPERTY ORDER IS LOAD-BEARING. Constrained decoders emit fields in schema
                # order, so anything placed after `new` is written *after* the model has
                # already committed to the text — post-hoc justification, not reasoning.
                # `source_quote` comes first so the model must point at the page's own words
                # before it paraphrases them, and the orchestrator can then verify the quote
                # is really in the page. That turns YMYL from a keyword heuristic into a
                # grounding check.
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "source_quote", "new", "tier"],
                "properties": {
                    "field": {"type": "string", "enum": ["yoast_title", "yoast_metadesc"]},
                    "source_quote": {
                        "type": "string",
                        "description": ("Verbatim span copied from this page's content excerpt "
                                        "carrying the claim the new text must preserve. Empty "
                                        "string only if the page makes no claim of any kind."),
                    },
                    "rationale": {"type": "string"},
                    "old": {"type": ["string", "null"]},
                    "new": {"type": "string"},
                    "tier": {"type": "integer", "enum": [1]},
                },
            },
        },
        "manual_queue": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tier", "check", "note"],
                "properties": {
                    "tier": {"type": "integer", "enum": [2, 3]},
                    "check": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        },
        "notes": {"type": "string"},
    },
}


# Loop A runs as ONE call over the whole in-scope set, not one call per page. The checklist
# requires titles and descriptions to be *unique*, and uniqueness is a property of the set —
# a model shown one page in isolation cannot honour it, and reconciling collisions afterwards
# means a second round of rewrites that can themselves collide. One call, global context.
LOOP_A_BATCH_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pages"],
    "properties": {"pages": {"type": "array", "items": LOOP_A_PAGE_SCHEMA}},
}

# Excerpt budget per page in batch mode. The whole set shares one context window, so the
# 1400-char fixture excerpts get trimmed — enough to know what a page is about, short enough
# that eleven of them still fit a small local model.
BATCH_EXCERPT_CHARS = 700


def loop_a_prompt(pages: list[dict], cfg: dict, taken: dict | None = None) -> str:
    """Assemble the Loop A prompt for a SET of pages.

    `pages` are seo_page_state-shaped dicts, each with a "content_excerpt" so the model can
    write a meta description that is actually about the page. `taken` optionally carries
    {"titles": [...], "descs": [...]} already in use elsewhere on the site — that is what makes
    the on-publish variant work: a batch of one that still cannot collide with the other pages.
    """
    th = cfg.get("thresholds", {})
    t = th.get("title_len", {})
    m = th.get("metadesc_len", {})
    t_min, t_max = t.get("min", 50), t.get("max", 60)
    m_min, m_max = m.get("min", 150), m.get("max", 160)
    brand = (cfg.get("site", {}) or {}).get("name", "")
    suffix = f" - {brand}" if brand else ""

    blocks = []
    for i, page in enumerate(pages, 1):
        excerpt = (page.get("content_excerpt") or "(not provided)")[:BATCH_EXCERPT_CHARS]
        blocks.append(
            f"--- PAGE {i} ---\n"
            f"url: {page.get('url')}\n"
            f"current title ({page.get('title_len')} chars): {page.get('title')!r}\n"
            f"current meta description ({page.get('metadesc_len')} chars): {page.get('metadesc')!r}\n"
            f"content excerpt: {excerpt!r}"
        )

    taken = taken or {}
    taken_block = ""
    if taken.get("titles") or taken.get("descs"):
        taken_block = (
            "\nAlready in use on other pages of this site — your output must not duplicate "
            f"or closely paraphrase any of these:\ntitles: {taken.get('titles', [])}\n"
            f"descriptions: {taken.get('descs', [])}\n"
        )

    return (
        f"You are writing Yoast metadata for {len(pages)} pages of one site, in a single pass, "
        f"so that you can keep every title and description distinct from every other.\n\n"
        + "\n\n".join(blocks) + "\n"
        + taken_block +
        f"\nTargets: title {t_min}-{t_max} chars, meta description {m_min}-{m_max} chars.\n\n"
        f"Title format: write the COMPLETE literal title exactly as it should render in the "
        f"<title> tag — it is written to the Yoast title field verbatim, with nothing appended. "
        f"End it with {suffix!r} so the brand survives in search results, and count those "
        f"characters toward the {t_min}-{t_max} band. Drop the suffix only if keeping it would "
        f"push the title past {t_max} characters.\n\n"
        "Tasks:\n"
        f"1. Return `pages`: exactly {len(pages)} entries, one per page above, in the same order, "
        "each echoing that page's url verbatim.\n"
        "2. For each page, if the title or meta description is outside its target band, missing, "
        "or weak, propose a Tier-1 replacement (field yoast_title / yoast_metadesc) that fits the "
        "band and reflects the page accurately. Do not touch health-claim substance.\n"
        "   For every change, first fill `source_quote` with a span copied VERBATIM from that "
        "page's content excerpt above — the words carrying the claim your new text has to "
        "preserve. Copy it character for character; it is checked against the page. Then write "
        "`new` so it says no more than the quote does: do not add an effect the page does not "
        "state, and keep any hedge the page uses ('may', 'can help', 'some people report') "
        "rather than asserting it flatly. Use an empty quote only if the page makes no claim.\n"
        "3. EVERY title must be distinct from every other title in your response, and every "
        "description distinct from every other description. Pages on similar topics must be "
        "differentiated by what is actually specific to each one, not by padding.\n"
        "4. Anything that needs body edits (alt text, headings, internal links) or a health-claim "
        "rewrite is NOT Tier 1 — put it in that page's manual_queue as tier 2 (structure) or "
        "tier 3 (claims).\n"
        "5. Do not judge whether a page passes overall — that is measured separately. Return "
        "only what you would change and what you would queue.\n"
        "Return only the JSON object."
    )


def loop_a_max_tokens(n_pages: int) -> int:
    """Output budget for a batch. One page's entry (url, status, title, description, tiering)
    runs ~250 tokens, so the 2048 default silently truncates anything past a handful of pages —
    which surfaces as a schema failure that looks like the model's fault instead of ours."""
    return max(2048, 400 * n_pages)


def propose_loop_a(pages: list[dict], cfg: dict, taken: dict | None = None,
                   model_ref: dict | None = None) -> Generation:
    """Run the whole in-scope set through the routed (or given) model in one call."""
    return generate_json(model_ref or route_for("loop_a_meta"),
                         loop_a_prompt(pages, cfg, taken), LOOP_A_BATCH_SCHEMA,
                         system=LOOP_A_SYSTEM, max_tokens=loop_a_max_tokens(len(pages)))


# --- orchestrator-side validation (the agent proposes; db.py remains the only writer) ------

def bands(cfg: dict) -> dict[str, tuple[int, int]]:
    th = cfg.get("thresholds", {})
    t, m = th.get("title_len", {}), th.get("metadesc_len", {})
    return {"yoast_title": (t.get("min", 50), t.get("max", 60)),
            "yoast_metadesc": (m.get("min", 150), m.get("max", 160))}


def out_of_band(payload: dict, cfg: dict) -> list[dict]:
    """Changes whose text misses its character band.

    No model in this class counts characters reliably, and it is not worth prompt budget to
    pretend otherwise — the rule is deterministic, so the orchestrator measures and the model
    gets told the exact overage. Note this is a *business* rule: generate_json's repair round
    only fixes JSON validity and would happily pass a 67-character title.
    """
    limits = bands(cfg)
    misses: list[dict] = []
    for page in payload.get("pages", []):
        for change in page.get("changes", []):
            lo, hi = limits.get(change.get("field"), (0, 10_000))
            n = len(change.get("new", ""))
            if not (lo <= n <= hi):
                misses.append({"url": page.get("url"), "field": change["field"],
                               "text": change["new"], "len": n, "min": lo, "max": hi})
    return misses


def refine_lengths(payload: dict, pages: list[dict], cfg: dict, model_ref: dict | None = None,
                   max_rounds: int = 2, base_taken: dict | None = None) -> tuple[dict, list[dict]]:
    """Re-ask only the pages that missed a band, feeding back the measured overage.

    Re-running the whole batch to fix two long titles wastes the expensive path and risks
    perturbing text that was already correct. So each round re-asks just the offending pages —
    and passes every accepted title/description as `taken`, which is exactly what that
    parameter is for: a partial re-ask that still cannot collide with what we are keeping.
    `base_taken` seeds this with titles/descs already live on OTHER pages of the site (see
    run_loop_a_fixes) — required when `pages` is a subset, or a retry could reproduce a title
    that already exists outside the batch it can see.

    Returns (merged_payload, remaining_misses).
    """
    merged = json.loads(json.dumps(payload))          # don't mutate the caller's payload
    by_url = {p.get("url"): p for p in merged.get("pages", [])}
    by_url_src = {p["url"]: p for p in pages}

    for _ in range(max_rounds):
        misses = out_of_band(merged, cfg)
        if not misses:
            break
        bad_urls = sorted({m["url"] for m in misses})
        retry_pages = [by_url_src[u] for u in bad_urls if u in by_url_src]
        if not retry_pages:
            break

        # Everything we are keeping is off-limits for the retry, plus anything already live
        # elsewhere on the site.
        base = base_taken or {}
        taken = {"titles": list(base.get("titles", [])), "descs": list(base.get("descs", []))}
        for url, page in by_url.items():
            if url in bad_urls:
                continue
            for change in page.get("changes", []):
                key = "titles" if change["field"] == "yoast_title" else "descs"
                taken[key].append(change["new"])

        detail = "\n".join(
            f"- {m['url']} {m['field']}: {m['len']} chars, must be {m['min']}-{m['max']} "
            f"({'shorten by ' + str(m['len'] - m['max']) if m['len'] > m['max'] else 'lengthen by ' + str(m['min'] - m['len'])}) "
            f"— was: {m['text']!r}"
            for m in misses)
        prompt = (loop_a_prompt(retry_pages, cfg, taken) +
                  "\n\nA previous attempt missed the character bands. Fix exactly these, keeping "
                  "the meaning and the source_quote grounding:\n" + detail)

        gen = generate_json(model_ref or route_for("loop_a_meta"), prompt, LOOP_A_BATCH_SCHEMA,
                            system=LOOP_A_SYSTEM, max_tokens=loop_a_max_tokens(len(retry_pages)))
        for page in gen.data.get("pages", []):
            if page.get("url") in by_url:
                by_url[page["url"]] = page
        merged["pages"] = list(by_url.values())

    return merged, out_of_band(merged, cfg)

def collisions(payload: dict) -> dict[str, list[str]]:
    """Duplicate titles/descriptions the model returned despite being asked for distinct ones.

    A batch call makes uniqueness *achievable*; it does not make it *guaranteed*. The
    orchestrator checks before persisting — a collision is a queue-for-human, never a write.
    """
    out: dict[str, list[str]] = {"titles": [], "descs": []}
    for field, key in (("yoast_title", "titles"), ("yoast_metadesc", "descs")):
        seen: dict[str, int] = {}
        for page in payload.get("pages", []):
            for change in page.get("changes", []):
                if change.get("field") == field:
                    seen[change["new"]] = seen.get(change["new"], 0) + 1
        out[key] = sorted(v for v, n in seen.items() if n > 1)
    return out


def reconcile(payload: dict, pages: list[dict]) -> dict:
    """Check a batch proposal against the request before anything is persisted.

    Returns {"ok": bool, "errors": [...], "by_url": {url: page_payload}}. Catches the three
    ways a batch reply goes wrong: a page missing, a url the model invented, and duplicate
    metadata. Coverage is keyed on url rather than position — order is requested, not trusted.
    """
    requested = [p["url"] for p in pages]
    returned = {p.get("url"): p for p in payload.get("pages", [])}
    errors: list[str] = []

    missing = [u for u in requested if u not in returned]
    unexpected = [u for u in returned if u not in requested]
    if missing:
        errors.append(f"no proposal returned for {len(missing)} page(s): {missing}")
    if unexpected:
        errors.append(f"proposals returned for url(s) never requested: {unexpected}")

    dupes = collisions(payload)
    if dupes["titles"]:
        errors.append(f"duplicate titles: {dupes['titles']}")
    if dupes["descs"]:
        errors.append(f"duplicate meta descriptions: {dupes['descs']}")

    return {"ok": not errors, "errors": errors, "by_url": returned}


# --- Loop B -----------------------------------------------------------------

LOOP_B_SYSTEM = (
    "You are an SEO analyst. Given a week of Search Console data plus last week's baseline, you "
    "rank opportunities and surface editorial gaps. Cite the metric that justifies each item. You "
    "do not write content or edit pages. Return only JSON matching the schema. " + YMYL_BOUNDARY
)

# Canonical Loop B contract — one weekly run in, ranked queue + editorial gaps out.
LOOP_B_RUN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["opportunities", "editorial_gaps"],
    "properties": {
        "opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "url_or_query", "metric", "priority"],
                "properties": {
                    "type": {"type": "string",
                             "enum": ["striking_distance", "low_ctr", "dropper", "indexing", "cwv"]},
                    "url_or_query": {"type": "string"},
                    "metric": {"type": "string"},
                    "delta": {"type": ["number", "string", "null"]},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "handed_to_loop_a": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
            },
        },
        "editorial_gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query", "impressions", "suggested_page"],
                "properties": {
                    "query": {"type": "string"},
                    "impressions": {"type": "integer"},
                    "suggested_page": {"type": "string"},
                },
            },
        },
    },
}


def loop_b_prompt(weekly: dict, cfg: dict) -> str:
    """Assemble the Loop B prompt from a seo_weekly-shaped dict (per_url + cwv_field)."""
    th = cfg.get("thresholds", {})
    sd = th.get("striking_distance", {})
    return (
        f"GSC window: {weekly.get('gsc_window')}\n"
        f"Per-URL data (current vs prior): {json.dumps(weekly.get('per_url', []))[:6000]}\n"
        f"Field CWV: {json.dumps(weekly.get('cwv_field', []))[:2000]}\n\n"
        f"Rank opportunities by ROI. Categories: striking_distance "
        f"(avg position {sd.get('min_position', 5)}-{sd.get('max_position', 15)}), low_ctr "
        f"(high impressions, low CTR — title/meta candidate), dropper (lost position vs prior), "
        f"indexing (in-scope page not indexed — hard flag), cwv (field regression, signal only). "
        f"Cite the justifying metric on each. Set handed_to_loop_a=true for on-page items Loop A can "
        f"fix. Emit editorial_gaps for queries with impressions but no dedicated page. Return only JSON."
    )


def propose_loop_b(weekly: dict, cfg: dict, model_ref: dict | None = None) -> Generation:
    """Run one weekly row through the routed (or given) model and return the validated proposal."""
    return generate_json(model_ref or route_for("loop_b_rank"),
                         loop_b_prompt(weekly, cfg), LOOP_B_RUN_SCHEMA, system=LOOP_B_SYSTEM)


# --- orchestrator entry points ---------------------------------------------

def run_loop_a_fixes(cfg: dict, site: str, pages: list[dict], apply: bool = False,
                     model_ref: dict | None = None) -> dict:
    """Propose Tier-1 metadata for the in-scope set, verify it, and record the outcome.

    Everything up to the WordPress write is implemented and runs today. `apply` is the second
    gate: with it off (the default) this proposes, reconciles, corrects lengths, queues Tier
    2/3 work and persists our own state — but writes nothing to the live site. That makes a
    full dress rehearsal against production data free of consequences.

    Returns a summary dict; the caller renders it. Raises rather than half-applying if the
    batch fails reconciliation — a proposal that lost a page or duplicated metadata is not
    something to partially trust.
    """
    # `pages` may be the whole in-scope set (nothing else exists to collide with) or a subset
    # (the on-publish variant, or a human applying one chosen page at a time) — in the subset
    # case, whatever is already live on the REST of the site is still off-limits.
    requested_urls = {p["url"] for p in pages}
    others = [p for p in db.list_page_state(site) if p["url"] not in requested_urls]
    taken = {"titles": [p["title"] for p in others if p.get("title")],
             "descs": [p["metadesc"] for p in others if p.get("metadesc")]}

    gen = propose_loop_a(pages, cfg, taken=taken, model_ref=model_ref)
    check = reconcile(gen.data, pages)
    if not check["ok"]:
        raise RuntimeError("Loop A batch failed reconciliation: " + "; ".join(check["errors"]))

    payload, still_off = refine_lengths(gen.data, pages, cfg, model_ref=model_ref, base_taken=taken)
    by_url = {p["url"]: p for p in payload.get("pages", [])}

    # The model was told what's `taken`, but that's a prompt, not a guarantee — same
    # discipline as the in-batch collisions() check, just against the rest of the site.
    taken_by_field = {"yoast_title": set(taken["titles"]), "yoast_metadesc": set(taken["descs"])}

    proposals: list[dict] = []
    for page in pages:
        proposal = by_url.get(page["url"], {})
        changes = proposal.get("changes", [])
        queue = list(proposal.get("manual_queue", []))
        written: dict[str, str] = {}

        off_band_fields = {m["field"] for m in still_off if m["url"] == page["url"]}
        safe_changes = []
        for change in changes:
            if change.get("new") in taken_by_field.get(change["field"], ()):
                queue.append({"tier": 2, "check": "wp_write",
                             "note": f"{change['field']} duplicates text already live on "
                                     f"another page — queued for a human instead of written."})
            elif change["field"] in off_band_fields:
                # Still outside its character band after every retry round — a model that
                # missed the length twice gets no benefit of the doubt on content quality
                # either (this is exactly how a length-retry artifact/corruption would surface).
                queue.append({"tier": 2, "check": "wp_write",
                             "note": f"{change['field']} still off character-band after retries "
                                     f"— queued for a human instead of written: {change['new']!r}"})
            else:
                safe_changes.append(change)
        changes = safe_changes

        db_fields: dict = {}

        if apply and changes:
            # The WP MCP write lands here, and only here. Everything above is safe to run
            # against production because nothing in it leaves the process.
            post_id = page.get("post_id")
            if not post_id:
                queue.append({"tier": 2, "check": "wp_write",
                             "note": "no post_id on record — add this page to "
                                     "scope.in_scope_ids so Loop A can write it."})
            else:
                try:
                    written = wp_mcp.apply_yoast_changes(post_id, changes)
                except wp_mcp.WPMCPError as err:
                    queue.append({"tier": 2, "check": "wp_write",
                                 "note": f"WP MCP write failed, nothing changed on this page: "
                                         f"{err}"})

            if written:
                now = datetime.now(timezone.utc).isoformat()
                changelog = list(page.get("changelog") or [])
                for change in changes:
                    if wp_mcp.YOAST_META_KEY.get(change["field"]) not in written:
                        continue
                    changelog.append({"ts": now, "field": change["field"],
                                      "old": change.get("old"), "new": change["new"],
                                      "by": "loop_a"})
                # Only changelog here — title/metadesc/*_len columns stay whatever the next
                # real audit measures from the live rendered page, not what we assume we wrote.
                db_fields["changelog"] = changelog

        proposals.append({
            "url": page["url"],
            "changes": changes,
            "applied": bool(written) if apply else False,
            "manual_queue": queue,
            "off_band": [m for m in still_off if m["url"] == page["url"]],
        })
        # Our own state is safe to record either way: manual_queue is a to-do list, not a
        # content change. checklist_status stays whatever audit.py measured — it describes
        # the live page, and until we apply, the live page has not moved.
        if queue:
            db_fields["manual_queue"] = queue
        if db_fields:
            db.upsert_page_state(site, page["url"], db_fields)

    return {
        "applied": apply,
        "model": f"{gen.provider}:{gen.model}",
        "latency_s": gen.latency_s,
        "repaired": gen.repaired,
        "proposals": proposals,
        "unresolved_bands": still_off,
    }


def render_proposals(summary: dict, pages: list[dict], cfg: dict) -> str:
    """Human-readable diff of what Loop A would change — the thing to read before --apply."""
    current = {p["url"]: p for p in pages}
    out = [f"Loop A proposal · {summary['model']} · {summary['latency_s']}s"
           f"{' · REPAIRED' if summary['repaired'] else ''}",
           f"{'APPLIED to WordPress' if summary['applied'] else 'PROPOSAL ONLY — nothing written'}",
           ""]
    for p in summary["proposals"]:
        page = current.get(p["url"], {})
        tag = ""
        if summary["applied"] and p["changes"]:
            tag = "  ✓ written" if p["applied"] else "  ✗ NOT written — see queue"
        out.append(p["url"] + tag)
        for change in p["changes"]:
            field = change["field"]
            was = page.get("title") if field == "yoast_title" else page.get("metadesc")
            out.append(f"  {field}")
            out.append(f"    - {was!r} ({len(was or '')} chars)")
            out.append(f"    + {change['new']!r} ({len(change['new'])} chars)")
            if change.get("source_quote"):
                out.append(f"    grounded in: {change['source_quote'][:90]!r}")
        for q in p["manual_queue"]:
            out.append(f"  queued tier{q['tier']}: {q['check']} — {q['note'][:80]}")
        for m in p["off_band"]:
            out.append(f"  ⚠ still off-band: {m['field']} {m['len']} chars "
                       f"(want {m['min']}-{m['max']})")
        out.append("")
    return "\n".join(out)


def run_loop_b_ranking(cfg: dict, site: str, run_date: str, weekly: dict) -> dict:
    """Rank + persist for Loop B. No content write, so this is fully implementable: propose, then
    upsert the opportunities / editorial_gaps onto the existing weekly row."""
    gen = propose_loop_b(weekly, cfg)
    db.upsert_weekly(site, run_date, {
        "opportunities": gen.data["opportunities"],
        "editorial_gaps": gen.data["editorial_gaps"],
        "run_status": "ok",
    })
    return gen.data
