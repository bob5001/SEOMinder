# SEOMinder-scoped Ollama tags

SEOMinder talks to Ollama through its OpenAI-compatible endpoint (`/v1/chat/completions` —
see `providers.ollama` in `config/models.yaml`). That endpoint silently drops a per-request
`num_ctx` on this host's Ollama build (0.32.7 as of 2026-08-24, verified empirically — it
accepts the field, top-level or nested under `options`, and just ignores it). Only Ollama's
*native* `/api/chat` endpoint honours `options.num_ctx` per request, and this project doesn't
use that endpoint.

So context length has to be pinned a different way: baked into the model via a Modelfile,
which Ollama does apply regardless of which endpoint a request comes through. Each Modelfile
here is a `FROM` of an existing local pull plus one `PARAMETER num_ctx` line, tagged `-seo` so
it's a distinct, independent copy — SEOMinder's calls are pinned to a fixed KV-cache footprint
no matter what context default anyone sets globally on this host for something else (e.g. a
big `OLLAMA_CONTEXT_LENGTH` for a different tool sharing this Ollama instance).

**16384 tokens** covers SEOMinder's worst case today with real headroom: Loop A's largest
batch (an 11-page in-scope set) runs ~3.5k input tokens (11 x ~700-char excerpts + prompt
scaffolding) against an output budget of 400 tokens/page (~4.4k tokens) — call it ~8k tokens
total. Loop B is smaller still. Raise it only if the in-scope page set grows enough to need it
— reflect any change here in the Modelfiles' comments too.

## Rebuilding the tags

Run this after first pulling the base models, and again any time one of them is re-pulled
(`ollama pull` does not update tags derived from it — the `-seo` copy stays pinned to
whatever the base was when it was created):

```
./deploy/ollama/create-seo-tags.sh
```

`config/models.yaml` routes to the `-seo` tags (`gemma4:31b-seo`, `gemma4:26b-seo`,
`qwen3.6:27b-seo`, `llama3.1:8b-seo`), not the base tags directly — verify with
`ollama show <tag>` that `num_ctx` reads back as expected, or watch `ollama ps` for the
`CONTEXT` column while a call is in flight.
