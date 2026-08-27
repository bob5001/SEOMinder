#!/usr/bin/env bash
# Wrapper invoked by systemd (under flock). Runs the one-shot container and logs.
# Kept as a script (not inline in the unit) so the same entry works on any host.
set -euo pipefail

cd /srv/seo-loops

DATE="$(date +%F)"
LOG="logs/loop-a-on-publish.${DATE}.log"

{
  echo "=== loop-a-on-publish start $(date -Is) ==="
  docker compose run --rm loop-runner python -m scripts.run_loop_a_on_publish
  echo "=== loop-a-on-publish end   $(date -Is) ==="
} >> "$LOG" 2>&1
