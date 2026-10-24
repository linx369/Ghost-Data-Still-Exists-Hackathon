# Token usage and cost — final full-dataset run

- Generated: 2026-10-24T15:44:41+00:00 by `python code/main.py` --offline (split: `test`, 518 requests).
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
