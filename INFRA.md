# Deliverable 3 — Infra Spec (Signal Sanctuary SEO Loops)

Execution home: **Docker on the Proxmox box.** Host `systemd` timer fires a one-shot container;
the container runs one loop, writes state to Postgres, and exits. Host owns scheduling + locking;
container owns execution. This pattern transplants verbatim to any Docker host later — migration is a
`git clone` + `.env` + one systemd timer.

Everything downstream of "which host runs the timer" is host-agnostic by design. Nothing in the state
schema or the script contracts assumes macOS, Proxmox, or a specific network.

---

## Layers (kept separate on purpose)

- **Definition** — this repo (loop prompts, scripts, schema, compose, timer). Git, public-safe.
- **Execution** — Proxmox Docker host: systemd timer → `docker compose run --rm loop-runner`.
- **State/memory** — Neon Postgres. Single source of truth for site state + change history. (The
  CodeManager *broker* is a **separate** agent-knowledge layer — see below — not the state store.)
- **Presentation** — md reports + Discord webhook. **Projections of Postgres state, never parallel truth.**

The presentation rule is the one discipline that matters: the runner writes structured results to
Postgres, then a *separate render step* reads Postgres and emits the report + Discord post. Reports are
a `SELECT`, not a second database. The day a dashboard is wanted, it reads from the same place the
report does — the same Neon database, filtered by `site`.

**CodeManager broker (distinct from state).** CodeManager is the cross-session agent-knowledge service
(projects + visits). The loops use it as a ledger for *out-of-band* SEO work that isn't a content
change — a redirect needing a server config, a hosting/CWV infra fix, a Tier-3 human task. Content
state lives in Postgres; things that need a human or a different system get logged to the broker.

---

## Secrets

Single gitignored `.env` on the host, injected into the container as env vars. Repo stays public-safe
because all secrets + site config live outside it. Values:

```
ANTHROPIC_API_KEY=
GSC_SERVICE_ACCOUNT_JSON=/run/secrets/gsc_sa.json   # path mounted into container, not committed
GSC_PROPERTY=sc-domain:signalsanctuary.health
PSI_API_KEY=                                         # PageSpeed Insights
DATABASE_URL=postgresql://…neon.tech/neondb?sslmode=require   # Neon Postgres — the state store
CODEMANAGER_URL=http://192.168.1.189:8007           # CodeManager broker (agent-knowledge), optional
WP_MCP_URL=https://signalsanctuary.health/wp-json/mcp/v1/http
WP_MCP_TOKEN=
DISCORD_WEBHOOK_URL=
```

The GSC service-account JSON is mounted as a file (read-only) rather than pasted inline. Keep it out
of the image and out of git.

---

## SEO state schema (Neon Postgres)

This is the **union of the two loop record blocks** — the loops already defined it; infra just
implements it. Four tables in Neon Postgres (project `SEOMinder`, db `neondb`). Every per-site table
carries a `site` column so multiple assets live in one store and the dashboard is `… GROUP BY site`;
a `sites` registry holds one row per asset. **Status: built** — tables + the `signalsanctuary` seed
row exist.

### `sites` — one row per asset (tenant registry)
```
site         text  pk        # short slug, e.g. 'signalsanctuary'
name         text            # 'Signal Sanctuary'
domain       text
base_url     text
gsc_property text            # 'sc-domain:signalsanctuary.health'
config_path  text            # 'config/signalsanctuary.yaml'
active       bool
created_at   timestamptz
```
Adding client #2 = a new `sites` row + a new `config/<client>.yaml`, not a fork.

### `seo_page_state` — one row per (site, URL), upserted by Loop A
```
site                text  pk        # -> sites.site
url                 text  pk        # composite PK (site, url)
post_id             int
page_type           text            # informational | landing | blog_index | blog_post
title               text
title_len           int
metadesc            text
metadesc_len        int
h1_count            int
heading_order_ok    bool
alt_coverage_pct    float
internal_links_out  int
internal_links_in   int
lighthouse_seo_pass bool
lighthouse_a11y_pass bool
indexable           bool
schema_type         text
schema_valid        bool
broken_links        json            # [{url,status}]
cwv_lab             json            # {lcp,cls,inp} (median of 3)
checklist_status    text            # green | queued | failing
manual_queue        json            # [{tier, check, note}]  tier2/tier3 awaiting human
last_audited_at     timestamptz
changelog           json            # [{ts, field, old, new, by}]
```

### `seo_weekly` — one row per (site, Loop B run), append-only
```
site                text  pk        # -> sites.site
run_date            date  pk        # composite PK (site, run_date)
gsc_window          text            # e.g. 2026-07-14..2026-07-20 vs prior 7d
per_url             json            # [{url,position,ctr,impressions,clicks,delta_position,indexed}]
opportunities       json            # [{type,url_or_query,metric,delta,priority,handed_to_loop_a}]
editorial_gaps      json            # [{query,impressions,suggested_page}]
cwv_field           json            # [{url,bucket,trend}]
run_status          text            # ok | partial | failed
digest_sent         bool
```

### `seo_run_log` — start/end record per invocation (the anti-rot record)
```
run_id      uuid pk        # defaults to gen_random_uuid()
site        text          # -> sites.site
loop        text          # loop_a | loop_b
started_at  timestamptz
ended_at    timestamptz
status      text          # running | ok | failed
host        text
error       text
```
Written at **start** (status=running) and **end** (ok/failed). A skipped or dead run is then visible
as a gap or a stuck `running` row — not a silent absence.

---

## Script contracts

### `scripts/gsc_pull.py`  (Loop B input)
- **In:** `GSC_SERVICE_ACCOUNT_JSON`, `GSC_PROPERTY`, window (default last 7d vs prior 7d)
- **Does:** Search Console Search Analytics query by page+query; indexing status per URL; CrUX/field
  CWV buckets.
- **Out:** writes `per_url` + `cwv_field` into the current `seo_weekly` row in Postgres.
- **Notes:** service-account auth (no OAuth dance); read-only scope
  `webmasters.readonly`. Pure API client — no browser.

### `scripts/audit.py`  (Loop A technical checks)
- **In:** target URL list (from `config`), `PSI_API_KEY`
- **Does:** (1) PageSpeed Insights API call per URL → Lighthouse SEO + Accessibility + lab CWV scores;
  (2) fetch rendered HTML + parse (BeautifulSoup) → H1 count, heading order, empty-alt detection,
  internal-link graph, schema presence; (3) schema validation.
- **Out:** upserts `seo_page_state` fields (everything except the Yoast-meta writes, which the loop
  agent does via MCP).
- **Notes:** **No local Chromium in V1** — PSI runs Lighthouse on Google's side. Elementor renders
  server-side, so fetched HTML already contains content; no JS execution needed. 25k/day PSI limit is
  effectively unlimited here. Add a Playwright sidecar later only if a check needs a real browser.

### `scripts/render_report.py`  (presentation — projection of Postgres)
- **In:** Postgres state (`seo_page_state`, latest `seo_weekly`) for a given `site`
- **Does:** reads Postgres → renders a structured md report into `reports/YYYY-MM-DD.md` → posts a
  summary to Discord via `DISCORD_WEBHOOK_URL`.
- **Out:** committed md file + Discord message. Writes nothing back to Postgres.
- **Notes:** this is the ONLY thing that talks to Discord. Webhook, not a bot (one-way notify). A bot
  is a V2 concern (two-way approve/trigger).

### `scripts/run_loop_a.py` / `scripts/run_loop_b.py`  (entrypoints)
- Thin orchestrators. Write `seo_run_log` start → run audit/gsc scripts → invoke `claude -p` with the
  loop md for the agent-judgment + MCP-write portion → write `seo_run_log` end → for Loop B, call
  `render_report.py`.

---

## Execution flow (weekly Loop B)

```
host systemd timer (Mon 06:00)
  └─ flock (no overlap)
       └─ docker compose run --rm loop-runner python -m scripts.run_loop_b
            ├─ seo_run_log: start
            ├─ gsc_pull.py           → Postgres seo_weekly.per_url, cwv_field
            ├─ claude -p loop-b-weekly.md
            │     ├─ diff vs last week, rank opportunities
            │     ├─ write opportunities/editorial_gaps → Postgres
            │     └─ hand top-N targets to Loop A queue
            ├─ render_report.py      → reports/DATE.md + Discord
            └─ seo_run_log: end (ok|failed)
```

Loop A runs the same way but is manual/`/goal` or on-publish, and includes `audit.py` +
the MCP Yoast-meta writes.

---

## Config seam (multi-tenant without building multi-tenancy)

Loop prompts + scripts stay generic. All site-specific values live in `config/<client>.yaml`.
Client #2 is a new yaml + a compose service, not a fork. See `config/signalsanctuary.yaml`.

---

## What is deliberately NOT in V1

- No dashboard (reports/ dir + committed md is the dashboard-lite; revisit on sub-weekly cadence,
  multi-site view, or client self-serve).
- No Chromium/local Lighthouse (PSI API covers it; add Playwright sidecar only on demand).
- No scheduler daemon inside the container (host owns scheduling).
- No Discord bot (webhook only; bot is V2 for two-way).
- No OpenClaw (plain systemd + flock; revisit at many-loops/many-sites or when retries + channel
  delivery + event triggers become first-class needs — and it lives in Docker too, so no re-platform).

---

## Migration to an outside server (why this shape was chosen)

Everything host-specific is two files: `deploy/seo-loop-b.service` + `.timer`, and the `.env`. On a
new Docker host: `git clone`, drop `.env`, `docker compose build`, install the systemd unit, `enable
--now` the timer. Linux→Linux, systemd→systemd. The container artifact is identical.
