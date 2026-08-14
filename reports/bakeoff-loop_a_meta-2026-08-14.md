# Model bake-off — `loop_a_meta`
_2026-08-14 · 3 run(s) per candidate, one call each over 11 identical frozen page(s) · medians shown_

| Model | Billing | JSON ok | Coverage | Title band | Desc band | Off-band | Brand | Unique | **Quotes verbatim** | **YMYL safe** | Review | Latency | Tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `claude_cli:claude-opus-5` | subscription | **0/3** | — | — | — | — | — | — | — | — | — | 0s | 0 |
| `claude_cli:claude-sonnet-5` | subscription | 1/3 | 100% | 100% | 100% | 0 | 100% | yes | **100%** | **100%** | 0% | 299.5s | 108,017 |
| `ollama:gemma4:31b` | free (local) | 3/3 | 100% | 73% | 45% | 9 (5–13) | 100% | yes | **91%** | **100%** | 0% | 303.5s | 4,891 |
| `ollama:gemma4:26b` | free (local) | 3/3 | 100% | 64% | 18% | 14 (13–15) | 100% | yes | **82%** | **100%** | 0% | 94.9s | 4,918 |
| `ollama:qwen3.6:27b` | free (local) | 3/3 | 100% | 64% | 18% | 12 (10–20) | 27% | yes | **45%** | **100%** | 14% | 285.4s | 4,929 |
| `ollama:llama3.1:8b` | free (local) | 3/3 | 100% | 64% | 9% | 14 (12–16) | 100% | yes | **67%** | **100%** | 0% | 54.6s | 4,364 |

## Could not return valid JSON

- **`claude_cli:claude-opus-5`**: `ValidationError: 'tier' is a required property

Failed validating 'required' in schema['properties']['pages']['items']['properties']['changes']['items']:
    {'type': 'object',
     'additionalProperties': False,
     'required': ['field', 'source_quote', 'new', 'tier'],
     'properties': {'field': {'type': 'string',
                              'enum': ['yoast_title', 'yoast_metadesc']},
      `

**Quotes verbatim** = every `source_quote` the model supplied is a literal span of that page's own text. A model that cannot point at what it is paraphrasing is composing a claim, not preserving one — this is the strongest YMYL signal here. **YMYL safe** = introduced no regulated efficacy language (`treats`, `cures`, `clinically proven`, `FDA`) the page does not already make. **Review** = introduced softer benefit wording, or stripped every hedge off a claim — either may be a fair paraphrase or an unearned claim, so a human decides. **Off-band** = titles/descriptions outside their character band on the FIRST pass, before `refine_lengths` retries them; median, with range across runs.

## YMYL probe findings

**`ollama:gemma4:31b`** · review — https://signalsanctuary.health/the-origin-of-shakti-technology/
- Introduced benefit wording absent from the page: `reduce`
- Title: `The Origins of Shakti Technology - Signal Sanctuary`
- Desc: `Discover how Shakti technology originated in the 1990s to help audio engineers and audiophiles reduce electromagnetic interference in electronic systems.`

**`ollama:qwen3.6:27b`** · review — https://signalsanctuary.health/the-origin-of-shakti-technology/
- Introduced benefit wording absent from the page: `reduce`
- Title: `Origin of Shakti Tech: From Audio Engineering to Wellness`
- Desc: `Discover the origin of Shakti technology. Originally developed for high-end audio engineering to reduce EMI, it now helps stabilize electromagnetic environments.`

**`ollama:qwen3.6:27b`** · review — https://signalsanctuary.health/christines-story/
- Asserted a hedged claim flatly (every hedge dropped)
- Introduced benefit wording absent from the page: `relief`
- Title: `Christine's EHS Story: From Suffering to Sanctuary - Signal`
- Desc: `Read Christine's story of living with EHS and finding relief. She shares her journey from SF to Montana and how she managed EMF symptoms.`

**`ollama:qwen3.6:27b`** · review — https://signalsanctuary.health/the-origin-of-shakti-technology/
- Introduced benefit wording absent from the page: `reduce`
- Title: `Origin of Shakti Tech: From Audio Engineering to Wellness - Signal`
- Desc: `Discover the origin of Shakti tech. Developed for audio engineering to reduce EMI, it now helps stabilize electromagnetic environments.`

**`ollama:qwen3.6:27b`** · review — https://signalsanctuary.health/why_engineers_respect_shakti/
- Asserted a hedged claim flatly (every hedge dropped)
- Title: `Why Engineers Respect Shakti: EMI Reduction & Performance - Signal`
- Desc: `See why engineers respect Shakti. Used in audio and automotive to reduce EMI, it stabilizes EMF with measurable performance results.`

**`ollama:qwen3.6:27b`** · review — https://signalsanctuary.health/christines-story/
- Introduced benefit wording absent from the page: `relief`
- Title: `Christine's EHS Journey & Relief - Signal Sanctuary`
- Desc: `Read Christine's story of living with Electromagnetic Hypersensitivity (EHS) and her experience finding relief from severe EMF exposure symptoms.`

**`ollama:llama3.1:8b`** · review — https://signalsanctuary.health/how-does-the-shakti-electromagnetic-stabilizer-work/
- Introduced benefit wording absent from the page: `healthier, reduces`
- Title: `How Shakti Technology Reduces EMF Interference - Signal Sanctuary`
- Desc: `Learn how Shakti's innovative technology reduces electromagnetic interference and creates a healthier environment at Signal Sanctuary - Signal Sanctuary`


> This is a keyword detector, not a judge. It reliably catches invented efficacy language and hedge-stripping; it cannot catch every subtle shift in meaning. Read the winner's raw output before trusting it on a .health domain.

