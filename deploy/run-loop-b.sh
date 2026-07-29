#!/usr/bin/env bash
# Wrapper invoked by systemd (under flock). Runs the one-shot container and logs.
# Kept as a script (not inline in the unit) so the same entry works on any host.
set -euo pipefail

cd /srv/seo-loops

DATE="$(date +%F)"
LOG="logs/loop-b.${DATE}.log"

{
  echo "=== loop-b start $(date -Is) ==="
  docker compose run --rm loop-runner python -m scripts.run_loop_b
  echo "=== loop-b end   $(date -Is) ==="
} >> "$LOG" 2>&1
