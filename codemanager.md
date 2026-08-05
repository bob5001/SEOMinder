---
codemanager_id: c347e6fc-9b8f-4a48-9cd8-2efb4bb2b753
name: SEOMinder
---
This file anchors the project to its codemanager record.
Agents: read `codemanager_id` and call `get_project_detail` — no search needed.
Do not delete or move this file.

## Orientation for agents (read me first)
- **Where we are / full history**: call `get_project_detail` **and** `get_visit_history` on the id
  above. The newest visit is the source of truth for current status — read it before starting work,
  and record a visit when you finish.
- **Design** → `INFRA.md` (layers, state schema, script contracts). **Workflow + status + gotchas** →
  `README.md`.
- **State lives in Neon Postgres**, not here. This CodeManager record is the *agent-knowledge* layer —
  and it's also where out-of-band, non-content SEO tasks (redirects, hosting/CWV, Tier-3 human work)
  get logged, per the loops' design.
