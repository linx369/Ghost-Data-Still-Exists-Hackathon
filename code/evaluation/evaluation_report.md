# Evaluation report — Ghost Data Still Exists

All numbers are reproducible:

```bash
python code/main.py --split sample --offline          # -> code/evaluation/sample_predictions.csv
python code/evaluation/main.py --pred code/evaluation/sample_predictions.csv --gold dataset/sample_output.csv --verbose
python -m pytest code/tests -q                       # custom evaluation cases
```

## 1. Metrics on the 32 labelled sample cases

| Metric | Definition | Score |
|---|---|---:|
| **Case-level status accuracy** | `deletion_status` exact match | **1.000** (32/32) |
| **Step precision** | predicted steps that exactly match a gold step (all fields except `step_no`) | **1.000** (183/183) |
| **Step recall** | gold steps reproduced exactly | **0.948** (183/193) |
| **Step F1** | | **0.973** |
| Step F1 ignoring `evidence_id` | isolates citation errors | 0.973 |
| **Sequence accuracy** | whole `steps_json` array exactly right, including order and numbering | **0.719** (23/32) |

**Field-level accuracy** (steps aligned by position; a missing step makes every later position in that
case count as wrong, so this is the strictest view):

| phase | system_id | resource_id | operation | state | evidence_id |
|---:|---:|---:|---:|---:|---:|
| 0.948 | 0.896 | 0.896 | 0.896 | 0.896 | 0.948 |

**Every remaining error is a missing step; there are no wrong or invented steps** (precision 1.000):

| Missed gold step | Count | Cause |
|---|---:|---|
| `analytics_platform` `VERIFY_RECORD_ANONYMIZED` | 7 | The analytics profile is already anonymized: no identifier links it to the person in any evidence. The only link is the deletion worker's activity timing, which is ambiguous when several requests share a batch (see §3, experiment 1). |
| `security_service` `VERIFY_RECORD_PRESENT` | 2 | `audit_record_6645` / `audit_record_5569` do not appear in any participant-facing file (searched all text, logs, SQL, JSON, and the extracted screenshots/PDFs). Not recoverable. |
| `account_service` `VERIFY_RECORD_ABSENT` | 1 | case_04: hand-written case whose account id appears nowhere in the data. |

### Progression during the build (samples)

| Version | Status acc. | Step F1 | Sequence acc. | Change |
|---|---:|---:|---:|---|
| v1 baseline | 0.938 | 0.915 | 0.406 | parser + identity + policy engine, text evidence only |
| v2 | 1.000 | 0.956 | 0.625 | screenshots/PDFs via Claude vision; "known vs citable" time rule; hashes in lookup keys |
| v3 | 1.000 | 0.973 | 0.719 | newest-request-first batch order; SQL scope inference; activity linking switched off |
| v4 (final) | 1.000 | 0.973 | 0.719 | robustness for test-only patterns (catalog links, legal holds, stale evidence) — samples unchanged |

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
| 1 | Anonymized analytics profiles have no identifiers. Tried linking them through the deletion worker's batch (policy actions in the 3 h before `reported_at`, handed out in batch order). | Only 3 of 9 assignments correct on samples; F1 0.973 → 0.966, precision 1.000 → 0.969. | **Rejected**, kept behind `ACTIVITY_LINK=1` for discussion; documented as the main known gap. |
| 2 | Account resource ids often appear only in the activity stream (no identifier). | Time window alone gives 2–6 candidates. Found that the worker's result matches the workflow claim (COMPLETE↔SUCCESS, PARTIALLY_COMPLETE↔PARTIAL, NOT_STARTED↔FAILED) on every generated sample. | Ladder: direct evidence → window + status match, solved jointly so one account = one case → flagged fallback (newest request takes earliest event; matched 4/4 sample batches). |
| 3 | Batch order: tried ordering by each case's account-deletion time. | Swapped case_27/28. Newest-request-first matched all 4 sample batches. | Newest-request-first. |
| 4 | "Missing from a later snapshot ⇒ deleted". | F1 0.973 → 0.931, status 1.000 → 0.969, broke 8 cases including failover-replica drift. Snapshots are partial selections, not full listings. | **Rejected and removed** (no dead code). |
| 5 | Re-running the vision extraction relabelled a support PDF from attachment to message. | F1 0.973 → 0.950 (case_07). | Policy category of a support-held PDF is now decided by code, not by the model's label. |
| 6 | A February sighting of a file leaked into a September audit. | case_07 regression; gold cites pre-request evidence 0/75 times. | Only observations after `requested_at` set a resource's state; older ones still link identities. |
| 7 | Log lines inside files captured after `audit_at` (case_09's audit record). | Gold still uses those resources. | Two clocks: a fact is *known* if it happened by `audit_at`; a file is *citable* only if captured by `audit_at`. |
| 8 | Data-catalog link events (test-only puzzle cases) tie screenshots/PDFs to people via aliases (file names, statement numbers). | Without them, ~12 test cases had no linked evidence. | Parsed as identity-only links; the vision step returns aliases. case_63: the linked profile displays a person who is in no identity record. Tom's decision: keep the link (equal cost either way, and dropping it leaves the case with no contract-valid answer, unlike every other puzzle case); the link is dropped only if the record shows another *known* person. |
| 9 | Hashes inside `correlation_key`, uppercase emails, legal holds named `legal_service`. | Missed links on case_12; wrong `system_id` on 7 test cases. | Normalized. |
| 10 | Custom case 8 failed: `COMPLETE` account check with no citable evidence. | Contract requires evidence on every COMPLETE verification. | Fallback citation: account-scoped snapshot → case's main evidence → the worker's deletion activity id. |

## 4. Limitations and honest caveats

- The 32 samples were used for development; there was no separate hold-out (too few cases). To limit
  over-fitting, every rule is stated as a general principle (time, identity, policy) and checked against
  test-set sanity signals rather than tuned per case.
- About 130 test accounts rely on the flagged fallback tie-break; those account steps may be wrong.
- Already-anonymized analytics profiles (no identifiers) are under-reported in `COMPLETE` cases.
- case_63 depends on a judgement call (catalog link vs. contradicting screenshot), decided by Tom.
