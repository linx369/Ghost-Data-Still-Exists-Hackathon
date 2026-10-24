"""Check output.csv against the submission contract before packaging.

Usage: python code/evaluation/validate.py [output.csv]
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))
from audit.policy import FINDING_ORDER, OP_STATE, PHASE_OF, REMEDIATION_ORDER, VERIFICATION_ORDER  # noqa: E402

ORDER = {op: i for i, op in enumerate(FINDING_ORDER + REMEDIATION_ORDER + VERIFICATION_ORDER)}
KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def main(path: str) -> int:
    ds = ROOT / "dataset"
    reqs = {r["case_id"]: r for r in csv.DictReader(open(ds / "deletion_requests.csv", encoding="utf-8"))}
    when = {r["file_id"]: r["captured_at"] for r in csv.DictReader(open(ds / "evidence_files.csv", encoding="utf-8"))}
    when |= {r["activity_id"]: r["event_time"] for r in csv.DictReader(open(ds / "system_activity.csv", encoding="utf-8"))}
    when |= {r["audit_id"]: r["event_time"] for r in csv.DictReader(open(ds / "user_audit_log.csv", encoding="utf-8"))}
    errs: list[str] = []
    with open(path, encoding="utf-8") as f:
        header = f.readline().strip()
        f.seek(0)
        rows = list(csv.DictReader(f))
    if header != "case_id,deletion_status,steps_json":
        errs.append(f"bad header: {header}")
    ids = [r["case_id"] for r in rows]
    if sorted(ids, key=lambda c: int(c.split("_")[1])) != ids:
        errs.append("rows not sorted by case_id")
    if set(ids) != set(reqs) or len(ids) != len(reqs):
        errs.append(f"row set mismatch: {len(ids)} rows vs {len(reqs)} requests")
    for r in rows:
        cid, status = r["case_id"], r["deletion_status"]
        audit_at = reqs.get(cid, {}).get("audit_at", "")
        try:
            steps = json.loads(r["steps_json"])
        except json.JSONDecodeError:
            errs.append(f"{cid}: invalid JSON")
            continue
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            errs.append(f"{cid}: bad status {status}")
        if status == "NOT_APPLICABLE" and steps:
            errs.append(f"{cid}: NOT_APPLICABLE with steps")
        if status != "NOT_APPLICABLE" and not steps:
            errs.append(f"{cid}: {status} with no steps")
        if [s.get("step_no") for s in steps] != list(range(1, len(steps) + 1)):
            errs.append(f"{cid}: step numbers not consecutive from 1")
        keys = [(ORDER.get(s.get("operation"), -1), s.get("system_id"), s.get("resource_id")) for s in steps]
        if keys != sorted(keys):
            errs.append(f"{cid}: steps out of order")
        for s in steps:
            op = s.get("operation")
            if set(s) != KEYS:
                errs.append(f"{cid}: step keys {sorted(s)}")
            if op not in OP_STATE or s.get("state") != OP_STATE[op] or s.get("phase") != PHASE_OF[op]:
                errs.append(f"{cid}: bad op/state/phase {op}/{s.get('state')}/{s.get('phase')}")
            ev = s.get("evidence_id", "")
            needs = s.get("phase") == "FINDING" or status == "COMPLETE"
            if needs and not ev:
                errs.append(f"{cid}: step {s.get('step_no')} needs evidence")
            if not needs and ev:
                errs.append(f"{cid}: future step {s.get('step_no')} must have empty evidence")
            if ev and (ev not in when or when[ev] > audit_at):
                errs.append(f"{cid}: evidence {ev} unknown or captured after audit_at")
        if status == "COMPLETE" and any(s.get("phase") != "VERIFICATION" for s in steps):
            errs.append(f"{cid}: COMPLETE must contain only verification steps")
        if status == "INCOMPLETE" and not any(s.get("phase") == "FINDING" for s in steps):
            errs.append(f"{cid}: INCOMPLETE without findings")
    for e in errs[:50]:
        print("ERROR", e)
    print(f"{len(rows)} rows checked, {len(errs)} problem(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "output.csv")))
