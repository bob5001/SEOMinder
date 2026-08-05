"""Config + env loading.

Site-specific values come from config/<site>.yaml; secrets and endpoints come from
the environment (a gitignored .env locally, or real env vars in the container). Keeping
the two apart is what lets the repo stay public-safe.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Populate os.environ from a local .env if present.

    Minimal parser (no python-dotenv dependency). Real env vars always win, so this is
    a no-op inside the container where .env isn't shipped.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), _unquote(val.strip()))


def _unquote(val: str) -> str:
    """Strip one pair of surrounding matching quotes. Values are otherwise literal — no
    shell/variable expansion — so tokens containing $, (), or # survive intact. Quote such
    values in .env (single quotes preferred) so Docker Compose doesn't interpolate the $."""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    return val


_load_dotenv()


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@lru_cache(maxsize=None)
def load_site_config(path: str | None = None) -> dict:
    path = path or env("SITE_CONFIG", "config/signalsanctuary.yaml")
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    with open(p) as f:
        return yaml.safe_load(f)


def resolve_path(p: str) -> Path:
    """Absolute paths pass through; relative paths resolve against the repo root — which is
    /app in the container — so one .env value (e.g. secrets/gsc_sa.json) works both in local
    dev and in the container."""
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path)


def site_slug(cfg: dict) -> str:
    """Tenant key used across the seo_* tables. Explicit `site.slug` wins; otherwise
    fall back to the first label of the domain (signalsanctuary.health -> signalsanctuary)."""
    site = cfg.get("site", {})
    return site.get("slug") or site["domain"].split(".")[0]
