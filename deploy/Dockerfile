# Pure API-client runner. No Chromium in V1 (PSI API does Lighthouse server-side).
FROM python:3.12-slim

# Node + Claude Code CLI for the `claude -p` agent-judgment step
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY deploy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ ./scripts/
COPY loops/   ./loops/
COPY config/  ./config/

# reports/, logs/, secrets/ come in as mounted volumes at runtime.
ENTRYPOINT []
