"""The submission contract as code: every rule a row of output.csv must satisfy.

Used in two places: inside the pipeline (each case is checked before it is written) and by
code/evaluation/validate.py (the whole file is checked before packaging).
"""
from __future__ import annotations

from .policy import FINDING_ORDER, OP_STATE, PHASE_OF, REMEDIATION_ORDER, VERIFICATION_ORDER

ORDER = {op: i for i, op in enumerate(FINDING_ORDER + REMEDIATION_ORDER + VERIFICATION_ORDER)}
STEP_KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")


def check_case(case_id: str, status: str, steps: list[dict], audit_at: str, evidence_time: dict[str, str]) -> list[str]:
    """Return every contract violation for one case (empty list = valid).

    evidence_time maps each citable id (file_id / activity_id / audit_id) to when it was captured."""
    errs: list[str] = []
    if status not in STATUSES:
        errs.append(f"{case_id}: bad status {status}")
    if status == "NOT_APPLICABLE" and steps:
        errs.append(f"{case_id}: NOT_APPLICABLE with steps")
    if status != "NOT_APPLICABLE" and not steps:
        errs.append(f"{case_id}: {status} with no steps")
    if [s.get("step_no") for s in steps] != list(range(1, len(steps) + 1)):
        errs.append(f"{case_id}: step numbers not consecutive from 1")
    keys = [(ORDER.get(s.get("operation"), -1), s.get("system_id"), s.get("resource_id")) for s in steps]
    if keys != sorted(keys):
        errs.append(f"{case_id}: steps out of order")
    for s in steps:
        op, ev = s.get("operation"), s.get("evidence_id", "")
        if set(s) != STEP_KEYS:
            errs.append(f"{case_id}: step keys {sorted(s)}")
        if op not in OP_STATE or s.get("state") != OP_STATE[op] or s.get("phase") != PHASE_OF[op]:
            errs.append(f"{case_id}: bad op/state/phase {op}/{s.get('state')}/{s.get('phase')}")
        needs = s.get("phase") == "FINDING" or status == "COMPLETE"
        if needs and not ev:
            errs.append(f"{case_id}: step {s.get('step_no')} needs evidence")
        if not needs and ev:
            errs.append(f"{case_id}: future step {s.get('step_no')} must have empty evidence")
        if ev and (ev not in evidence_time or evidence_time[ev] > audit_at):
            errs.append(f"{case_id}: evidence {ev} unknown or captured after audit_at")
    if status == "COMPLETE" and any(s.get("phase") != "VERIFICATION" for s in steps):
        errs.append(f"{case_id}: COMPLETE must contain only verification steps")
    if status == "INCOMPLETE" and not any(s.get("phase") == "FINDING" for s in steps):
        errs.append(f"{case_id}: INCOMPLETE without findings")
    return errs
