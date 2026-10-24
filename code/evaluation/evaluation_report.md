# Evaluation report — Ghost Data Still Exists

Every number in §1 is measured and written by one command:

```bash
python code/verify.py           # tests, sample scores, leave-one-out, reproduction of output.csv, contract
python code/verify.py --fresh   # additionally re-runs from an EMPTY model cache (API calls) and compares
```

The pieces it runs can also be called alone: `python -m pytest code/tests -q`,
`python code/main.py --split sample --offline` + `python code/evaluation/main.py --pred … --gold …`,
`python code/evaluation/loo_timing.py`, `python code/evaluation/validate.py`.

## 1. Measured results

<!-- VERIFY:START -->
_Measured by `python code/verify.py` on 2026-10-24 15:45 UTC. Everything between these markers is generated; do not edit by hand._

**Checks**

| Check | Result | Detail |
|---|---|---|
| Unit + stress tests | PASS | 22 passed in 26.55s |
| Sample status accuracy is 1.000 | PASS | 1.000 |
| Sample step F1 at least 0.99 (regression guard) | PASS | 0.992 |
| Leave-one-out: no wrong account link | PASS | 0 wrong |
| Pipeline reproduces the submitted output.csv byte for byte | PASS | identical |
| output.csv satisfies the contract | PASS | 518 rows, 0 problems |
| In-pipeline verification (contract + grounding) of every answer | PASS | 0 problems |

**Sample scores (32 labelled cases)**

| Metric | Score |
|---|---:|
| Case-level status accuracy | **1.000** (32/32) |
| Step precision | **1.000** (190/190) |
| Step recall | **0.984** (190/193) |
| Step F1 | **0.992** |
| Step F1 ignoring `evidence_id` | 0.992 |
| Sequence accuracy (whole `steps_json` exactly right) | **0.906** (29/32) |

Field-level accuracy (steps aligned by position):

| phase | system_id | resource_id | operation | state | evidence_id |
|---:|---:|---:|---:|---:|---:|
| 0.984 | 0.959 | 0.959 | 0.959 | 0.959 | 0.979 |

**Leave-one-out validation of account linking on the unlabelled test requests** (each proven
account link is hidden and re-derived; the timing model is rebuilt without that person):

| Outcome | Requests |
|---|---:|
| account outside window | 7 |
| no other worker actions (tie stays) | 7 |
| signature: correct | 21 |
| signature: tie | 2 |
| status match alone: correct | 208 |

**Full run on the 518 requests**

| Status | Requests |
|---|---:|
| COMPLETE | 156 |
| INCOMPLETE | 352 |
| NOT_APPLICABLE | 10 |

| Account link | Requests |  | Anonymized-analytics link | Requests |
|---|---:|---|---|---:|
| `direct` | 245 |  | `timing-grid` | 112 |
| `direct-multi` | 1 |  | `unresolved-tie` | 6 |
| `none` | 3 |  |  |  |
| `same-user-other-case` | 1 |  |  |  |
| `unresolved-tie` | 23 |  |  |  |
| `window` | 164 |  |  |  |
| `window+signature` | 71 |  |  |  |

Requests containing at least one uncertain step: **28** (listed in [`uncertainty_report.md`](uncertainty_report.md)).
<!-- VERIFY:END -->

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

Results: see the leave-one-out table in §1.

The deletion window itself is measured, not assumed: on the 238 proven links the worker deletes the
account **75–41 min before `reported_at`**. The first version used a 3 h window, which let adjacent
batches compete for accounts (31 unresolved instead of 9).

Two further ideas were tested out of sample and **rejected**:
- *"The worker processes a batch newest request first"* — matched 4/4 sample batches, but on proven
  test pairs it agrees 74 times and disagrees 76 times: chance. It is no longer used as a rule.
- *Profile-update events at the time of an identity change* — no proven account shows any.

Requests whose evidence fits several candidates equally are **unresolved ties**. Tom's decision: output a
deterministic pick, because every deletion case must verify its account, and report it as uncertain. The
current counts are in §1; every link's provenance and the affected requests are listed in
[`uncertainty_report.md`](uncertainty_report.md).

## 2. Custom evaluation cases (created by us)

`code/tests/test_engine.py` builds a tiny synthetic dataset for each trap described in the challenge and
asserts the engine's **exact** output; `code/tests/test_stress.py` adds unusual situations;
`code/tests/test_intent.py` tests the model-call safeguards with a scripted fake model. No model
calls (intent is injected); all but the full-run determinism test take milliseconds. **22/22 pass.**

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
| 9 | **Same person, two requests** | Two audits of one person at different dates. | Both find the same directly evidenced account. |
| 10 | **Overlapping batches** | The 11:00 batch's account is deleted at 10:00, inside the 10:00 batch's ±15 min fallback window. | Each request gets its own account (main window first). |
| 11 | **True tie** | Two identical requests, two identical deletions. | Deterministic output, labelled `unresolved-tie`, listed as uncertain. |
| 12 | **Timing signature needs support** | The grid must be learned from a directly evidenced person before it may break a tie. | Tie broken correctly, labelled `window+signature`. |
| 13 | **Malformed evidence file** | One evidence file is invalid JSON. | Skipped with a warning; the rest of the audit is unaffected (previously the whole run crashed). |
| 14 | **No evidence at all** | Nothing links the request to any record. | No crash; account link `none`; serialisable output. |
| 15 | **Determinism across hash seeds** | Two full offline runs under different `PYTHONHASHSEED` values. | Byte-identical `output.csv` (a set-order citation tie was found and fixed this way). |
| 16 | **Grounding check** | A step cites a file that never mentions the record. | Stage 5 flags it. |
| 17 | **Presence seen only in a later file** | A file captured after `audit_at` shows the account still active. | No finding is raised and the file is never cited. |
| 18 | **Missing intent answer** | The model's batch answer skips one request. | That request is re-asked alone and answered. |
| 19 | **Duplicated intent answer** | Two answers for one request. | Neither is trusted; re-asked alone. |
| 20 | **Model unavailable** | No answer at all. | Safe default: audit the deletion (and the intake cross-check flags any non-deletion it contradicts). |
| 21 | **Prompt injection via slot closing** | A request text tries to close its slot and open a new one. | Escaped: exactly one slot. |
| 22 | **Label that does not fit the system** | A search entry labelled as a security audit record. | Ignored; code decides `SEARCH_INDEX_ENTRY`. |

### Sanity checks on the 518 unlabelled requests

- **Intent:** the model marked exactly **10** requests as non-deletion; the activity stream independently
  contains exactly **10** `privacy_intake … classified request as non-deletion` events.
- **Contract and reproduction:** checked by `verify.py` (§1): `output.csv` satisfies every contract rule, and
  the pipeline reproduces it byte for byte. `--fresh` repeats this from an empty model cache; the result of
  the last fresh run is in §1. Output is also identical across Python hash seeds (test 15).

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
| 12 | Tom: "make it reliable on the hidden cases, not the samples". Examined the 30 fallback accounts one by one. | The 3 h window mixed adjacent batches; true offset measured on 238 proven links is 75–41 min. | Window narrowed (±15 min kept only as a fallback). Out-of-sample unresolved 31 → 9, still 0 wrong; real fallback 30 → 23. |
| 13 | Is "newest request first" real? | Out of sample: 74 agree / 76 disagree. | Not a rule. Remaining ties are labelled `unresolved-tie` and reported (Tom's decision A: keep a deterministic pick, report it as uncertain). |
| 14 | A single malformed evidence file crashed the whole run. | Found while writing stress test 13. | Each file is parsed independently; failures are reported, never fatal. |
| 15 | Two identical offline runs differed in one citation (case_393). | Evidence-citation tie broken by Python set order, which varies with the hash seed. | Stable tie-break by evidence id; 6 seeds byte-identical; regression test 15. |
| 16 | Code review: the analytics linker still used the original 3 h window after the account window was narrowed. | 4 "unresolved-tie" analytics links pointed at an anonymization 88–89 min before `reported_at`, i.e. from an earlier batch; every real one sits 21–29 min before. | Analytics events limited to the measured clean-up slot (35–20 min before; proven links put placeholder/legal-hold actions at ~30, support redactions ~25, flow shut-offs ~15, restore blocks ~10 min before), near window only if the slot is empty (same rule as for accounts). Samples unchanged; 4 wrong links removed; uncertain requests 31 → 28. |
| 17 | Report numbers were typed by hand and could drift from the code. | Several figures had to be corrected by hand during the day. | `python code/verify.py` measures everything and writes §1 itself; it fails if any check fails. |
| 18 | New in-pipeline grounding check (every citation must mention its record). | case_126: the account was seen active only in a file captured *after* `audit_at`; the code still reported it as remaining and filled the citation with a file that never mentions the account. The file-level validator could not see this. | A fact known only from a later file can no longer create a finding; the account is judged on admissible evidence (a covering snapshot without it, the same pattern as the sample answers). case_126 → `COMPLETE`. Only output change of the refactor. |
| 19 | The grounding check then flagged our own citation fallback ("the case's main evidence file") in a synthetic test. | Never used on the real data, but not grounded. | Fallback is now: a snapshot covering accounts, else the worker's deletion row for that account. |
| 20 | Code review: 40 untrusted request texts per model call with no isolation; batch answers not checked for completeness; the vision model's label could set any category. | One malicious text could try to steer its neighbours; a skipped answer silently became the default. | Escaped per-request slots, exactly-once answers with single re-asks, per-request cross-check against the intake record (all 10 non-deletion decisions confirmed), labels accepted only if valid for the system. Output unchanged. |

## 4. Limitations and honest caveats

- The 32 samples were used for development; there was no separate hold-out (too few cases). To limit
  over-fitting, every rule is stated as a general principle (time, identity, policy) and checked against
  test-set sanity signals rather than tuned per case.
- 23 test accounts and 9 anonymized-analytics links are unresolved ties (deterministic pick, ~chance); listed in `uncertainty_report.md`.
- Already-anonymized analytics profiles carry no identifiers: 111 test links come from the timing grid. The timing signature is a property of how this dataset's worker logs were produced; on real systems I would ask for the worker's batch log instead of inferring it.
- case_63 depends on a judgement call (catalog link vs. contradicting screenshot), decided by Tom.
