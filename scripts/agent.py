"""Agent step for the loops — model-agnostic, structured-JSON generation (NOT an agentic loop).

The model is handed the state we already gathered and asked to return JSON matching a schema;
the orchestrator (db.py / the WP MCP write path) acts on it. Because we validate the JSON and
gate every write, model quality affects output *quality*, never *safety* — which is why any
model, including local ones, can run these tasks, and why the bake-off is meaningful.

The provider/model for each task comes from config/models.yaml via scripts.models. Nothing
here is bound to a specific provider.

Contract (unchanged):
  * The agent NEVER writes to Postgres. It returns JSON; the orchestrator persists via db.py.
  * The only live CONTENT writes are Yoast meta via WP MCP on Loop A, Tier-gated: Tier 1
    auto-fix; Tier 2/3 detect + queue for a human. WP backs up daily (7 days) — rollback net.
  * Out-of-band, non-content tasks (redirects, hosting/CWV, Tier-3) go to the CodeManager broker.

Two things are still to build before WIRED=True:
  1. Loop A: the WP MCP write that applies the returned Tier-1 changes (and page content excerpt
     plumbing — the meta writer needs the page's topic; see loop_a_prompt).
  2. End-to-end validation of the model calls against at least one cloud + one local model.
Loop B has no content write — `run_loop_b_ranking` below is fully implementable now.

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

from . import db
from .models import Generation, generate_json, route_for

# Flip to True once the WP MCP write (Loop A) is built and the model calls are validated.
WIRED = False

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
    "required": ["url", "checklist_status", "changes", "manual_queue"],
    "properties": {
        "url": {"type": "string"},
        "checklist_status": {"type": "string", "enum": ["green", "queued", "failing"]},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "new", "tier"],
                "properties": {
                    "field": {"type": "string", "enum": ["yoast_title", "yoast_metadesc"]},
                    "old": {"type": ["string", "null"]},
                    "new": {"type": "string"},
                    "tier": {"type": "integer", "enum": [1]},
                    "rationale": {"type": "string"},
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
        "3. EVERY title must be distinct from every other title in your response, and every "
        "description distinct from every other description. Pages on similar topics must be "
        "differentiated by what is actually specific to each one, not by padding.\n"
        "4. Anything that needs body edits (alt text, headings, internal links) or a health-claim "
        "rewrite is NOT Tier 1 — put it in that page's manual_queue as tier 2 (structure) or "
        "tier 3 (claims).\n"
        "5. Set each page's checklist_status: 'green' if no Tier-1 change is needed, else "
        "'queued'; 'failing' only if something is broken you cannot address.\n"
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
    import json
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

def run_loop_a_fixes(cfg: dict, pages: list[dict]) -> list[dict]:
    """DEFERRED: propose_loop_a() over the whole set -> reconcile() -> apply Tier-1 changes via
    WP MCP -> persist checklist_status / manual_queue / changelog via db.upsert_page_state.

    The model call and the reconciliation are built and tested; the WP MCP write is the
    remaining piece, which is why WIRED is still False. The shape it will take:

        gen = propose_loop_a(pages, cfg)
        check = reconcile(gen.data, pages)
        if not check["ok"]:
            raise ...            # never write a batch that failed reconciliation
        for url, proposal in check["by_url"].items():
            ...                  # WP MCP Yoast write, then db.upsert_page_state
    """
    raise NotImplementedError(
        "Loop A WP-MCP write path not built — the model call is scripts.agent.propose_loop_a.")


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
