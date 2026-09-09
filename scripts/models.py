"""Provider-neutral model layer — the model-agnostic seam for the agent step.

The loops do NOT run an agentic tool loop: the model is asked to return structured JSON,
and the orchestrator (db.py / the WP MCP write path) acts on it. So every task reduces to
one call:

    gen = generate_json(model_ref, prompt, schema, system=...)
    payload = gen.data            # dict, already validated against `schema`

`model_ref` is a {"provider", "model"} dict resolved from config/models.yaml — either the
routed model for a task (`route_for(task)`) or a bake-off candidate (`candidates_for(task)`).
Providers are pluggable:
  - "claude_cli" -> the Claude Code CLI (`claude -p`). Runs on the Pro/Max SUBSCRIPTION,
                    not the metered API — this is the default for production.
  - "anthropic"  -> Claude Messages API (structured outputs). METERED: costs API dollars.
  - "openai"     -> the OpenAI Chat Completions wire format, which ALSO covers local
                    runtimes: Ollama, vLLM, LM Studio (just a different base_url).
Adding a provider = one adapter function in _ADAPTERS.

Why this is safe to swap models on (including small local ones): the orchestrator validates
the JSON and gates every write, so model quality affects output *quality*, never *safety*.
That is exactly what the bake-off measures.

Bake-off usage (the other harness builds the scoring; this is the interface it calls):
    from scripts.models import generate_json, candidates_for
    for cand in candidates_for("loop_a_meta"):
        gen = generate_json(cand, prompt, schema, system=system)
        score(gen.data, expected)           # your rubric
        record(gen.latency_s, gen.usage, gen.repaired, gen.raw_text)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import jsonschema
import yaml

from .config import REPO_ROOT, env

MODELS_CONFIG = "config/models.yaml"
DEFAULT_MAX_TOKENS = 2048


@dataclass
class Generation:
    """One model call's result — the payload plus everything the bake-off scores on."""
    data: dict            # JSON payload, validated against the schema
    provider: str
    model: str
    latency_s: float
    usage: dict           # best-effort {"input_tokens", "output_tokens"}
    repaired: bool        # a repair round was needed to get valid JSON
    raw_text: str         # the model's raw reply (for scoring / debugging)


# --- config (config/models.yaml) -------------------------------------------

@lru_cache(maxsize=None)
def _cfg() -> dict:
    p = Path(MODELS_CONFIG)
    if not p.is_absolute():
        p = REPO_ROOT / p
    with open(p) as f:
        return yaml.safe_load(f)


def route_for(task: str) -> dict:
    """Production model for a task, e.g. {'provider': 'anthropic', 'model': 'claude-opus-4-8'}."""
    routing = _cfg().get("routing", {})
    if task not in routing:
        raise KeyError(f"No routing for task '{task}' in {MODELS_CONFIG}")
    return routing[task]


def candidates_for(task: str) -> list[dict]:
    """Bake-off candidate models for a task (empty list if none configured)."""
    return _cfg().get("bakeoff", {}).get(task, []) or []


def _provider(name: str) -> dict:
    providers = _cfg().get("providers", {})
    if name not in providers:
        raise KeyError(f"Unknown provider '{name}' in {MODELS_CONFIG}")
    return providers[name]


def sampling_for(model: str) -> dict:
    """Sampling parameters for a model, by longest matching prefix in config `sampling:`.

    Model families are post-trained at different sampling settings, and using one family's
    defaults on another is a common reason a model looks worse than its benchmarks. It bites
    this project specifically: a model tuned for temperature 1.0 driven at 0 tends toward
    degenerate repetition, which would show up in the bake-off as poor *distinctness* — the
    single metric Loop A's one-call design exists to satisfy. Wrong sampling would make the
    right model look wrong.
    """
    table = _cfg().get("sampling", {}) or {}
    best = ""
    for prefix in table:
        if prefix != "default" and model.startswith(prefix) and len(prefix) > len(best):
            best = prefix

    # Family blocks OVERLAY the default rather than replacing it, so settings that should hold
    # everywhere (suppressing thinking, and the assertion that proves it held) are written once
    # instead of copied into every family and forgotten in the next one added.
    merged = dict(table.get("default", {}) or {})
    family = dict(table.get(best, {}) or {}) if best else {}
    extra = {**(merged.get("extra_body") or {}), **(family.pop("extra_body", None) or {})}
    merged.update(family)
    if extra:
        merged["extra_body"] = extra
    return merged


# --- public API -------------------------------------------------------------

def generate_json(model_ref: dict, prompt: str, schema: dict, *,
                  system: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS,
                  sampling: dict | None = None) -> Generation:
    """Ask `model_ref` for JSON matching `schema`; validate it (with one repair retry) and
    return a Generation. Raises if the model can't produce schema-valid JSON after the retry.

    Kept deliberately simple: no agentic loop, no tool-calling — just constrained generation,
    which every model (cloud or local) can do."""
    prov = _provider(model_ref["provider"])
    complete = _ADAPTERS.get(prov["kind"])
    if complete is None:
        raise KeyError(f"No adapter for provider kind '{prov['kind']}'")

    if sampling is None:
        sampling = sampling_for(model_ref["model"])
    sampling = dict(sampling)
    # Our own metadata, not an API parameter — preflight reads it, the wire never sees it.
    sampling.pop("expect_no_reasoning", None)

    started = time.monotonic()
    text, usage = complete(prov, model_ref["model"], system, prompt, schema, max_tokens, sampling)
    repaired = False
    try:
        data = _validate(text, schema)
    except (json.JSONDecodeError, jsonschema.ValidationError) as err:
        repaired = True
        repair_prompt = (
            f"{prompt}\n\nYour previous reply was NOT valid JSON for the required schema "
            f"(error: {err}). Return ONLY a single JSON object matching the schema — no prose, "
            f"no code fences."
        )
        text, usage2 = complete(prov, model_ref["model"], system, repair_prompt, schema,
                                max_tokens, sampling)
        # Numeric fields (token counts) sum across the two calls; non-numeric ones (e.g. the
        # OpenAI-adapter's "finish_reason", which can legitimately be a string or None) don't
        # add — keep the repair call's value, since it's the one that produced `text`. A blind
        # `+` here crashed with "unsupported operand ... NoneType and str" the moment a
        # finish_reason was None on either call, masking the real (and intended-to-surface)
        # invalid-JSON error from `_validate` below with an opaque one instead.
        def _merge(a, b):
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return a + b
            return b if b is not None else a
        usage = {k: _merge(usage.get(k), usage2.get(k)) for k in set(usage) | set(usage2)}
        data = _validate(text, schema)  # raises if still invalid — caller/bake-off records the failure

    return Generation(
        data=data, provider=model_ref["provider"], model=model_ref["model"],
        latency_s=round(time.monotonic() - started, 3), usage=usage,
        repaired=repaired, raw_text=text,
    )


PREFLIGHT_SCHEMA: dict = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "n"],
    "properties": {"ok": {"type": "boolean"}, "n": {"type": "integer"}},
}


def preflight(model_ref: dict) -> tuple[bool, str]:
    """Cheap check that a model+runtime can honour a JSON schema at all.

    Worth its own step because of a known Ollama defect where a schema-constrained request
    with thinking disabled returned HTTP 200 carrying plain prose — no error, no schema. Our
    validate-then-repair path turns that into a loud exception rather than a silent bad write,
    but without a probe the failure looks like the *model* is bad at JSON. This separates
    "this runtime is broken" from "this model is weak", which are different decisions.
    """
    try:
        gen = generate_json(model_ref, 'Return exactly {"ok": true, "n": 7} and nothing else.',
                            PREFLIGHT_SCHEMA, max_tokens=200)
    except Exception as err:
        return False, f"{type(err).__name__}: {err}"[:200]
    if gen.data.get("n") != 7:
        return False, f"schema honoured but content wrong: {gen.data}"

    # Prove the thinking toggle applied, rather than trusting that we sent it. A model that
    # keeps reasoning spends its whole output budget on it and returns empty content with
    # finish_reason=length — which looks like "bad at JSON" and is really a dropped parameter.
    if sampling_for(model_ref["model"]).get("expect_no_reasoning"):
        n = (gen.usage or {}).get("reasoning_chars", 0)
        if n:
            return False, (f"thinking NOT suppressed ({n} chars of reasoning) — the toggle in "
                           f"config/models.yaml is being ignored by this runtime")
    return True, "repaired" if gen.repaired else "ok"


def _validate(text: str, schema: dict) -> dict:
    data = json.loads(_extract_json(text))
    jsonschema.validate(data, schema)
    return data


def _extract_json(text: str) -> str:
    """Pull the JSON object from a reply that may be fenced or wrapped in prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


# --- adapters (one per provider kind) --------------------------------------
# Signature: (provider_cfg, model, system, prompt, schema, max_tokens, sampling) -> (text, usage)
#
# `sampling` is honoured only by the openai adapter. Current Claude models REJECT
# temperature / top_p / top_k with a 400, and the Claude CLI exposes no sampling knobs at
# all, so both Anthropic paths ignore it by design rather than by omission.

def _complete_anthropic(prov, model, system, prompt, schema, max_tokens, sampling=None):
    import anthropic  # lazy: only needed if the anthropic provider is used
    client = anthropic.Anthropic(api_key=env(prov.get("api_key_env") or "ANTHROPIC_API_KEY", required=True))
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        # structured outputs — constrains the reply to the schema; no temperature (rejected on 4.7+)
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return text, usage


def _complete_openai(prov, model, system, prompt, schema, max_tokens, sampling=None):
    import openai  # lazy: covers OpenAI *and* local Ollama/vLLM/LM Studio via base_url
    api_key = env(prov["api_key_env"]) if prov.get("api_key_env") else "local-no-key"
    client = openai.OpenAI(base_url=prov.get("base_url"), api_key=api_key or "local-no-key")
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]

    sampling = dict(sampling or {})
    # top_k isn't an OpenAI parameter, and thinking toggles are per-family template kwargs —
    # both ride through extra_body, which local runtimes read.
    extra_body = dict(sampling.pop("extra_body", {}) or {})
    if "top_k" in sampling:
        extra_body["top_k"] = sampling.pop("top_k")
    common = {"model": model, "messages": messages, "max_tokens": max_tokens, **sampling}
    if extra_body:
        common["extra_body"] = extra_body

    try:  # prefer strict json_schema (OpenAI, recent Ollama/vLLM)
        resp = client.chat.completions.create(
            response_format={"type": "json_schema",
                             "json_schema": {"name": "payload", "schema": schema, "strict": True}},
            **common,
        )
    except openai.OpenAIError:  # server doesn't support json_schema -> plain JSON mode + our validator
        resp = client.chat.completions.create(response_format={"type": "json_object"}, **common)
    msg = resp.choices[0].message
    text = msg.content or ""
    u = getattr(resp, "usage", None)
    usage = ({"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens} if u else {})
    # Surfaced so preflight can PROVE a thinking toggle took effect. Ollama's /v1 layer drops
    # unknown parameters without complaint, so "we set the flag" is not evidence it applied.
    usage["reasoning_chars"] = len(getattr(msg, "reasoning", None) or "")
    usage["finish_reason"] = resp.choices[0].finish_reason
    return text, usage


def _complete_claude_cli(prov, model, system, prompt, schema, max_tokens, sampling=None):
    """Claude Code CLI (`claude -p`) — runs on the Pro/Max SUBSCRIPTION, not the API meter.

    Two things make this the production default:
      * No API dollars. `claude` authenticates with the logged-in subscription.
      * It is just another adapter, so the model-neutral seam is unchanged.

    The cost is per-call overhead: the CLI ships its own large system prompt (~17k cached
    tokens per invocation) and has a multi-second floor latency. Fine for 11 pages once a
    week; the bake-off measures whether a local model makes even that unnecessary.

    CRITICAL: ANTHROPIC_API_KEY is scrubbed from the subprocess environment. scripts.config
    loads .env into this process, and the CLI prefers an API key over the subscription — so
    leaving it set would silently bill the API for every call. That is the whole trap.
    """
    exe = shutil.which("claude")
    if exe is None:
        raise RuntimeError("claude CLI not found on PATH — install it or route to another provider.")

    argv = [exe, "-p", prompt, "--output-format", "json", "--model", model,
            # No tools: this is one-shot structured generation, not an agent loop.
            "--allowedTools", "", "--strict-mcp-config"]
    if system:
        argv += ["--append-system-prompt", system]

    env_ = {k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}

    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=prov.get("timeout_s", 300), env=env_)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:500]}")

    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI reported an error: {str(envelope.get('result'))[:500]}")

    u = envelope.get("usage", {})
    usage = {
        "input_tokens": u.get("input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        # Surfaced so the bake-off can show the CLI's harness overhead honestly.
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
        # Reported by the CLI even on a subscription, where it bills no API dollars.
        "reported_cost_usd": envelope.get("total_cost_usd", 0.0),
    }
    return envelope.get("result", ""), usage


_ADAPTERS: dict[str, Callable] = {
    "claude_cli": _complete_claude_cli,
    "anthropic": _complete_anthropic,
    "openai": _complete_openai,
}
