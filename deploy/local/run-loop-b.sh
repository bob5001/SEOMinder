#!/usr/bin/env bash
# macOS-local wrapper, invoked by launchd. Runs the venv directly, NOT Docker — Docker
# Desktop on Mac doesn't support network_mode: host the way Linux does, and Loop A's
# production route needs to reach Ollama on localhost, so containerizing this on the Mac
# would reintroduce exactly the bug fixed in docker-compose.yml for the Linux target.
# The deploy/*.service + *.timer + docker-compose.yml files are kept as-is for that eventual
# Linux/Proxmox migration — this directory is the interim, Mac-native path.
set -euo pipefail

REPO="/Users/robertkoch/Projects/SEOMinder"
cd "$REPO"

DATE="$(date +%F)"
LOG="logs/loop-b.${DATE}.log"

{
  echo "=== loop-b start $(date '+%Y-%m-%dT%H:%M:%S%z') ==="
  "$REPO/.venv/bin/python3" -m scripts.run_loop_b
  echo "=== loop-b end   $(date '+%Y-%m-%dT%H:%M:%S%z') ==="
} >> "$LOG" 2>&1
