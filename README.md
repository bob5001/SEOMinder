# seo-loops

AI-driven SEO automation for client sites. V1 target: **signalsanctuary.health**.
Runs on the Proxmox Docker box (host systemd fires a one-shot container). Portable to any Docker host.

## Layout
```
loops/      loop-a-onpage.md, loop-b-weekly.md   (the /goal + weekly agent prompts)
scripts/    gsc_pull.py, audit.py, render_report.py, run_loop_a.py, run_loop_b.py
config/     <site>.yaml  (per-client seam — domain, scope IDs, thresholds, webhook)
reports/    committed weekly md history (dashboard-lite)
deploy/     Dockerfile, requirements.txt, compose is at repo root; systemd unit+timer, wrapper
logs/       run logs (gitignored)
INFRA.md    full infra spec: layers, state schema, script contracts, migration
```

## Architecture in one breath
Definition (git) · Execution (host systemd -> one-shot container) · State (Neon Postgres) ·
Presentation (md reports + Discord webhook, a projection of Postgres — never parallel truth).
The CodeManager broker is a separate agent-knowledge layer (out-of-band task ledger), not the state store.

- **Loop A** = verifiable on-page checklist -> all-green -> stop. Manual `/goal` or on-publish.
  Tier 1 (Yoast meta) auto-fixes now; Tier 2 (Elementor body) detect+queue; Tier 3 (health claims) human-only.
- **Loop B** = weekly measurement + prioritization pass. Pulls GSC, ranks opportunities, queues targets
  for Loop A, emits editorial gaps, posts a Discord digest. Not a convergence loop.

## Status (Aug 2026) — you are here
The whole **deterministic** system is built and **validated live** against signalsanctuary.health:
Neon state store, all five scripts, both orchestrators, md reports, and a working Discord digest.
A full baseline is committed at `reports/2026-08-04.md` (11 in-scope pages).

**The one remaining build is the agent step** — `scripts/agent.py` (`WIRED=False`): Loop A's Tier-1
Yoast auto-fixes + the authoritative `checklist_status` + the `/goal` command, then Loop B's
opportunity ranking. Until it's wired, the orchestrators run the deterministic pipeline and skip it.
For the running detail, read the latest CodeManager visit (`get_project_detail` + `get_visit_history`
on the id in `codemanager.md`) — the newest visit is the source of truth for "where we are."

## Gate-vs-signal (the core discipline)
Gate (Loop A, per-page, reproducible now): Yoast title/desc, alt, heading semantics, internal links,
Lighthouse SEO+A11y, indexability, broken links, schema-validates, lab CWV (band, median-of-3).
Signal (Loop B, slow/aggregate): field CWV, position, CTR, indexing confirmation.
Never gate: Yoast composite, Flesch, third-party DA/DR, vendor GEO scores.

## Build order
1. **Infra first** (this deliverable): state schema, the three scripts, config, container, timer.
2. ~~Bring up the Neon Postgres SEO schema (see INFRA.md).~~ **DONE** — project `SEOMinder`, tables + `signalsanctuary` seed row live.
3. ~~`scripts/audit.py` (PSI API + HTML parse) — no Chromium in V1.~~ **DONE** — fully validated live (incl. PSI).
4. ~~`scripts/gsc_pull.py` (service account, webmasters.readonly).~~ **DONE** — validated live (Search Analytics, URL Inspection, CrUX).
5. ~~`scripts/render_report.py` (Postgres -> md -> Discord webhook).~~ **DONE** — md + a live Discord digest verified.
   - `scripts/run_loop_a.py` / `run_loop_b.py` thin orchestrators **DONE** (deterministic pipeline). The `claude -p` agent step is staged behind a seam (`scripts/agent.py`, `WIRED=False`) — **the next task** (see "The agent step" below).
6. Wire Loop A manually first (dry-run: detect only, no writes) to validate the checklist against Postgres.
7. Turn on Tier 1 auto-fix for Loop A.
8. Install the systemd timer for Loop B; verify with `systemctl list-timers`.

## The agent step (the next build)
The loops' judgment + content-write half runs via `claude -p` (`scripts/agent.py`, currently
`WIRED=False`, so the orchestrators run deterministically and skip it). The **agreed contract** — honour it:
- **The agent never writes to Postgres.** It returns structured JSON; the orchestrator persists via
  `db.py` — `db.py` is the single writer. Exact JSON shapes are in `scripts/agent.py`'s docstring.
- **The agent's only live writes are content writes via the WP MCP** (Yoast meta on Loop A),
  Tier-gated: Tier 1 auto-fix; Tier 2/3 detect + queue. (WP backs up daily / 7 days — rollback net.)
- **Reads use a read-only DB query tool** (the hybrid), or a snapshot the orchestrator pre-loads.
- **Out-of-band, non-content tasks** (redirects, hosting/CWV, Tier-3) → logged to the CodeManager
  broker, not Postgres.

Also build here: the **`/goal` custom slash command** (a `.claude/commands/goal.md` or skill) that runs
`run_loop_a` for the in-scope pages. Creds (`ANTHROPIC_API_KEY`, `WP_MCP_TOKEN`) are in `.env`. Validate
the `claude -p` invocation (flags, `--mcp-config`, `--allowedTools`, `--output-format json`, reply
parsing) against the live CLI **before** flipping `WIRED=True`.

## Setup
```
cp .env.example .env          # fill secrets (gitignored) — incl. DATABASE_URL from the Neon console
mkdir -p secrets && cp /path/to/gsc_sa.json secrets/gsc_sa.json
# edit config/signalsanctuary.yaml as needed
docker compose build
# dry run:
docker compose run --rm loop-runner python -m scripts.run_loop_b
# schedule:
sudo cp deploy/seo-loop-b.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now seo-loop-b.timer
```

### Local development (run scripts directly, no container)
```
python3 -m venv .venv && .venv/bin/pip install -r deploy/requirements.txt
# audit one page, print JSON, skip PSI + link-checking, no DB write:
.venv/bin/python -m scripts.audit --url https://signalsanctuary.health/ --no-psi --no-links --dry-run
# full in-scope audit + persist to Neon:
.venv/bin/python -m scripts.audit --site signalsanctuary
# GSC pull (needs secrets/gsc_sa.json):
.venv/bin/python -m scripts.gsc_pull --site signalsanctuary --dry-run
# render report + post the Discord digest:
.venv/bin/python -m scripts.render_report --site signalsanctuary
```
`scripts/config.py` auto-loads `.env`, so no manual export needed. Common flags across scripts:
`--dry-run` (compute + print, no writes), `--no-psi` / `--no-links` (skip the slow calls),
`--site <slug>` or `--url <url>`, `--no-discord`, `--no-agent`. The venv is gitignored.

## Operational notes & gotchas
- **Repo is public** → every secret lives only in the gitignored `.env` (`.env.example` is the template).
- **GSC uses the URL-prefix property** `https://signalsanctuary.health/`, *not* `sc-domain:…` — the
  service account only has access there. `svc.sites().list()` shows what it can see. To move to
  `sc-domain` later: grant the SA on a verified Domain property first, then update `.env` + the yaml +
  the `sites` row.
- **Secrets containing `$ ( ) #` or spaces must be single-quoted** in `.env` (e.g. `WP_MCP_TOKEN='…'`)
  so Docker Compose doesn't interpolate `$`. The `.env` loader strips the surrounding quotes.
- **`GSC_SERVICE_ACCOUNT_JSON=secrets/gsc_sa.json`** is relative to the repo root, which is `/app` in
  the container — one value works in both places (`config.resolve_path`).
- **CrUX returns `no_data`** for this site (below CrUX's real-user traffic threshold) — expected and
  handled gracefully; field CWV is signal-only anyway.
- **`checklist_status` is NULL until the Loop A agent runs** — `audit.py` measures; the agent judges.
- **Neon**: project `SEOMinder` / `blue-firefly-95433128`, db `neondb`; `DATABASE_URL` in `.env`.
- **Pushes are the human's job**: the remote uses the `gh` HTTPS token as `bob5001` (the `github_neos`
  SSH key isn't registered on GitHub).

## Deliberately NOT in V1
No dashboard, no local Chromium, no in-container scheduler, no Discord bot, no OpenClaw.
Each has a named trigger condition for revisiting — see INFRA.md.
