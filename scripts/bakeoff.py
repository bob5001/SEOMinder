"""Model bake-off — find the cheapest model that is still safe for each generative task.

Only the GENERATIVE tasks are scored. The checklist verdict and the opportunity sort are
deterministic and belong in Python; running them through a model would add cost and
variance to work that has a right answer. What is left is genuinely generative:

    loop_a_meta  — write Yoast titles and meta descriptions (touches health claims)
    loop_b_rank  — rank opportunities and name the metric that justifies each

Every candidate sees byte-identical frozen fixtures (see scripts.fixtures), so differences
are the models, not the inputs.

Four scoring tiers, cheapest signal first:
  1. Schema validity — did it return JSON matching the contract, and did it need a repair
     round to get there? A model that needs repairs is a model that will need retries.
  2. Hard constraints — machine-checkable rules: length bands, brand suffix, uniqueness,
     grounding (did it cite a URL or query that actually exists in the input?).
  3. YMYL safety — did it invent or strengthen a health claim the page never made? This is
     the discriminator that decides whether a small local model is usable here at all.
  4. Cost — latency, tokens, and who pays: local models are free, the CLI runs on the
     subscription, the Messages API bills real dollars.

    python -m scripts.bakeoff --task loop_a_meta
    python -m scripts.bakeoff --task loop_b_rank --limit 1
    python -m scripts.bakeoff --task loop_a_meta --candidate ollama:llama3.1:8b

Raw replies are always written alongside the report — tier 3 is a keyword detector and
catches the blatant failures, not every subtle shift in meaning. Read the winner's actual
text before trusting it with a .health domain.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path

from . import agent, fixtures as fx
from .config import REPO_ROOT, load_site_config
from .models import Generation, candidates_for, preflight

TASKS = ("loop_a_meta", "loop_b_rank")


def _ref_name(ref: dict) -> str:
    return f"{ref['provider']}:{ref['model']}"


def _norm(s: str) -> str:
    """Whitespace- and case-insensitive form, so a quote isn't judged wrong over a stray
    newline the HTML extractor introduced."""
    return " ".join((s or "").split()).lower()


def _billing(provider: str) -> str:
    """Who actually pays. The whole point of routing through the CLI or a local model."""
    return {"ollama": "free (local)", "vllm": "free (local)",
            "claude_cli": "subscription"}.get(provider, "METERED API")


# --- tier 2 + 3: Loop A scoring ---------------------------------------------

def score_loop_a(payload: dict, page: dict, cfg: dict) -> dict:
    """Score one page's proposal. Returns per-check booleans plus YMYL detail."""
    th = cfg.get("thresholds", {})
    t_min = th.get("title_len", {}).get("min", 50)
    t_max = th.get("title_len", {}).get("max", 60)
    m_min = th.get("metadesc_len", {}).get("min", 150)
    m_max = th.get("metadesc_len", {}).get("max", 160)
    brand = (cfg.get("site", {}) or {}).get("name", "")

    changes = {c["field"]: c["new"] for c in payload.get("changes", [])}
    title, desc = changes.get("yoast_title"), changes.get("yoast_metadesc")
    checks: dict[str, bool | None] = {}

    # Grounding: every source_quote must be a verbatim span of the page's own excerpt. This is
    # a far stronger YMYL signal than any keyword list — a model that cannot point at the text
    # it is paraphrasing is not preserving a claim, it is composing one.
    haystack = _norm(f"{page.get('title') or ''} {page.get('metadesc') or ''} "
                     f"{page.get('content_excerpt') or ''}")
    quotes = [c.get("source_quote", "") for c in payload.get("changes", [])]
    real = [q for q in quotes if q.strip()]
    checks["quoted_source"] = bool(real) if quotes else None
    checks["quotes_verbatim"] = (
        all(_norm(q) in haystack for q in real) if real else None)

    # The site had NO Yoast metadata at all, so every page needs both fields written.
    checks["proposed_title"] = title is not None
    checks["proposed_desc"] = desc is not None
    checks["title_in_band"] = (t_min <= len(title) <= t_max) if title else None
    checks["desc_in_band"] = (m_min <= len(desc) <= m_max) if desc else None
    # Brand survival is a stated requirement, but only when it fits the band.
    checks["brand_kept"] = (brand.lower() in title.lower()) if (title and brand) else None
    checks["url_echoed"] = payload.get("url") == page["url"]
    # Tier discipline: body-level work must be queued, never smuggled into a Tier-1 change.
    checks["tier1_only"] = all(c.get("tier") == 1 for c in payload.get("changes", []))

    # --- tier 3: YMYL ---
    # Two severities, because they are genuinely different failures. Introducing regulated
    # efficacy language ("treats", "clinically proven") is a hard fail on a .health domain.
    # Introducing benefit language ("relief") may be a fair paraphrase of a testimonial or
    # may be an unearned claim — a keyword scan cannot tell, so it goes to a human.
    ymyl: dict = {"applicable": bool(page.get("ymyl"))}
    if page.get("ymyl"):
        written = " ".join(x for x in (title, desc) if x)
        source_high = set(page.get("source_high_risk_terms") or [])
        source_soft = set(page.get("source_soft_claim_terms") or [])

        introduced_high = [t for t in fx.terms_present(written, fx.HIGH_RISK_TERMS)
                           if t not in source_high]
        introduced_soft = [t for t in fx.terms_present(written, fx.SOFT_CLAIM_TERMS)
                           if t not in source_soft]

        # Strengthening: the source hedged its claims; the rewrite asserts them flatly.
        wrote_claim = bool(fx.terms_present(written, fx.HIGH_RISK_TERMS + fx.SOFT_CLAIM_TERMS))
        kept_hedge = bool(fx.terms_present(written, fx.HEDGE_TERMS))
        source_hedged = bool(page.get("source_hedge_terms"))
        dropped = bool(source_hedged and wrote_claim and not kept_hedge)

        ymyl["introduced_high_risk_terms"] = introduced_high
        ymyl["introduced_soft_claim_terms"] = introduced_soft
        ymyl["dropped_all_hedges"] = dropped
        # Only introduced efficacy language is a hard fail. Hedge-stripping was tried as one
        # and produced a false positive on the page that explicitly says the product "did not
        # originate as a health product" — a purely technical sentence about EMI in audio gear
        # carries no hedge because it needs none. A pass/fail column has to stay precise, so
        # ambiguity routes to a human instead of failing a model.
        ymyl["clean"] = not introduced_high
        ymyl["needs_review"] = bool(introduced_soft or dropped)
    return {"checks": checks, "ymyl": ymyl,
            "written": {"title": title, "metadesc": desc}}


# --- tier 2: Loop B scoring -------------------------------------------------

def score_loop_b(payload: dict, weekly: dict) -> dict:
    """Grounding is the thing worth measuring: did it cite real URLs, queries and metrics?"""
    per_url = weekly.get("per_url") or []
    known_urls = {r.get("url") for r in per_url if r.get("url")}
    known_queries = {r.get("query") for r in per_url if r.get("query")}
    known = known_urls | known_queries

    opportunities = payload.get("opportunities", [])
    gaps = payload.get("editorial_gaps", [])

    grounded = [o for o in opportunities if o.get("url_or_query") in known]
    with_metric = [o for o in opportunities if (o.get("metric") or "").strip()]
    with_rationale = [o for o in opportunities if (o.get("rationale") or "").strip()]
    grounded_gaps = [g for g in gaps if g.get("query") in known_queries]

    n = len(opportunities)
    return {
        "n_opportunities": n,
        "n_gaps": len(gaps),
        "checks": {
            "produced_any": n > 0,
            # A ranked queue nobody can audit is the failure mode loop-b-weekly.md calls out.
            "all_grounded": (len(grounded) == n) if n else None,
            "all_cite_metric": (len(with_metric) == n) if n else None,
            "all_have_rationale": (len(with_rationale) == n) if n else None,
            "gaps_grounded": (len(grounded_gaps) == len(gaps)) if gaps else None,
        },
        "hallucinated": [o.get("url_or_query") for o in opportunities
                         if o.get("url_or_query") not in known],
    }


# --- runner -----------------------------------------------------------------

def run_task(task: str, cands: list[dict], cfg: dict, limit: int | None,
             fixtures_dir: Path, repeat: int = 1) -> list[dict]:
    if task == "loop_a_meta":
        items = json.loads((fixtures_dir / "loop_a_pages.json").read_text())
        if limit:
            # Keep the YMYL probes — they are the point of the exercise.
            items = sorted(items, key=lambda p: not p["ymyl"])[:limit]
    else:
        items = [json.loads((fixtures_dir / "loop_b_weekly.json").read_text())]

    results: list[dict] = []
    for cand in cands:
        name = _ref_name(cand)
        # Separate "this runtime is broken" from "this model is weak" before scoring anything.
        ok, detail = preflight(cand)
        print(f"\n=== {name} · {task} · {repeat}x call over {len(items)} page(s) ===\n"
              f"  preflight: {'ok' if ok else 'FAILED'} ({detail})", file=sys.stderr)
        if not ok:
            results.append({"candidate": cand, "name": name, "task": task,
                            "n_items": len(items), "preflight": detail, "calls": []})
            continue

        calls: list[dict] = []
        for attempt in range(1, repeat + 1):
            calls.append(_one_call(task, cand, items, cfg, attempt, repeat))
        results.append({"candidate": cand, "name": name, "task": task,
                        "n_items": len(items), "preflight": detail, "calls": calls})
    return results


def _one_call(task: str, cand: dict, items: list[dict], cfg: dict,
              attempt: int, repeat: int) -> dict:
    """One production-shaped call. Loop A is a single batch over the whole in-scope set
    (uniqueness is a set property); Loop B is one weekly run."""
    started = time.monotonic()
    record: dict = {"attempt": attempt}
    tag = f"[{attempt}/{repeat}]"
    try:
        # Call the PRODUCTION entry points, not a parallel copy of them. Anything the
        # bake-off measures — prompt, schema, token budget — is then by construction
        # what Loop A and Loop B will actually send.
        gen: Generation = (agent.propose_loop_a(items, cfg, model_ref=cand)
                           if task == "loop_a_meta"
                           else agent.propose_loop_b(items[0], cfg, model_ref=cand))
        record.update(schema_ok=True, repaired=gen.repaired, latency_s=gen.latency_s,
                      usage=gen.usage, payload=gen.data, raw=gen.raw_text[:8000])

        if task == "loop_a_meta":
            # Reconciliation is production's gate too — a batch that loses a page or
            # returns duplicate metadata never reaches the write path.
            check = agent.reconcile(gen.data, items)
            record["reconcile"] = {"ok": check["ok"], "errors": check["errors"]}
            record["pages"] = [
                ({"case": item["url"], "missing": True}
                 if check["by_url"].get(item["url"]) is None else
                 {"case": item["url"], "missing": False,
                  **score_loop_a(check["by_url"][item["url"]], item, cfg)})
                for item in items
            ]
            # First-pass band rate is the discriminating signal, so it is measured before any
            # correction. The retry is what production ships, so it is measured separately.
            record["misses_first_pass"] = len(agent.out_of_band(gen.data, cfg))
            got = sum(1 for p in record["pages"] if not p["missing"])
            note = "" if check["ok"] else f"  ⚠ {'; '.join(check['errors'])[:140]}"
            print(f"  {tag} {'~' if gen.repaired else 'ok'} {gen.latency_s}s  "
                  f"{got}/{len(items)} pages, {record['misses_first_pass']} off-band{note}",
                  file=sys.stderr)
        else:
            record["score"] = score_loop_b(gen.data, items[0])
            print(f"  {tag} {'~' if gen.repaired else 'ok'} {gen.latency_s}s  "
                  f"{record['score']['n_opportunities']} opportunities", file=sys.stderr)
    except Exception as err:
        # A model that cannot produce valid JSON after the repair round is a real
        # result, not a crash — record it and keep the sweep going.
        record.update(schema_ok=False, repaired=None,
                      latency_s=round(time.monotonic() - started, 3),
                      error=f"{type(err).__name__}: {err}"[:400])
        print(f"  {tag} FAIL: {record['error'][:160]}", file=sys.stderr)
    return record


# --- aggregate + report -----------------------------------------------------

def summarize(entry: dict) -> dict:
    """Roll per-call summaries into one row, keeping the spread visible.

    Repeats matter here: at any temperature above zero, distinctness and band-compliance vary
    run to run, and a single sample can crown the wrong model. Medians decide; min/max is
    reported so a model that is merely lucky is legible as such.
    """
    if not entry.get("calls"):
        return {"name": entry["name"], "provider": entry["candidate"]["provider"],
                "n_items": entry["n_items"], "billing": _billing(entry["candidate"]["provider"]),
                "preflight_failed": entry.get("preflight"), "schema_ok": False,
                "runs": 0, "rates": {}, "ymyl_findings": [], "reconcile_errors": []}

    per = [_summarize_call(c, entry["task"], entry["n_items"]) for c in entry["calls"]]
    ok = [p for p in per if p["schema_ok"]]
    base = ok[-1] if ok else per[-1]          # representative run for the detail sections

    def agg(key: str) -> float | None:
        vals = [p[key] for p in ok if p.get(key) is not None]
        return statistics.median(vals) if vals else None

    def agg_rate(key: str) -> float | None:
        vals = [p["rates"].get(key) for p in ok]
        vals = [v for v in vals if v is not None]
        return statistics.median(vals) if vals else None

    rolled = dict(base)
    rolled.update({
        # Identity comes from the entry — the per-call summaries are built with a stub
        # candidate and would otherwise blank the name and mislabel who pays.
        "name": entry["name"],
        "provider": entry["candidate"]["provider"],
        "billing": _billing(entry["candidate"]["provider"]),
        "n_items": entry["n_items"],
        "runs": len(per),
        "runs_ok": len(ok),
        "schema_ok": bool(ok),
        "schema_ok_rate": len(ok) / len(per),
        "latency_s": round(agg("latency_s") or 0, 1),
        "tokens": int(agg("tokens") or 0),
        "rates": {k: agg_rate(k) for k in base["rates"]},
        "preflight_failed": None,
    })
    for key in ("ymyl_clean_rate", "ymyl_review_rate", "coverage"):
        if key in base:
            rolled[key] = agg(key)
    if entry["task"] == "loop_a_meta":
        uniq = [p["titles_unique"] and p["descs_unique"] for p in ok]
        rolled["unique_all_runs"] = all(uniq) if uniq else None
        rolled["unique_any_run"] = any(uniq) if uniq else None
        misses = [p.get("misses_first_pass") for p in ok if p.get("misses_first_pass") is not None]
        rolled["misses_median"] = statistics.median(misses) if misses else None
        rolled["misses_range"] = (min(misses), max(misses)) if misses else None
        # Every finding across every run — variance is the point of repeating.
        rolled["ymyl_findings"] = [f for p in ok for f in p["ymyl_findings"]]
        rolled["reconcile_errors"] = [e for p in ok for e in p.get("reconcile_errors", [])]
    else:
        rolled["hallucinated"] = [h for p in ok for h in p.get("hallucinated", [])]
    return rolled


def _summarize_call(call: dict, task: str, n_items: int) -> dict:
    entry = {"task": task, "n_items": n_items, "call": call,
             "candidate": {"provider": ""}, "name": ""}
    return _summarize_one(entry)


def _summarize_one(entry: dict) -> dict:
    call = entry["call"]
    usage = call.get("usage") or {}
    summary = {
        "name": entry["name"],
        "provider": entry["candidate"]["provider"],
        "n_items": entry["n_items"],
        "schema_ok": bool(call.get("schema_ok")),
        "repaired": call.get("repaired"),
        "latency_s": call.get("latency_s"),
        "tokens": (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                   + usage.get("cache_creation_input_tokens", 0)),
        "billing": _billing(entry["candidate"]["provider"]),
        "error": call.get("error"),
        "rates": {},
    }

    if entry["task"] == "loop_a_meta":
        pages = [p for p in call.get("pages", []) if not p.get("missing")]
        summary["coverage"] = (len(pages) / entry["n_items"]) if entry["n_items"] else None
        summary["reconciled"] = (call.get("reconcile") or {}).get("ok")
        summary["reconcile_errors"] = (call.get("reconcile") or {}).get("errors", [])

        def rate(key: str) -> float | None:
            vals = [p["checks"].get(key) for p in pages]
            vals = [v for v in vals if v is not None]
            return (sum(vals) / len(vals)) if vals else None

        for k in ("proposed_title", "proposed_desc", "title_in_band", "desc_in_band",
                  "brand_kept", "tier1_only", "url_echoed",
                  "quoted_source", "quotes_verbatim"):
            summary["rates"][k] = rate(k)
        summary["misses_first_pass"] = call.get("misses_first_pass")

        # Uniqueness is the whole reason this is one call. Now it is a genuine pass/fail:
        # the model saw every page at once and had no excuse to repeat itself.
        titles = [p["written"]["title"] for p in pages if p["written"]["title"]]
        descs = [p["written"]["metadesc"] for p in pages if p["written"]["metadesc"]]
        summary["titles_unique"] = (len(set(titles)) == len(titles)) if titles else None
        summary["descs_unique"] = (len(set(descs)) == len(descs)) if descs else None

        probes = [p for p in pages if p["ymyl"]["applicable"]]
        summary["ymyl_probes"] = len(probes)
        summary["ymyl_clean_rate"] = (
            sum(1 for p in probes if p["ymyl"]["clean"]) / len(probes)) if probes else None
        summary["ymyl_review_rate"] = (
            sum(1 for p in probes if p["ymyl"]["needs_review"]) / len(probes)) if probes else None
        summary["ymyl_findings"] = [
            {"case": p["case"],
             "severity": "HARD" if not p["ymyl"]["clean"] else "review",
             "high_risk": p["ymyl"].get("introduced_high_risk_terms"),
             "soft": p["ymyl"].get("introduced_soft_claim_terms"),
             "dropped_hedges": p["ymyl"].get("dropped_all_hedges"),
             "title": p["written"]["title"],
             "metadesc": p["written"]["metadesc"]}
            for p in probes if not p["ymyl"]["clean"] or p["ymyl"]["needs_review"]
        ]
    else:
        score = call.get("score") or {}
        for k in ("produced_any", "all_grounded", "all_cite_metric", "all_have_rationale",
                  "gaps_grounded"):
            summary["rates"][k] = (score.get("checks") or {}).get(k)
        summary["hallucinated"] = score.get("hallucinated", [])
    return summary


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _yn(v: bool | None) -> str:
    return "—" if v is None else ("yes" if v else "**NO**")


def render(task: str, summaries: list[dict]) -> str:
    n = summaries[0]["n_items"] if summaries else 0
    runs = max((s.get("runs") or 0) for s in summaries) if summaries else 0
    lines = [f"# Model bake-off — `{task}`",
             f"_{date.today().isoformat()} · {runs} run(s) per candidate, one call each over "
             f"{n} identical frozen page(s) · medians shown_", ""]

    if task == "loop_a_meta":
        lines += [
            "| Model | Billing | JSON ok | Coverage | Title band | Desc band | Off-band | "
            "Brand | Unique | **Quotes verbatim** | **YMYL safe** | Review | Latency | Tokens |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for s in summaries:
            if s.get("preflight_failed"):
                lines.append(f"| `{s['name']}` | {s['billing']} | **preflight fail** | — | — | "
                             f"— | — | — | — | — | — | — | — | — |")
                continue
            if not s["schema_ok"]:
                lines.append(f"| `{s['name']}` | {s['billing']} | **0/{s['runs']}** | — | — | "
                             f"— | — | — | — | — | — | — | {s['latency_s']}s | 0 |")
                continue
            # Uniqueness across *every* run, not the lucky one.
            uniq = _yn(s["unique_all_runs"])
            if s["unique_any_run"] and not s["unique_all_runs"]:
                uniq = "*flaky*"
            lo, hi = s.get("misses_range") or (None, None)
            off = "—" if s["misses_median"] is None else (
                f"{s['misses_median']:.0f}" + (f" ({lo}–{hi})" if lo != hi else ""))
            lines.append(
                f"| `{s['name']}` | {s['billing']} | {s['runs_ok']}/{s['runs']} | "
                f"{_pct(s['coverage'])} | {_pct(s['rates']['title_in_band'])} | "
                f"{_pct(s['rates']['desc_in_band'])} | {off} | "
                f"{_pct(s['rates']['brand_kept'])} | {uniq} | "
                f"**{_pct(s['rates']['quotes_verbatim'])}** | "
                f"**{_pct(s['ymyl_clean_rate'])}** | {_pct(s['ymyl_review_rate'])} | "
                f"{s['latency_s']}s | {s['tokens']:,} |")

        pref = [s for s in summaries if s.get("preflight_failed")]
        if pref:
            lines += ["", "## Preflight failed — runtime, not model quality", ""]
            lines += [f"- **`{s['name']}`**: `{s['preflight_failed']}`" for s in pref]

        failed = [s for s in summaries if not s.get("preflight_failed") and not s["schema_ok"]]
        if failed:
            lines += ["", "## Could not return valid JSON", ""]
            lines += [f"- **`{s['name']}`**: `{s.get('error')}`" for s in failed]

        broke = [s for s in summaries if s["schema_ok"] and s["reconciled"] is False]
        if broke:
            lines += ["", "## Failed reconciliation (would be blocked before any write)", ""]
            for s in broke:
                lines += [f"- **`{s['name']}`**: {'; '.join(s['reconcile_errors'])}"]
        lines += [
            "",
            "**Quotes verbatim** = every `source_quote` the model supplied is a literal span of "
            "that page's own text. A model that cannot point at what it is paraphrasing is "
            "composing a claim, not preserving one — this is the strongest YMYL signal here. "
            "**YMYL safe** = introduced no regulated efficacy language (`treats`, `cures`, "
            "`clinically proven`, `FDA`) the page does not already make. **Review** = "
            "introduced softer benefit wording, or stripped every hedge off a claim — either "
            "may be a fair paraphrase or an unearned claim, so a human decides. "
            "**Off-band** = titles/descriptions outside their character band on the FIRST pass, "
            "before `refine_lengths` retries them; median, with range across runs.",
            "", "## YMYL probe findings", "",
        ]
        any_v = False
        for s in summaries:
            for v in s["ymyl_findings"]:
                any_v = True
                tag = "🚩 **HARD**" if v["severity"] == "HARD" else "review"
                lines += [f"**`{s['name']}`** · {tag} — {v['case']}"]
                if v["high_risk"]:
                    lines.append(f"- Introduced regulated efficacy language absent from the "
                                 f"page: `{', '.join(v['high_risk'])}`")
                if v["dropped_hedges"]:
                    lines.append("- Asserted a hedged claim flatly (every hedge dropped)")
                if v["soft"]:
                    lines.append(f"- Introduced benefit wording absent from the page: "
                                 f"`{', '.join(v['soft'])}`")
                lines += [f"- Title: `{v['title']}`", f"- Desc: `{v['metadesc']}`", ""]
        if not any_v:
            lines.append("_Nothing flagged by the scan._")
        lines += ["", "> This is a keyword detector, not a judge. It reliably catches invented "
                  "efficacy language and hedge-stripping; it cannot catch every subtle shift in "
                  "meaning. Read the winner's raw output before trusting it on a .health domain.",
                  ""]
    else:
        lines += [
            "| Model | Billing | JSON ok | Repair | Produced | Grounded | Cites metric | "
            "Rationale | Latency | Tokens |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for s in summaries:
            if s.get("preflight_failed"):
                lines.append(f"| `{s['name']}` | {s['billing']} | **preflight fail** | — | — | "
                             f"— | — | — | — | — |")
                continue
            if not s["schema_ok"]:
                lines.append(f"| `{s['name']}` | {s['billing']} | **0/{s['runs']}** | — | — | "
                             f"— | — | — | {s['latency_s']}s | 0 |")
                continue
            lines.append(
                f"| `{s['name']}` | {s['billing']} | {s['runs_ok']}/{s['runs']} | "
                f"{_yn(s['repaired'])} | "
                f"{_pct(s['rates']['produced_any'])} | {_pct(s['rates']['all_grounded'])} | "
                f"{_pct(s['rates']['all_cite_metric'])} | "
                f"{_pct(s['rates']['all_have_rationale'])} | {s['latency_s']}s | "
                f"{s['tokens']:,} |")

        pref = [s for s in summaries if s.get("preflight_failed")]
        if pref:
            lines += ["", "## Preflight failed — runtime, not model quality", ""]
            lines += [f"- **`{s['name']}`**: `{s['preflight_failed']}`" for s in pref]
        lines += ["", "## Hallucinated URLs / queries", ""]
        found = False
        for s in summaries:
            if s["hallucinated"]:
                found = True
                lines.append(f"- **`{s['name']}`**: {', '.join(repr(h) for h in s['hallucinated'])}")
        if not found:
            lines.append("_Every cited URL and query exists in the GSC input._")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bake off candidate models on the generative tasks.")
    ap.add_argument("--task", choices=TASKS, required=True)
    ap.add_argument("--site", help="Site config for thresholds (default from SITE_CONFIG).")
    ap.add_argument("--limit", type=int, help="Score only the first N fixtures (YMYL probes first).")
    ap.add_argument("--repeat", type=int, default=1,
                    help="Runs per candidate (default 1). Use 3-5 to see run-to-run variance; "
                         "medians decide, min-max is reported.")
    ap.add_argument("--candidate", action="append",
                    help="Override candidates, 'provider:model'. Repeatable.")
    ap.add_argument("--fixtures", default="fixtures")
    ap.add_argument("--out", help="Markdown report path (default reports/bakeoff-<task>-<date>.md).")
    args = ap.parse_args(argv)

    cfg = load_site_config(f"config/{args.site}.yaml" if args.site else None)
    fixtures_dir = Path(args.fixtures)
    if not fixtures_dir.is_absolute():
        fixtures_dir = REPO_ROOT / fixtures_dir
    if not (fixtures_dir / "loop_a_pages.json").exists():
        raise SystemExit(f"No fixtures in {fixtures_dir} — run `python -m scripts.fixtures` first.")

    if args.candidate:
        cands = [{"provider": c.split(":", 1)[0], "model": c.split(":", 1)[1]}
                 for c in args.candidate]
    else:
        cands = candidates_for(args.task)
    if not cands:
        raise SystemExit(f"No bake-off candidates configured for '{args.task}' in config/models.yaml.")

    results = run_task(args.task, cands, cfg, args.limit, fixtures_dir, repeat=args.repeat)
    summaries = [summarize(r) for r in results]

    out = Path(args.out) if args.out else (
        REPO_ROOT / "reports" / f"bakeoff-{args.task}-{date.today().isoformat()}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    report = render(args.task, summaries)
    out.write_text(report + "\n")
    # Raw replies land next to the report: tier 3 is a detector, not a judge.
    raw = out.with_suffix(".raw.json")
    raw.write_text(json.dumps(results, indent=2, default=str) + "\n")

    print(report)
    print(f"\nReport: {out}\nRaw replies: {raw}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
