# Token usage and cost — full-dataset run

## What this report measures (provenance)

- **This is a separate verification run, not the run that first wrote the submitted file.** It was made on
  2026-10-24 between 16:09 and 16:11 UTC, from an **empty** model-answer cache, so every model answer
  below was a live API call (25 calls, 0 cache hits, no retries, no fallback model needed).
- Its `output.csv` was **byte-for-byte identical** to the submitted `output.csv`
  (SHA-256 `b6219ee2d1d760aefb8c4b3b7a02d63f24280703a7087475dfdcf23694f9178e`), so these figures are the
  real cost of producing the submitted answers from scratch.
- The submitted `output.csv` itself was written by the same code reading answers cached in `code/cache/`
  (collected during development); that run made no billable calls. The model's free-text wording differs
  slightly between runs (output tokens 37,567 here vs 35,921 for the cached answers), but every answer
  that feeds a decision was the same, so the output did not change.
- Model calls made during development (earlier prompt versions, test and sample runs) are **not** counted here.

- Report written: 2026-10-24T16:11:10+00:00 by `python code/main.py` (split: `test`, 518 requests).
- Provider: **Anthropic (Claude API)**. Prices used for estimates (USD per million tokens, input / output): `claude-fable-5-1` 10.00 / 50.00, `claude-opus-5-5` 4.00 / 20.00.
- Model calls happen only for (a) reading the 11 screenshots + 4 PDFs once and (b) classifying each
  *distinct* request text once (batched 40 per call). Everything else is deterministic Python, so the
  per-request cost is the amortized cost of those calls. Every answer is cached in `code/cache/`;
  `cache hits` below are calls served from that cache (no tokens billed in this run).

| Model | API calls | cache hits | input tokens | output tokens | total tokens | est. cost (USD) |
|---|---:|---:|---:|---:|---:|---:|
| `claude-fable-5-1` | 25 | 0 | 70,241 | 37,567 | 107,808 | 2.5808 |
| **Overall** | 25 | 0 | 70,241 | 37,567 | 107,808 | 2.5808 |

Token counts include calls served from cache (the tokens those answers originally cost), so the
figures describe the full cost of producing this output, not only what was billed in this run.

| Per request (÷ 518) | value |
|---|---:|
| average total tokens | 208.1 |
| average input tokens | 135.6 |
| average output tokens | 72.5 |
| average model calls | 0.048 |
| estimated cost | $0.00498 |

Why it is cheap: the model *describes* (artifact facts, request intent) once per distinct input;
deterministic code *decides* per case. A naive agent-per-case design would re-read evidence for
every one of the 518 requests.
