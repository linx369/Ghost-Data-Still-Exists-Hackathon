# Token usage and cost — final full-dataset run

## What these figures measure (provenance)

- **The run that produced the submitted `output.csv`** (table below) is an offline run of the final code:
  every one of its 25 model calls was answered from the cache in `code/cache/`, so it billed nothing.
  The token and cost figures in the table are what those cached answers originally cost.
- **Live measurement (previous version).** On 2026-10-24, 16:09-16:11 UTC, the previous version (commit
  `ede8387`) was run from an **empty** cache with live API calls: **25 calls, 0 cache hits, no retries, no
  fallback model; 70,241 input + 37,567 output = 107,808 tokens; estimated $2.58** ($0.00498 per request).
  Its `output.csv` was byte-identical to the submission of that time (SHA-256 `b6219ee2…`).
- **What changed since then is deterministic matching code only** (`audit/timing.py`: action types with no
  learned timing grid are now scored on the shared grid). The model prompts, the model-calling code, the
  inputs sent to the model and the cached model answers are all unchanged, so a live run of the final
  code would make the same 25 calls; the AI-generated answers are identical. That change alters 3 rows of
  `output.csv` (case_546, 547, 548; SHA-256 now `084d0894…`) without any model call.
- The model's free-text wording varies slightly between runs (37,567 live vs 35,921 cached output
  tokens); every answer that feeds a decision was the same.
- Model calls made during development (earlier prompt versions, test and sample runs) are not counted.

## Final run

- Report written: 2026-10-24T17:04:51+00:00 by `python code/main.py` --offline (split: `test`, 518 requests).
- Provider: **Anthropic (Claude API)**. Prices used for estimates (USD per million tokens, input / output): `claude-fable-5-1` 10.00 / 50.00, `claude-opus-5-5` 4.00 / 20.00.
- Model calls happen only for (a) reading the 11 screenshots + 4 PDFs once and (b) classifying each
  *distinct* request text once (batched 40 per call). Everything else is deterministic Python, so the
  per-request cost is the amortized cost of those calls. Every answer is cached in `code/cache/`;
  `cache hits` below are calls served from that cache (no tokens billed in this run).

| Model | API calls | cache hits | input tokens | output tokens | total tokens | est. cost (USD) |
|---|---:|---:|---:|---:|---:|---:|
| `claude-fable-5-1` | 0 | 25 | 70,241 | 35,921 | 106,162 | 2.4985 |
| **Overall** | 0 | 25 | 70,241 | 35,921 | 106,162 | 2.4985 |

Token counts include calls served from cache (the tokens those answers originally cost), so the
figures describe the full cost of producing this output, not only what was billed in this run.

| Per request (÷ 518) | value |
|---|---:|
| average total tokens | 204.9 |
| average input tokens | 135.6 |
| average output tokens | 69.3 |
| average model calls | 0.048 |
| estimated cost | $0.00482 |

Why it is cheap: the model *describes* (artifact facts, request intent) once per distinct input;
deterministic code *decides* per case. A naive agent-per-case design would re-read evidence for
every one of the 518 requests.
