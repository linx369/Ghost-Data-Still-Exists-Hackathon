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
from audit.clock import canon  # noqa: E402
from audit.contract import check_case  # noqa: E402


def evidence_times(dataset: Path) -> dict[str, str]:
    """When each citable id became available: evidence files, activity rows, audit-log rows."""
    def rows(name):
        with open(dataset / name, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    when = {r["file_id"]: canon(r["captured_at"]) for r in rows("evidence_files.csv")}
    when |= {r["activity_id"]: canon(r["event_time"]) for r in rows("system_activity.csv")}
    when |= {r["audit_id"]: canon(r["event_time"]) for r in rows("user_audit_log.csv")}
    return when


def validate(path: Path, dataset: Path = ROOT / "dataset") -> tuple[int, list[str]]:
    """Return (rows checked, list of problems)."""
    with open(dataset / "deletion_requests.csv", encoding="utf-8") as f:
        reqs = {r["case_id"]: r for r in csv.DictReader(f)}
    when = evidence_times(dataset)
    with open(path, encoding="utf-8") as f:
        header = f.readline().strip()
        f.seek(0)
        rows = list(csv.DictReader(f))
    errs: list[str] = []
    if header != "case_id,deletion_status,steps_json":
        errs.append(f"bad header: {header}")
    ids = [r["case_id"] for r in rows]
    if sorted(ids, key=lambda c: int(c.split("_")[1])) != ids:
        errs.append("rows not sorted by case_id")
    if set(ids) != set(reqs) or len(ids) != len(reqs):
        errs.append(f"row set mismatch: {len(ids)} rows vs {len(reqs)} requests")
    for r in rows:
        try:
            steps = json.loads(r["steps_json"])
        except json.JSONDecodeError:
            errs.append(f"{r['case_id']}: invalid JSON")
            continue
        audit_at = canon(reqs.get(r["case_id"], {}).get("audit_at", ""))
        errs += check_case(r["case_id"], r["deletion_status"], steps, audit_at, when)
    return len(rows), errs


def main() -> int:
    n, errs = validate(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output.csv")
    for e in errs[:50]:
        print("ERROR", e)
    print(f"{n} rows checked, {len(errs)} problem(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
