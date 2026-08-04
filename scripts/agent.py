"""Agent seam for the loops — the `claude -p` judgment + content-write step.

Wiring is DEFERRED on purpose: it needs ANTHROPIC_API_KEY + WP_MCP_TOKEN and must be
validated against the live `claude -p` CLI (flags, --mcp-config, --allowedTools,
--output-format json, reply parsing). Building it blind would be guesswork. The orchestrators
run their deterministic pipeline today and skip this step with a clear notice until WIRED.

The contract (already agreed) this step must honour:
  * The agent NEVER writes to Postgres. It returns structured JSON; the orchestrator
    validates and persists via db.py — db.py stays the single writer.
  * The agent's only live writes are CONTENT writes through the WP MCP (Yoast meta on
    Loop A), Tier-gated: Tier 1 auto-fix; Tier 2/3 detect + queue for a human.
  * Reads use a READ-ONLY query tool (the hybrid), or a snapshot the orchestrator pre-loads.
  * Out-of-band, non-content tasks (redirects, hosting/CWV, Tier-3) are logged to the
    CodeManager broker, not Postgres.

Expected agent JSON:
  Loop A (per page):
    {"url", "checklist_status": "green|queued|failing",
     "changes":      [{"field","old","new","tier","by"}],
     "manual_queue": [{"tier","check","note"}], "notes"}
  Loop B (per run):
    {"opportunities":  [{"type","url_or_query","metric","delta","priority","handed_to_loop_a"}],
     "editorial_gaps": [{"query","impressions","suggested_page"}]}
"""
from __future__ import annotations

import shutil

from .config import env

# Flip to True once the claude -p invocation + reply validation are implemented and tested.
WIRED = False


def agent_available() -> bool:
    """CLI present and a key set — necessary but not sufficient (see WIRED)."""
    return shutil.which("claude") is not None and bool(env("ANTHROPIC_API_KEY"))


def run_loop_a_fixes(cfg: dict, pages: list[dict]) -> list[dict]:
    """Deferred: per in-scope page, run the Loop A prompt so the agent applies Tier-1 Yoast
    fixes via WP MCP and returns the JSON above; caller persists checklist_status / manual_queue
    / changelog via db.upsert_page_state. Not implemented until WIRED."""
    raise NotImplementedError("Loop A agent step not wired — see module docstring.")


def run_loop_b_ranking(cfg: dict, weekly: dict) -> dict:
    """Deferred: run the Loop B prompt so the agent ranks opportunities + emits editorial gaps
    and returns the JSON above; caller persists via db.upsert_weekly. Not implemented until WIRED."""
    raise NotImplementedError("Loop B agent step not wired — see module docstring.")
