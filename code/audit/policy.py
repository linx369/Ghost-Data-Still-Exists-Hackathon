"""Deterministic policy engine: resource states at audit_at -> decision + ordered steps.

The mapping from deletion_rules.csv categories to required treatment is read from the
rules file at runtime; the step vocabulary and ordering come from the problem statement.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                      "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
PHASE_OF = {**{o: "FINDING" for o in FINDING_ORDER}, **{o: "REMEDIATION" for o in REMEDIATION_ORDER},
            **{o: "VERIFICATION" for o in VERIFICATION_ORDER}}


def load_rules(dataset: Path) -> dict[str, str]:
    with open(dataset / "deletion_rules.csv", encoding="utf-8") as f:
        return {r["data_category"]: r["required_action"] for r in csv.DictReader(f)}


@dataclass
class ResourceState:
    system_id: str
    resource_id: str
    category: str
    state: str            # PRESENT / ABSENT / CLEAN / ACTIVE / STOPPED / RETAINED
    evidence_id: str      # evidence proving that state at audit_at
    note: str = ""


def treatment(category: str, rules: dict[str, str]) -> str:
    if category == "DATA_FLOW":
        return "DISABLE"
    if category == "BACKUP_ARCHIVE":
        return "BLOCK"
    return rules.get(category, "DELETE")


def plan(resources: list[ResourceState], rules: dict[str, str]) -> tuple[str, list[dict], list[str]]:
    """Return (deletion_status, steps, reasoning lines)."""
    findings, remediation, verification = [], [], []
    why = []
    for r in resources:
        t = treatment(r.category, rules)
        f = m = v = None
        if t == "DELETE":
            v = "VERIFY_RECORD_ABSENT"
            if r.state in ("PRESENT", "CLEAN"):
                f, m = "REPORT_REMAINING_RECORD", "DELETE_RECORD"
            elif r.category != "USER_PROFILE":
                v = None  # already gone and not the primary account: nothing to prove
        elif t in ("REDACT", "ANONYMIZE"):
            v = "VERIFY_RECORD_REDACTED" if t == "REDACT" else "VERIFY_RECORD_ANONYMIZED"
            if r.state == "PRESENT":
                f, m = "REPORT_EXPOSED_PII", ("REDACT_RECORD" if t == "REDACT" else "ANONYMIZE_RECORD")
            elif r.state == "ABSENT":
                v = None
        elif t == "RETAIN":
            v = "VERIFY_RECORD_PRESENT" if r.state in ("RETAINED", "PRESENT", "CLEAN") else None
        elif t == "DISABLE":
            v = "VERIFY_DATA_FLOW_DISABLED"
            if r.state == "ACTIVE":
                f, m = "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW"
        elif t == "BLOCK":
            v = "VERIFY_RESTORE_BLOCKED"
            if r.state == "ACTIVE":
                f, m = "REPORT_RESTORE_RISK", "BLOCK_RESTORE"
        why.append(f"{r.resource_id} [{r.category}] state={r.state} via {r.evidence_id} -> "
                   f"{t}{': ' + f if f else ''}{' ' + r.note if r.note else ''}")
        if f:
            findings.append((f, r))
        if m:
            remediation.append((m, r))
        if v:
            verification.append((v, r))

    status = "INCOMPLETE" if findings else "COMPLETE"
    steps = []

    def add(group, order, ev_fn):
        for op, r in sorted(group, key=lambda x: (order.index(x[0]), x[1].system_id, x[1].resource_id)):
            steps.append({"step_no": len(steps) + 1, "phase": PHASE_OF[op], "system_id": r.system_id,
                          "resource_id": r.resource_id, "operation": op, "state": OP_STATE[op],
                          "evidence_id": ev_fn(r)})

    add(findings, FINDING_ORDER, lambda r: r.evidence_id)
    add(remediation, REMEDIATION_ORDER, lambda r: "")
    add(verification, VERIFICATION_ORDER, (lambda r: "") if status == "INCOMPLETE" else (lambda r: r.evidence_id))
    return status, steps, why
