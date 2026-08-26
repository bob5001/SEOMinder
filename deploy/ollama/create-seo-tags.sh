#!/usr/bin/env bash
# Rebuilds the SEOMinder-scoped Ollama tags (see README.md). Idempotent — re-run any time a
# base model is re-pulled, or after editing a Modelfile's PARAMETER lines.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

for modelfile in Modelfile.*; do
  base="${modelfile#Modelfile.}"          # e.g. gemma4-31b
  tag="${base/-/:}-seo"                   # e.g. gemma4:31b-seo (first '-' only, matches base tag's ':')
  echo "==> ollama create ${tag} -f ${modelfile}"
  ollama create "${tag}" -f "${modelfile}"
done

echo
echo "Done. Verify with: ollama list | grep -- -seo"
