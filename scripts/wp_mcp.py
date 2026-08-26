"""JSON-RPC 2.0 client for the WordPress MCP endpoint (the site's "SS MCP CRUD" plugin).

Deliberately NOT the official `mcp` SDK — the repo needs one tool call (`wp_update_post_meta`),
not session/handshake machinery, so this is ~60 lines of `requests` per the repo's minimal-deps
ethos (requests/bs4/psycopg/yaml). Auth is a bearer token: `Authorization: Bearer {WP_MCP_TOKEN}`
against `WP_MCP_URL`, both from .env (see .env.example). Plain WP REST cannot do this write —
Yoast's `_yoast_wpseo_*` fields aren't registered `show_in_rest`, confirmed live (GET exposes only
`meta: ["footnotes"]`, OPTIONS shows `meta` isn't writable) — which is why this client exists at
all.

This is the ONLY module that writes to the live WordPress site. Loop A's `--apply` gate
(scripts.agent.run_loop_a_fixes) calls into it and nowhere else.
"""
from __future__ import annotations

import json
from typing import Any

import requests

from .config import env

TIMEOUT_S = 30

# Loop A field name -> the Yoast post-meta key it actually writes.
YOAST_META_KEY = {
    "yoast_title": "_yoast_wpseo_title",
    "yoast_metadesc": "_yoast_wpseo_metadesc",
}


class WPMCPError(RuntimeError):
    """A JSON-RPC error reply, or a transport/HTTP failure calling the WP MCP endpoint."""


def call(method: str, params: dict | None = None) -> Any:
    """Raw JSON-RPC 2.0 call. Returns the `result` field; raises WPMCPError otherwise."""
    url = env("WP_MCP_URL", required=True)
    token = env("WP_MCP_TOKEN", required=True)
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as err:
        raise WPMCPError(f"WP MCP request failed: {err}") from err
    if r.status_code != 200:
        raise WPMCPError(f"WP MCP HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    if "error" in body:
        err = body["error"]
        raise WPMCPError(f"WP MCP error {err.get('code')}: {err.get('message')}")
    return body.get("result")


def call_tool(name: str, arguments: dict | None = None) -> Any:
    """Call one MCP tool by name and unwrap its reply to the actual payload.

    Two envelope shapes seen from this plugin, and a tool can use either: some (mcp_ping)
    carry a top-level `data` field alongside `content`; most reads (wp_get_post_meta,
    wp_get_post, wp_get_post_snapshot) carry ONLY `content`, a list of {"type": "text", "text":
    ...} parts where `text` is itself a JSON-encoded string of the real payload. Verified live:
    trusting `content` at face value (e.g. printing it) or assuming `data` is always present
    both misrepresent a perfectly successful call as empty/wrong — this unwraps both cases.
    """
    result = call("tools/call", {"name": name, "arguments": arguments or {}})
    if not isinstance(result, dict):
        return result
    if "data" in result:
        return result["data"]
    parts = result.get("content")
    if isinstance(parts, list):
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
    return result


def ping() -> dict:
    """Connectivity check — {"time": ..., "name": <site name>}."""
    return call_tool("mcp_ping")


def get_post_meta(post_id: int, key: str | None = None) -> Any:
    args: dict[str, Any] = {"ID": post_id}
    if key is not None:
        args["key"] = key
    return call_tool("wp_get_post_meta", args)


def update_post_meta(post_id: int, meta: dict[str, str]) -> Any:
    """Write one or more post-meta fields in a single call (e.g. Yoast title + metadesc)."""
    return call_tool("wp_update_post_meta", {"ID": post_id, "meta": meta})


def get_yoast_readability_score(post_id: int) -> int | None:
    """Yoast's own readability score (0-100 traffic light), if a human has ever run its
    analysis for this post in the WP editor.

    NOT a live signal: this is computed client-side by Yoast's JS analysis engine and only
    saved when someone opens/saves the post in wp-admin — confirmed live, a page whose
    title/metadesc WE rewrote through this same API kept an empty score afterward. There is
    also no reindex/recalculate tool exposed anywhere in this plugin's tool list. Treat a
    present value as "whatever a human last saw in the editor", never as current, and treat
    an absent one as "nobody has ever opened this post there" rather than "0".
    """
    val = get_post_meta(post_id, key="_yoast_wpseo_content_score")
    if val in (None, ""):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def apply_yoast_changes(post_id: int, changes: list[dict]) -> dict[str, str]:
    """Apply a Loop A `changes` list (field/new pairs) to one post's Yoast meta in one call,
    then verify each field against a fresh read before trusting it.

    `wp_get_post_meta(ID)` with no `key` returns stale/empty data on this plugin — verified
    live, the object cache it reads clearly isn't busted by wp_update_post_meta the way
    wp_update_post busts the post cache. Per-key reads are NOT stale, so that's what verifies
    here; do not swap this back to a no-key fetch-all.

    Returns the meta dict that was actually confirmed written, keyed by the real Yoast field
    name — that's what the caller persists into `changelog` for rollback reference. Raises
    WPMCPError if a written value doesn't read back as written, rather than reporting success
    on a call that merely didn't error.
    """
    meta = {YOAST_META_KEY[c["field"]]: c["new"] for c in changes if c["field"] in YOAST_META_KEY}
    if not meta:
        return meta
    update_post_meta(post_id, meta)
    for key, expected in meta.items():
        actual = get_post_meta(post_id, key=key)
        if actual != expected:
            raise WPMCPError(
                f"post {post_id}: wrote {key}={expected!r} but read back {actual!r}")
    return meta
