# Ghost Data Still Exists — deletion-audit agent

HackerRank Orchestrate, October 2026. The challenge brief is in [`problem_statement.md`](./problem_statement.md).

For each of the 518 requests in `dataset/deletion_requests.csv` the agent decides whether deletion was
actually requested, reconstructs every copy of the person's data at the case's `audit_at`, applies
`deletion_rules.csv`, and writes `COMPLETE` / `INCOMPLETE` / `NOT_APPLICABLE` with an ordered,
evidence-cited plan to `output.csv`.

**Design in one line:** the model *describes* (screenshots, PDFs, multilingual request intent);
deterministic code *decides* (identity, time, policy, ordering).

## Quick start

Python 3.10+.

```bash
pip install -r code/requirements.txt
cp .env.example .env            # put your ANTHROPIC_API_KEY in .env (never committed)
python code/main.py             # 518 requests -> ./output.csv  (+ code/evaluation/usage_report.md)
```

Other commands:

```bash
python code/main.py --offline                         # no API calls: replay cached model answers (code/cache/)
python code/main.py --split sample                    # the 32 labelled samples -> code/evaluation/sample_predictions.csv
python code/evaluation/main.py --pred code/evaluation/sample_predictions.csv --gold dataset/sample_output.csv --verbose
python code/evaluation/validate.py                    # contract check of output.csv
python -m pytest code/tests -q                        # 8 custom evaluation cases
python code/main.py --trace trace.jsonl               # per-case reasoning (why each step exists)
```

A full run takes ~1.5 min with fresh model calls (~$2.43, 25 calls) and ~2 s offline.
`--offline` reproduces the submitted `output.csv` byte for byte.

Environment variables: `ANTHROPIC_API_KEY` (required unless `--offline`), `MODEL` (default
`claude-fable-5-1`), `FALLBACK_MODEL` (default `claude-opus-5-5`), `ACTIVITY_LINK=0` (turns off the
batch-order heuristic for anonymized analytics; see the evaluation report).

## Architecture

```
dataset/ ──► evidence.py ── SQL (sqlite), JSON, 7 log formats, handover text, catalog-link events
         └─► multimodal.py ─ 11 PNG + 4 PDF ──► Claude (vision) ──► same records    [cached]
                     │
                     ▼  Observation = (evidence_id, captured_at, observed_at, system, resource, attrs)
identity.py   current + historical emails / phones / hashes per user_id
intent.py     distinct request texts ──► Claude ──► authorizes deletion?             [cached]
accounts.py   which account_service record is this case's (direct link → window+status → fallback)
interpret.py  observation ──► (policy category, state: PRESENT/ABSENT/CLEAN/ACTIVE/STOPPED/RETAINED)
engine.py     per case: linked resources, state at audit_at, which evidence may be cited
policy.py     deletion_rules.csv ──► findings / remediation / verification, ordered per the contract
main.py       ──► output.csv, trace, usage_report.md
```

| Module | Role | Model? |
|---|---|---|
| `audit/evidence.py` | Parse every text evidence format into uniform observations; resolve trace ids so a log line's lookup key is attached to its result line | no |
| `audit/multimodal.py` | Screenshots and PDFs → the same observation shape (records, identifiers, visible state, aliases such as file names) | **yes**, once per file |
| `audit/intent.py` | Does the request authorize deletion? Batches of 40 distinct texts, any language | **yes**, once per distinct text |
| `audit/identity.py` | Every identifier a person ever had, from `user_identities.csv` + `user_audit_log.csv` | no |
| `audit/accounts.py` | Link each case to its account record | no |
| `audit/activity.py` | Link already-anonymized analytics profiles via the worker's batch (heuristic, labelled) | no |
| `audit/interpret.py` | Map ~40 evidence vocabularies (404, rows=0, NOT_FOUND, enabled=false, restore_allowed=0, null PII…) to one state vocabulary | no |
| `audit/engine.py` | Time rules, linking, state at `audit_at`, citations | no |
| `audit/policy.py` | Rules → steps and ordering | no |
| `audit/llm.py` | Claude client: JSON-only answers validated with pydantic, one repair retry, fallback model, disk cache, token accounting | — |

## Key decisions

- **Model describes, code decides.** Output is graded on exact ids and ordering, and the 518 cases share a
  small evidence corpus. A per-case LLM agent would re-read the evidence 518 times (far more tokens),
  vary between runs, and drift on exact identifiers. The model is used where only a model can help: images, PDFs, multilingual intent.
- **Two clocks.** A fact is *known* if it happened by `audit_at` (log lines carry their own timestamps); an
  evidence file is *citable* only if it was captured by `audit_at`.
- **Only post-request observations set state.** A sighting from before the request cannot prove the record
  survived the deletion run (the sample answers never cite pre-request evidence: 0/75). Older sightings
  still link identities.
- **Claims are not proof.** Workflow statuses, worker results and catalog links are claims. State comes
  from records. Catalog links establish ownership; if the linked record displays another *known*
  person it is dropped, otherwise it is kept and the conflict is written to the trace (case_63, Tom's call).
- **Replication drift.** Same-time conflicting readings resolve to the riskier one (a replica holding the
  row beats a primary that lost it).
- **Account linking ladder.** (1) evidence ties the account to the person; (2) the worker deleted it in the
  3 h before `reported_at`, nobody else owns it, and its result matches the workflow claim
  (COMPLETE↔SUCCESS, PARTIALLY_COMPLETE↔PARTIAL, NOT_STARTED↔FAILED), solved jointly so one account = one
  case; (3) a flagged fallback for true ties. Each case's link type is in the trace.

## Safety and failure handling

- Untrusted inputs: request texts and artifacts are data. The model only classifies or extracts into a fixed
  JSON schema; it never chooses steps. Prompt-injection text in a request can at most change one boolean,
  which is audited against the activity log (10 non-deletion requests = 10 intake events).
- Model failures: JSON validated with pydantic; one repair retry with the validation error; then the
  fallback model (`claude-opus-5-5`); refusals and network errors are caught. If both fail, intent defaults
  to "audit the deletion" (the conservative choice) and the artifact is skipped; the run never crashes.
- Every model answer is cached by a hash of (prompt version, model, input), so reruns are free and identical.
- Secrets come from the environment / `.env` only; nothing is logged or committed.
- `validate.py` enforces the output contract (sorting, numbering, allowed values, phase order, citations,
  no evidence after `audit_at`) before packaging.

## Results

Samples (32 labelled cases): status accuracy **1.000**, step precision **0.989**, recall **0.964**,
F1 **0.976**, sequence accuracy **0.781**. Status is never wrong; remaining errors are missing or
heuristically-linked anonymized analytics and two audit records that no participant file contains.
Details, custom cases, failures and improvements: [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md).
Token use and cost: [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md).

## Known limitations

- Already-anonymized analytics profiles carry no identifier; they are linked through the worker's batch
  order (`audit/activity.py`), right 3 of 5 times on samples.
- About 130 test accounts depend on the fallback tie-break.
- No hold-out split: the 32 samples were all used for development.

## How AI tools were used

Built by Tom with Claude (claude.ai agent session, model `claude-opus-5-5`) as the coding agent;
the full conversation is in `log.txt`. Tom chose the architecture (deterministic engine plus AI for
language and vision, reviewer only if the samples showed a need), the order of work, and the time-box
for the account tie-breaker, and approved each checkpoint. Claude wrote the code, ran the experiments
and reported their numbers; rejected ideas are documented in the evaluation report.
At runtime the agent calls `claude-fable-5-1` (fallback `claude-opus-5-5`).
