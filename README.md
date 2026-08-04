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

## Gate-vs-signal (the core discipline)
Gate (Loop A, per-page, reproducible now): Yoast title/desc, alt, heading semantics, internal links,
Lighthouse SEO+A11y, indexability, broken links, schema-validates, lab CWV (band, median-of-3).
Signal (Loop B, slow/aggregate): field CWV, position, CTR, indexing confirmation.
Never gate: Yoast composite, Flesch, third-party DA/DR, vendor GEO scores.

## Build order
1. **Infra first** (this deliverable): state schema, the three scripts, config, container, timer.
2. ~~Bring up the Neon Postgres SEO schema (see INFRA.md).~~ **DONE** — project `SEOMinder`, tables + `signalsanctuary` seed row live.
3. ~~`scripts/audit.py` (PSI API + HTML parse) — no Chromium in V1.~~ **DONE** — validated live; PSI path pending `PSI_API_KEY`.
4. ~~`scripts/gsc_pull.py` (service account, webmasters.readonly).~~ **DONE** — logic unit-checked; full run pending `secrets/gsc_sa.json`.
5. ~~`scripts/render_report.py` (Postgres -> md -> Discord webhook).~~ **DONE** — md verified; Discord pending `DISCORD_WEBHOOK_URL`.
   - `scripts/run_loop_a.py` / `run_loop_b.py` thin orchestrators **DONE** (deterministic pipeline). The `claude -p` agent step is staged behind a seam (`scripts/agent.py`, `WIRED=False`) — the next task, needs `ANTHROPIC_API_KEY` + `WP_MCP_TOKEN`.
6. Wire Loop A manually first (dry-run: detect only, no writes) to validate the checklist against Postgres.
7. Turn on Tier 1 auto-fix for Loop A.
8. Install the systemd timer for Loop B; verify with `systemctl list-timers`.

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

## Deliberately NOT in V1
No dashboard, no local Chromium, no in-container scheduler, no Discord bot, no OpenClaw.
Each has a named trigger condition for revisiting — see INFRA.md.
