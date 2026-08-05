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

Bake-off interface (the other harness scores; it calls these):
  from scripts.agent import LOOP_A_PAGE_SCHEMA, LOOP_A_SYSTEM, loop_a_prompt
  from scripts.models import generate_json, candidates_for
  prompt = loop_a_prompt(fixture_page, cfg)
  for cand in candidates_for("loop_a_meta"):
      gen = generate_json(cand, prompt, LOOP_A_PAGE_SCHEMA, system=LOOP_A_SYSTEM)
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


def loop_a_prompt(page: dict, cfg: dict) -> str:
    """Assemble the Loop A prompt for one page.

    `page` is a seo_page_state-shaped dict; include a "content_excerpt" key (a paragraph or two
    of the page's visible text) so the model can write a meaningful meta description — audit.py
    fetches the HTML, so the excerpt plumbing is a small add on the Loop A path."""
    th = cfg.get("thresholds", {})
    t = th.get("title_len", {})
    m = th.get("metadesc_len", {})
    return (
        f"Page: {page.get('url')}\n"
        f"Current title ({page.get('title_len')} chars): {page.get('title')!r}\n"
        f"Current meta description ({page.get('metadesc_len')} chars): {page.get('metadesc')!r}\n"
        f"Page content excerpt: {page.get('content_excerpt', '(not provided)')!r}\n\n"
        f"Targets: title {t.get('min', 50)}-{t.get('max', 60)} chars, "
        f"meta description {m.get('min', 150)}-{m.get('max', 160)} chars; both unique and accurate.\n\n"
        "Tasks:\n"
        "1. If the title or meta description is outside its target band, missing, or weak, propose a "
        "Tier-1 replacement (field yoast_title / yoast_metadesc) that fits the band and reflects the "
        "page accurately. Do not touch health-claim substance.\n"
        "2. Anything that needs body edits (alt text, headings, internal links) or a health-claim "
        "rewrite is NOT Tier 1 — put it in manual_queue as tier 2 (structure) or tier 3 (claims).\n"
        "3. Set checklist_status: 'green' if no Tier-1 change is needed, else 'queued'; 'failing' only "
        "if something is broken you cannot address.\n"
        "Return only the JSON object."
    )


def propose_loop_a_page(page: dict, cfg: dict, model_ref: dict | None = None) -> Generation:
    """Run one page through the routed (or given) model and return the validated proposal."""
    return generate_json(model_ref or route_for("loop_a_meta"),
                         loop_a_prompt(page, cfg), LOOP_A_PAGE_SCHEMA, system=LOOP_A_SYSTEM)


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
    """DEFERRED: per page, propose_loop_a_page() -> apply Tier-1 changes via WP MCP -> persist
    checklist_status / manual_queue / changelog via db.upsert_page_state. The model call is ready;
    the WP MCP write (and content-excerpt plumbing) is the remaining build. Not wired yet."""
    raise NotImplementedError("Loop A WP-MCP write path not built — model call is scripts.agent.propose_loop_a_page.")


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
