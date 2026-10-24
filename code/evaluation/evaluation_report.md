# Evaluation report — Ghost Data Still Exists

All numbers are reproducible:

```bash
python code/main.py --split sample --offline          # -> code/evaluation/sample_predictions.csv
python code/evaluation/main.py --pred code/evaluation/sample_predictions.csv --gold dataset/sample_output.csv --verbose
python -m pytest code/tests -q                       # custom evaluation cases
python code/evaluation/loo_timing.py                 # out-of-sample check of account linking (no labels)
```

## 1. Metrics on the 32 labelled sample cases

| Metric | Definition | Score |
|---|---|---:|
| **Case-level status accuracy** | `deletion_status` exact match | **1.000** (32/32) |
| **Step precision** | predicted steps that exactly match a gold step (all fields except `step_no`) | **1.000** (190/190) |
| **Step recall** | gold steps reproduced exactly | **0.984** (190/193) |
| **Step F1** | | **0.992** |
| Step F1 ignoring `evidence_id` | isolates citation errors | 0.992 |
| **Sequence accuracy** | whole `steps_json` array exactly right, including order and numbering | **0.906** (29/32) |

**Field-level accuracy** (steps aligned by position; a missing step makes every later position in that
case count as wrong, so this is the strictest view):

| phase | system_id | resource_id | operation | state | evidence_id |
|---:|---:|---:|---:|---:|---:|
| 0.984 | 0.959 | 0.959 | 0.959 | 0.959 | 0.979 |

**Remaining errors** (3 missing gold steps, 0 wrong or extra steps; status is never wrong). None is
recoverable from participant-facing data:

| Gold step not reproduced | Count | Cause |
|---|---:|---|
| `security_service` `VERIFY_RECORD_PRESENT` | 2 | `audit_record_6645` / `audit_record_5569` appear in no participant-facing file (searched all text, logs, SQL, JSON and the extracted screenshots/PDFs). |
| `account_service` `VERIFY_RECORD_ABSENT` | 1 | case_04: hand-written case whose account id appears nowhere in the data (no record, no worker event). |

### Progression during the build (samples)

| Version | Status acc. | Step F1 | Sequence acc. | Change |
|---|---:|---:|---:|---|
| v1 baseline | 0.938 | 0.915 | 0.406 | parser + identity + policy engine, text evidence only |
| v2 | 1.000 | 0.956 | 0.625 | screenshots/PDFs via Claude vision; "known vs citable" time rule; hashes in lookup keys |
| v3 | 1.000 | 0.973 | 0.719 | newest-request-first batch order; SQL scope inference; activity linking switched off |
| v4 | 1.000 | 0.973 | 0.719 | robustness for test-only patterns (catalog links, legal holds, stale evidence) — samples unchanged |
| v5 | 1.000 | 0.976 | 0.781 | anonymized analytics linked only for otherwise-COMPLETE cases, in batch order |
| v6 (final) | 1.000 | **0.992** | **0.906** | worker timing signature (learned from direct links) for account ties and anonymized analytics |

### Out-of-sample validation of account linking (no labels)

The samples are only 32 cases, so the account linker was also validated on the 518 *unlabelled* test
requests: for every request whose account is directly evidenced, the link is hidden, the timing model
is rebuilt **without that person's links**, and the account is recovered from the deletion window
(`python code/evaluation/loo_timing.py`):

| Outcome | Requests |
|---|---:|
| Status match alone picks the account — correct | 139 |
| Timing signature breaks the tie — correct | 68 |
| **Wrong (either way)** | **0** |
| Still tied (fallback used) | 6 |
| Person has no other worker actions to compare (fallback used) | 25 |
| Account outside the 3 h window | 7 |

On the real run this cut the fallback tie-break from 131 to 30 test accounts, and 108 of 120
anonymized-analytics links are now decided by the timing grid (12 by batch order).

## 2. Custom evaluation cases (created by us)

`code/tests/test_engine.py` builds a tiny synthetic dataset for each trap described in the challenge and
asserts the engine's **exact** output. No model calls (intent is injected), so they run in milliseconds.
**8/8 pass.**

| # | Case | What it proves | Expected result |
|---|---|---|---|
| 1 | **Replica drift** | The primary lost the profile but a read replica still holds it after the deletion. Workflow says COMPLETE. | `INCOMPLETE`: `REPORT_REMAINING_RECORD account_1` citing the replica snapshot → `DELETE_RECORD` → `VERIFY_RECORD_ABSENT`; retained audit record verified present. |
| 2 | **Historical email** | Request comes from an email the user changed away from (only in `user_audit_log.csv`); a search entry still uses it. | The old address resolves to the user; the search entry is reported as remaining. |
| 3 | **Stale sighting** | A search entry was seen only *before* the request. | Not reported: an older sighting cannot prove the record survived the deletion run → `COMPLETE`. |
| 4 | **Evidence after `audit_at`** | A snapshot captured after the audit cutoff shows data present. | Ignored and never cited → `COMPLETE`. |
| 5 | **Look-alike decoy** | `test.person.jr@` vs `test.person@`. | Decoy not linked. |
| 6 | **Non-deletion request** | "Send me a copy of my data, keep my account." | `NOT_APPLICABLE`, `[]`. |
| 7 | **Ordering** | Search, analytics, support, data flow, backup and audit record together. | Findings by operation then system; flows disabled and restores blocked before records are changed; full 17-step sequence asserted. |
| 8 | **Citations on COMPLETE** | Clean tombstone, disabled flow, retained audit record. | Every verification step cites evidence (a fallback citation path was added after this test failed — see §3). |

### Sanity checks on the 518 unlabelled requests

- **Intent:** the model marked exactly **10** requests as non-deletion; the activity stream independently
  contains exactly **10** `privacy_intake … classified request as non-deletion` events.
- **Contract:** 518 rows, sorted, valid JSON, consecutive step numbers, only allowed operations/states,
  no finding without evidence, no `COMPLETE` verification without evidence, no evidence captured after
  `audit_at` — `python code/evaluation/validate.py`: 518 rows, 0 problems.
- **Reproducibility:** a run from an *empty* cache (fresh model calls) reproduced the previous
  `output.csv` exactly (518/518 rows, step F1 1.000 against it); `--offline` replay is byte-identical.

## 3. Failures found and improvements made

| # | Failure / idea | Evidence | Decision |
|---|---|---|---|
| 1 | Anonymized analytics profiles have no identifiers. First attempt: hand the worker's unowned policy actions in each batch to every case lacking that system, in batch order. | 3/9 right; F1 0.973 → 0.966, precision 1.000 → 0.969. | Rejected. Second look at the samples: every missed profile belonged to an otherwise-COMPLETE case (a failed anonymization would leave identifiers and be linked directly). Restricting to those cases and to ANONYMIZE actions: 3 right, 2 wrong ids, F1 0.973 → **0.976**, sequence 0.719 → **0.781**. Adopted; a wrong id only lands on a case that was already missing the step. `ACTIVITY_LINK=0` disables it. |
| 2 | Account resource ids often appear only in the activity stream (no identifier). | Time window alone gives 2–6 candidates. Found that the worker's result matches the workflow claim (COMPLETE↔SUCCESS, PARTIALLY_COMPLETE↔PARTIAL, NOT_STARTED↔FAILED) on every generated sample. | Ladder: direct evidence → window + status match, solved jointly so one account = one case → flagged fallback (newest request takes earliest event; matched 4/4 sample batches). |
| 3 | Batch order: tried ordering by each case's account-deletion time. | Swapped case_27/28. Newest-request-first matched all 4 sample batches. | Newest-request-first. |
| 4 | "Missing from a later snapshot ⇒ deleted". | F1 0.973 → 0.931, status 1.000 → 0.969, broke 8 cases including failover-replica drift. Snapshots are partial selections, not full listings. | **Rejected and removed** (no dead code). |
| 5 | Re-running the vision extraction relabelled a support PDF from attachment to message. | F1 0.973 → 0.950 (case_07). | Policy category of a support-held PDF is now decided by code, not by the model's label. |
| 6 | A February sighting of a file leaked into a September audit. | case_07 regression; gold cites pre-request evidence 0/75 times. | Only observations after `requested_at` set a resource's state; older ones still link identities. |
| 7 | Log lines inside files captured after `audit_at` (case_09's audit record). | Gold still uses those resources. | Two clocks: a fact is *known* if it happened by `audit_at`; a file is *citable* only if captured by `audit_at`. |
| 8 | Data-catalog link events (test-only puzzle cases) tie screenshots/PDFs to people via aliases (file names, statement numbers). | Without them, ~12 test cases had no linked evidence. | Parsed as identity-only links; the vision step returns aliases. case_63: the linked profile displays a person who is in no identity record. Tom's decision: keep the link (equal cost either way, and dropping it leaves the case with no contract-valid answer, unlike every other puzzle case); the link is dropped only if the record shows another *known* person. |
| 9 | Hashes inside `correlation_key`, uppercase emails, legal holds named `legal_service`. | Missed links on case_12; wrong `system_id` on 7 test cases. | Normalized. |
| 10 | Custom case 8 failed: `COMPLETE` account check with no citable evidence. | Contract requires evidence on every COMPLETE verification. | Fallback citation: account-scoped snapshot → case's main evidence → the worker's deletion activity id. |
| 11 | Reviewing the 7 imperfect sample sequences (Tom's request): 4 were anonymized-analytics assignments; the other 3 are not recoverable from the data (§1). Found that the worker schedules each request's actions on a fixed grid relative to its account deletion: (Δt mod 60 s) is type-specific (e.g. export deletions 51–52 s, search deletions 21–22 s, restore blocks 9–10 s). | Learned from directly evidenced links only; leave-one-out on test accounts 68 correct / 0 wrong. A first coarse version (mod 30 for every type) broke case_23; per-type sets fixed it. | Adopted for account ties (`timing.py`) and anonymized analytics (coarse grid, since no directly owned *successful* anonymization exists to learn from — the one sample-informed choice). Samples: F1 0.976 → **0.992**, sequence 0.781 → **0.906**, precision 1.000. |

## 4. Limitations and honest caveats

- The 32 samples were used for development; there was no separate hold-out (too few cases). To limit
  over-fitting, every rule is stated as a general principle (time, identity, policy) and checked against
  test-set sanity signals rather than tuned per case.
- 30 test accounts still rely on the flagged fallback tie-break (no other worker actions to compare); those account steps may be wrong.
- Already-anonymized analytics profiles carry no identifiers: 108 test links come from the timing grid, 12 from batch order. The timing signature is a property of how this dataset's worker logs were produced; on real systems I would ask for the worker's batch log instead of inferring it.
- case_63 depends on a judgement call (catalog link vs. contradicting screenshot), decided by Tom.
