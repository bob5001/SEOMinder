# seo-loops

AI-driven SEO automation for client sites. V1 target: **signalsanctuary.health**.
Target deploy is the Proxmox Docker box (host systemd fires a one-shot container), portable to
any Docker host — but that migration hasn't happened yet. **Both loops currently run live on a
Mac Studio M1** via macOS `launchd` LaunchAgents (`deploy/local/`), a parallel scheduling path
kept in sync with the Linux units; see `deploy/local/README.md` for why and how.

## Layout
```
loops/      loop-a-onpage.md, loop-b-weekly.md   (the /goal + weekly agent prompts)
scripts/    gsc_pull.py, audit.py, agent.py, wp_mcp.py, render_report.py,
            run_loop_a.py, run_loop_a_on_publish.py, run_loop_b.py
config/     <site>.yaml  (per-client seam — domain, scope IDs, thresholds, webhook)
reports/    committed weekly md history (dashboard-lite)
deploy/     Dockerfile, requirements.txt, compose is at repo root; systemd unit+timer, wrapper —
            the Linux/Proxmox target, not yet installed anywhere
deploy/local/  macOS launchd LaunchAgents — the path actually running today, on the Mac Studio
logs/       run logs (gitignored)
INFRA.md    full infra spec: layers, state schema, script contracts, migration
```

## Architecture in one breath
Definition (git) · Execution (target: host systemd -> one-shot container; today: macOS launchd
-> venv python, see `deploy/local/README.md`) · State (Neon Postgres) · Presentation (md reports
+ Discord webhook, a projection of Postgres — never parallel truth).
The CodeManager broker is a separate agent-knowledge layer (out-of-band task ledger), not the state store.

- **Loop A** = verifiable on-page checklist -> all-green -> stop. Manual `/goal` or on-publish.
  Tier 1 (Yoast meta) auto-fixes now; Tier 2 (Elementor body) detect+queue; Tier 3 (health claims) human-only.
- **Loop B** = weekly measurement + prioritization pass. Pulls GSC, ranks opportunities, queues targets
  for Loop A, emits editorial gaps, posts a Discord digest. Not a convergence loop.

## Status (Sep 2026) — you are here
Deterministic system + agent step are both built and **validated live** against
signalsanctuary.health, writes included: Neon state store, all scripts, both orchestrators, md
reports, a working Discord digest, `scripts/agent.py` (`WIRED=True`), and `scripts/wp_mcp.py` —
the JSON-RPC client that does the actual Yoast writes (plain WP REST can't; Yoast's fields
aren't `show_in_rest`). Routing moved off `claude -p` for Loop A's meta task — `config/
models.yaml` routes `loop_a_meta` to a local Ollama model (`gemma4:31b-seo`) after the
subscription CLI path reproduced a real schema-validation failure live (`loop_b_rank` still
routes to `claude-sonnet-5`, confirmed current as of 2026-09-10); see the file's own comments
for why. Both pages AND posts are in scope (`scope.in_scope_ids` / `in_scope_post_ids`).
`scripts/run_loop_a_on_publish.py` polls for newly-published content and auto-applies Loop A
per item — see its docstring; it polls hourly (lowered from every 10 min on 2026-09-12, once
the preflight was reordered to run after the cheap "anything new?" check instead of before it).

Both loops are **live and scheduled today** via macOS `launchd` on the Mac Studio
(`deploy/local/`), not yet the Proxmox/systemd target. Agent-availability preflights are now
split per task (`loop_a_meta` / `loop_b_rank` checked independently) and post a Discord alert
only when a route's status actually *changes* — this caught a real bug where Loop B's ranking
step was silently skipped for an entire scheduled run. Manual backlink tracking was added
2026-09-11. A 2026-09-11 live-fire diagnosis found one page
(`is-my-router-making-me-sick`) actively de-indexed by Google as a near-duplicate of a
templated content cluster — a content-authoring fix, not yet done; see `INFRA.md`/CodeManager
for the other sibling pages sharing that risk.

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
6. ~~Wire Loop A manually first (dry-run: detect only, no writes) to validate the checklist against Postgres.~~ **DONE.**
7. ~~Turn on Tier 1 auto-fix for Loop A.~~ **DONE** — live on both pages and posts, `scripts/wp_mcp.py`.
8. ~~Schedule Loop B.~~ **DONE, on the interim macOS path** — `deploy/local/com.seominder.loop-b.plist`
   (launchd, weekly Monday 06:00) is installed and running on the Mac Studio. The Linux systemd
   timer (`deploy/seo-loop-b.{service,timer}`) is still just files, not yet installed anywhere —
   open until the Proxmox migration.
9. ~~Schedule Loop A's on-publish trigger.~~ **DONE, on the interim macOS path** —
   `deploy/local/com.seominder.loop-a-on-publish.plist` (launchd, hourly, auto-applies) is
   installed and running on the Mac Studio. The Linux systemd timer
   (`deploy/seo-loop-a-on-publish.{service,timer}`) is still just files, not yet installed —
   open until the Proxmox migration.

## The agent step (built — the contract it actually honours)
The loops' judgment + content-write half is `scripts/agent.py` (`WIRED=True`), model-agnostic —
`config/models.yaml` routes each task to a provider (currently a local Ollama model for
`loop_a_meta`, `claude -p` on the subscription for `loop_b_rank`); `scripts.models` is the seam,
so swapping either is a config change, not a code change. The **contract**:
- **The agent never writes to Postgres.** It returns structured JSON; the orchestrator persists via
  `db.py` — `db.py` is the single writer. Exact JSON shapes are in `scripts/agent.py`'s docstring.
- **The agent's only live writes are content writes via `scripts/wp_mcp.py`** (Yoast meta on
  Loop A), Tier-gated: Tier 1 auto-fix; Tier 2/3 detect + queue. Two more gates sit in front of
  every write: a sitewide title/description collision check, and a still-out-of-band-after-
  retries check — both queue for a human instead of writing rather than trusting the model's
  output at face value. (WP backs up daily / 7 days — rollback net.)
- **Out-of-band, non-content tasks** (redirects, hosting/CWV, Tier-3) → logged to the CodeManager
  broker, not Postgres.

No `/goal` slash command was built — Loop A is invoked directly (`run_loop_a.py`, or the
on-publish poller above), which turned out sufficient in practice.

## Setup
This is the **Linux/Proxmox target path** (Docker + systemd) — not what's actually scheduled
today. For the live macOS setup on the Mac Studio, see `deploy/local/README.md` instead; it
runs `.venv/bin/python3` directly via `launchd`, no container.
```
cp .env.example .env          # fill secrets (gitignored) — incl. DATABASE_URL from the Neon console
mkdir -p secrets && cp /path/to/gsc_sa.json secrets/gsc_sa.json
# edit config/signalsanctuary.yaml as needed
docker compose build
# dry run:
docker compose run --rm loop-runner python -m scripts.run_loop_b
# schedule Loop B (weekly):
sudo cp deploy/seo-loop-b.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now seo-loop-b.timer
# schedule Loop A on-publish (hourly, auto-applies to newly published content):
sudo cp deploy/seo-loop-a-on-publish.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now seo-loop-a-on-publish.timer
```
`docker-compose.yml` runs `loop-runner` with `network_mode: host` — required for the container
to reach Ollama on `localhost:11434` (the host's, not its own). Linux-only, which is why the Mac
Studio uses the `deploy/local/` launchd path instead of Docker Desktop in the meantime.

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
- **The `claude` CLI isn't on the minimal PATH a scheduler provides.** `shutil.which("claude")`
  (used by `scripts/models.py` to find it) fails under `launchd`/`cron`/`systemd` unless the
  unit explicitly sets `PATH` to include wherever it actually lives (`~/.local/bin` on the Mac
  Studio) — both `deploy/local/*.plist` set this; check it again on whatever Linux host
  eventually runs `deploy/*.service`, since the `claude` binary's location there is unverified.
- **A "wired" code path can be dead and still look fine.** Loop B's ranking step was a bare
  attribute reference (`agent.run_loop_b_ranking`, never called) for the project's entire
  history before 2026-08-28 — it went undetected because the agent-availability preflight was
  *also* always failing, so the harmless-looking "agent unavailable, skipping" branch fired
  first every time and nobody had reason to suspect the branch behind it was unreachable code.
  Don't assume a step ran just because its skip-path logged something plausible.

## Deliberately NOT in V1
No dashboard, no local Chromium, no in-container scheduler, no Discord bot, no OpenClaw.
Each has a named trigger condition for revisiting — see INFRA.md.
