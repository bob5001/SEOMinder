"""Provider-neutral model layer — the model-agnostic seam for the agent step.

The loops do NOT run an agentic tool loop: the model is asked to return structured JSON,
and the orchestrator (db.py / the WP MCP write path) acts on it. So every task reduces to
one call:

    gen = generate_json(model_ref, prompt, schema, system=...)
    payload = gen.data            # dict, already validated against `schema`

`model_ref` is a {"provider", "model"} dict resolved from config/models.yaml — either the
routed model for a task (`route_for(task)`) or a bake-off candidate (`candidates_for(task)`).
Providers are pluggable:
  - "anthropic"  -> Claude Messages API (structured outputs)
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
import re
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


# --- public API -------------------------------------------------------------

def generate_json(model_ref: dict, prompt: str, schema: dict, *,
                  system: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS) -> Generation:
    """Ask `model_ref` for JSON matching `schema`; validate it (with one repair retry) and
    return a Generation. Raises if the model can't produce schema-valid JSON after the retry.

    Kept deliberately simple: no agentic loop, no tool-calling — just constrained generation,
    which every model (cloud or local) can do."""
    prov = _provider(model_ref["provider"])
    complete = _ADAPTERS.get(prov["kind"])
    if complete is None:
        raise KeyError(f"No adapter for provider kind '{prov['kind']}'")

    started = time.monotonic()
    text, usage = complete(prov, model_ref["model"], system, prompt, schema, max_tokens)
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
        text, usage2 = complete(prov, model_ref["model"], system, repair_prompt, schema, max_tokens)
        usage = {k: usage.get(k, 0) + usage2.get(k, 0) for k in set(usage) | set(usage2)}
        data = _validate(text, schema)  # raises if still invalid — caller/bake-off records the failure

    return Generation(
        data=data, provider=model_ref["provider"], model=model_ref["model"],
        latency_s=round(time.monotonic() - started, 3), usage=usage,
        repaired=repaired, raw_text=text,
    )


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
# Signature: (provider_cfg, model, system, prompt, schema, max_tokens) -> (text, usage)

def _complete_anthropic(prov, model, system, prompt, schema, max_tokens):
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


def _complete_openai(prov, model, system, prompt, schema, max_tokens):
    import openai  # lazy: covers OpenAI *and* local Ollama/vLLM/LM Studio via base_url
    api_key = env(prov["api_key_env"]) if prov.get("api_key_env") else "local-no-key"
    client = openai.OpenAI(base_url=prov.get("base_url"), api_key=api_key or "local-no-key")
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    common = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    try:  # prefer strict json_schema (OpenAI, recent Ollama/vLLM)
        resp = client.chat.completions.create(
            response_format={"type": "json_schema",
                             "json_schema": {"name": "payload", "schema": schema, "strict": True}},
            **common,
        )
    except openai.OpenAIError:  # server doesn't support json_schema -> plain JSON mode + our validator
        resp = client.chat.completions.create(response_format={"type": "json_object"}, **common)
    text = resp.choices[0].message.content or ""
    u = getattr(resp, "usage", None)
    usage = ({"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens} if u else {})
    return text, usage


_ADAPTERS: dict[str, Callable] = {
    "anthropic": _complete_anthropic,
    "openai": _complete_openai,
}
