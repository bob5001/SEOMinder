"""Sole state-writer layer over Neon Postgres.

Every read/write of the seo_* tables goes through here. This is the single-writer
discipline from INFRA.md: the loop agent never touches Postgres directly — it emits
structured JSON and an orchestrator calls these helpers. `query()` additionally backs
the agent's read-only query tool.

Column identifiers below are internal constants (never user input), so they are safe to
interpolate into SQL; all *values* are passed as bound parameters.
"""
from __future__ import annotations

import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import env

# jsonb columns per table — values for these get wrapped so dicts/lists serialize correctly.
JSONB_COLUMNS: dict[str, set[str]] = {
    "seo_page_state": {"broken_links", "cwv_lab", "manual_queue", "changelog"},
    "seo_weekly": {"per_url", "opportunities", "editorial_gaps", "cwv_field"},
}


def _dsn() -> str:
    return env("DATABASE_URL", required=True)


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(_dsn(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Read-only convenience helper (also the surface behind the agent's read tool)."""
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def _wrap_jsonb(table: str, fields: dict[str, Any]) -> dict[str, Any]:
    cols = JSONB_COLUMNS.get(table, set())
    return {k: (Jsonb(v) if k in cols and v is not None else v) for k, v in fields.items()}


# --- sites -----------------------------------------------------------------

def get_site(site: str) -> dict | None:
    rows = query("SELECT * FROM sites WHERE site = %s", (site,))
    return rows[0] if rows else None


# --- run log (the anti-rot record) ----------------------------------------

def start_run(site: str, loop: str, host: str | None = None) -> str:
    host = host or socket.gethostname()
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO seo_run_log (site, loop, status, host) "
            "VALUES (%s, %s, 'running', %s) RETURNING run_id",
            (site, loop, host),
        ).fetchone()
    return str(row["run_id"])


def end_run(run_id: str, status: str, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE seo_run_log SET ended_at = now(), status = %s, error = %s "
            "WHERE run_id = %s",
            (status, error, run_id),
        )


# --- seo_page_state (Loop A) -----------------------------------------------

def _upsert(table: str, conflict_cols: list[str], key: dict[str, Any], fields: dict[str, Any]) -> None:
    fields = _wrap_jsonb(table, {k: v for k, v in fields.items() if k not in key})
    cols = list(key.keys()) + list(fields.keys())
    vals = list(key.values()) + list(fields.values())
    placeholders = ", ".join(["%s"] * len(cols))
    non_key = list(fields.keys())
    do_update = (
        f"DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in non_key)}"
        if non_key
        else "DO NOTHING"
    )
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict_cols)}) {do_update}"
    )
    with connect() as conn:
        conn.execute(sql, vals)


def upsert_page_state(site: str, url: str, fields: dict[str, Any]) -> None:
    fields = dict(fields)
    fields.setdefault("last_audited_at", datetime.now(timezone.utc))
    _upsert("seo_page_state", ["site", "url"], {"site": site, "url": url}, fields)


def get_page_state(site: str, url: str) -> dict | None:
    rows = query("SELECT * FROM seo_page_state WHERE site = %s AND url = %s", (site, url))
    return rows[0] if rows else None


def list_page_state(site: str) -> list[dict]:
    return query("SELECT * FROM seo_page_state WHERE site = %s ORDER BY url", (site,))


# --- seo_weekly (Loop B) ---------------------------------------------------

def upsert_weekly(site: str, run_date: str, fields: dict[str, Any]) -> None:
    _upsert("seo_weekly", ["site", "run_date"], {"site": site, "run_date": run_date}, fields)


def get_latest_weekly(site: str) -> dict | None:
    rows = query(
        "SELECT * FROM seo_weekly WHERE site = %s ORDER BY run_date DESC LIMIT 1", (site,)
    )
    return rows[0] if rows else None
