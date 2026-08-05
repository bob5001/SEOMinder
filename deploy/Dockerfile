# Pure-Python API-client runner. No Chromium in V1 (PSI API does Lighthouse server-side).
# No Node / Claude Code CLI: the agent step is model-agnostic structured-JSON generation via
# provider SDKs (anthropic / openai-compatible, incl. local Ollama/vLLM), not `claude -p`.
FROM python:3.12-slim

WORKDIR /app

COPY deploy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ ./scripts/
COPY loops/   ./loops/
COPY config/  ./config/

# reports/, logs/, secrets/ come in as mounted volumes at runtime.
ENTRYPOINT []
