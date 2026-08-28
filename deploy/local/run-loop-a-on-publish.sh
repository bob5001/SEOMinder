#!/usr/bin/env bash
# macOS-local wrapper, invoked by launchd. See run-loop-b.sh for why this runs the venv
# directly instead of Docker on this host.
set -euo pipefail

REPO="/Users/robertkoch/Projects/SEOMinder"
cd "$REPO"

DATE="$(date +%F)"
LOG="logs/loop-a-on-publish.${DATE}.log"

{
  echo "=== loop-a-on-publish start $(date '+%Y-%m-%dT%H:%M:%S%z') ==="
  "$REPO/.venv/bin/python3" -m scripts.run_loop_a_on_publish
  echo "=== loop-a-on-publish end   $(date '+%Y-%m-%dT%H:%M:%S%z') ==="
} >> "$LOG" 2>&1
