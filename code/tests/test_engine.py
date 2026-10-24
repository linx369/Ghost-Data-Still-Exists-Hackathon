"""Custom evaluation cases: each builds a tiny synthetic dataset around one trap from the
challenge and checks the engine's exact output. No model calls (intent is injected).

Run:  python -m pytest code/tests -q
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
REAL_RULES = CODE.parent / "dataset" / "deletion_rules.csv"

from audit.engine import Auditor  # noqa: E402
from audit.evidence import load_text_evidence  # noqa: E402

HASH = "a" * 64
USER = {"user_id": "user_900", "name": "Test Person", "email": "test.person@example.com",
        "phone": "+1 555 0100", "email_hash": HASH, "updated_at": "2026-01-01T00:00:00Z"}
REQ = {"case_id": "case_1", "email": "test.person@example.com", "requested_at": "2026-09-01T10:00:00Z",
       "reported_status": "COMPLETE", "reported_at": "2026-09-02T12:00:00Z", "audit_at": "2026-09-10T12:00:00Z",
       "request_text": "Please delete my account."}


def _csv(path: Path, rows: list[dict], header: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def build(tmp: Path, snapshots: list[dict], *, audit_log=(), activity=(), req=None, users=(USER,)) -> tuple[Path, dict]:
    """snapshots: [{"file_id", "captured_at", "data_store", "records": [...]}]"""
    req = {**REQ, **(req or {})}
    (tmp / "evidence" / "records").mkdir(parents=True)
    shutil.copy(REAL_RULES, tmp / "deletion_rules.csv")
    _csv(tmp / "user_identities.csv", list(users), list(USER))
    _csv(tmp / "user_audit_log.csv", list(audit_log), ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"])
    acts = list(activity) or [{"activity_id": "activity_1", "event_time": "2026-09-02T11:00:00Z",
                               "event_type": "DELETE_RECORD", "source_system": "deletion_worker",
                               "target_system": "account_service", "resource_id": "account_1",
                               "reported_result": "SUCCESS", "details": "Deletion worker submitted primary profile account_1."}]
    _csv(tmp / "system_activity.csv", acts, ["activity_id", "event_time", "event_type", "source_system",
                                              "target_system", "resource_id", "reported_result", "details"])
    manifest = []
    for s in snapshots:
        name = f"evidence/records/{s['file_id']}.json"
        (tmp / name).write_text(json.dumps({"exported_at": s["captured_at"], "source": "test", "snapshots": [
            {"captured_at": s["captured_at"], "data_store": s["data_store"], "collection": "c", "records": s["records"]}]}))
        manifest.append({"file_id": s["file_id"], "file_path": name, "origin": "test", "captured_at": s["captured_at"]})
    _csv(tmp / "evidence_files.csv", manifest, ["file_id", "file_path", "origin", "captured_at"])
    _csv(tmp / "deletion_requests.csv", [req], list(REQ))
    return tmp, req


def run(ds: Path, req: dict, authorizes=True):
    auditor = Auditor(ds, load_text_evidence(ds), [req], {req["case_id"]: {"authorizes_deletion": authorizes}})
    r = auditor.audit(req)
    return r.status, [(s["phase"], s["operation"], s["resource_id"], s["evidence_id"]) for s in r.steps]


AUDIT_REC = {"resource_id": "audit_record_1", "subject_reference": "user_900", "event_name": "account_deletion_reviewed"}


def test_replica_drift_reports_remaining_account(tmp_path):
    """Primary lost the row but a read replica still holds the profile after the deletion."""
    ds, req = build(tmp_path, [
        {"file_id": "evidence_01", "captured_at": "2026-09-05T00:00:00Z", "data_store": "account_service",
         "records": [{"resource_id": "account_1", "customer_reference": "user_900", "email": "test.person@example.com",
                      "account_status": "ACTIVE"}]},
        {"file_id": "evidence_02", "captured_at": "2026-09-05T00:00:00Z", "data_store": "security_service",
         "records": [AUDIT_REC]}])
    status, steps = run(ds, req)
    assert status == "INCOMPLETE"
    assert steps == [("FINDING", "REPORT_REMAINING_RECORD", "account_1", "evidence_01"),
                     ("REMEDIATION", "DELETE_RECORD", "account_1", ""),
                     ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_1", ""),
                     ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_1", "")]


def test_historical_email_is_resolved(tmp_path):
    """Request arrives from an email the person used before changing it; a search entry still carries it."""
    old = "old.address@example.com"
    ds, req = build(tmp_path, [
        {"file_id": "evidence_01", "captured_at": "2026-09-05T00:00:00Z", "data_store": "search_service",
         "records": [{"resource_id": "search_7", "index_name": "x", "document": {"email": old}}]}],
        audit_log=[{"audit_id": "audit_1", "user_id": "user_900", "event_time": "2025-05-01T00:00:00Z",
                    "field_name": "email", "old_value": old, "new_value": USER["email"]}],
        req={"email": old})
    status, steps = run(ds, req)
    assert status == "INCOMPLETE"
    assert ("FINDING", "REPORT_REMAINING_RECORD", "search_7", "evidence_01") in steps


def test_sighting_before_request_does_not_set_state(tmp_path):
    """A search entry seen before the request may have been removed by the deletion run."""
    ds, req = build(tmp_path, [
        {"file_id": "evidence_01", "captured_at": "2026-08-01T00:00:00Z", "data_store": "search_service",
         "records": [{"resource_id": "search_7", "index_name": "x", "document": {"email": USER["email"]}}]},
        {"file_id": "evidence_02", "captured_at": "2026-09-05T00:00:00Z", "data_store": "security_service",
         "records": [AUDIT_REC]}])
    status, steps = run(ds, req)
    assert status == "COMPLETE"
    assert all(s[2] != "search_7" for s in steps)


def test_evidence_captured_after_audit_is_not_cited(tmp_path):
    """A snapshot taken after audit_at must not change or support the answer."""
    ds, req = build(tmp_path, [
        {"file_id": "evidence_09", "captured_at": "2026-09-20T00:00:00Z", "data_store": "search_service",
         "records": [{"resource_id": "search_7", "index_name": "x", "document": {"email": USER["email"]}}]},
        {"file_id": "evidence_02", "captured_at": "2026-09-05T00:00:00Z", "data_store": "security_service",
         "records": [AUDIT_REC]}])
    status, steps = run(ds, req)
    assert status == "COMPLETE"
    assert all(s[3] != "evidence_09" for s in steps)


def test_lookalike_decoy_is_not_linked(tmp_path):
    """A record for test.person.jr@ belongs to someone else."""
    ds, req = build(tmp_path, [
        {"file_id": "evidence_01", "captured_at": "2026-09-05T00:00:00Z", "data_store": "search_service",
         "records": [{"resource_id": "search_8", "index_name": "x", "document": {"email": "test.person.jr@example.com"}}]},
        {"file_id": "evidence_02", "captured_at": "2026-09-05T00:00:00Z", "data_store": "security_service",
         "records": [AUDIT_REC]}])
    status, steps = run(ds, req)
    assert status == "COMPLETE"
    assert all(s[2] != "search_8" for s in steps)


def test_non_deletion_request_is_not_applicable(tmp_path):
    ds, req = build(tmp_path, [], req={"request_text": "Please send me a copy of my data. Keep my account."})
    assert run(ds, req, authorizes=False) == ("NOT_APPLICABLE", [])


def test_phase_and_operation_ordering(tmp_path):
    """Findings by operation order then system; flows disabled before records are changed."""
    ds, req = build(tmp_path, [
        {"file_id": "evidence_01", "captured_at": "2026-09-05T00:00:00Z", "data_store": "mixed", "records": [
            {"resource_id": "support_3", "subject_reference": "user_900",
             "message_text": "Send the update to test.person@example.com about the order."},
            {"resource_id": "search_7", "index_name": "x", "document": {"email": USER["email"]}},
            {"resource_id": "analytics_5", "email": USER["email"], "user_id": None},
            {"resource_id": "data_flow_2", "subject_selector": USER["email"], "enabled": True},
            {"resource_id": "backup_4", "subject_reference": "user_900", "restore_allowed": True},
            AUDIT_REC]}])
    status, steps = run(ds, req)
    ops = [(s[0], s[1], s[2]) for s in steps]
    assert status == "INCOMPLETE"
    assert ops == [
        ("FINDING", "REPORT_REMAINING_RECORD", "search_7"),
        ("FINDING", "REPORT_EXPOSED_PII", "analytics_5"),
        ("FINDING", "REPORT_EXPOSED_PII", "support_3"),
        ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "data_flow_2"),
        ("FINDING", "REPORT_RESTORE_RISK", "backup_4"),
        ("REMEDIATION", "DISABLE_DATA_FLOW", "data_flow_2"),
        ("REMEDIATION", "BLOCK_RESTORE", "backup_4"),
        ("REMEDIATION", "DELETE_RECORD", "search_7"),
        ("REMEDIATION", "REDACT_RECORD", "support_3"),
        ("REMEDIATION", "ANONYMIZE_RECORD", "analytics_5"),
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_1"),
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_7"),
        ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_1"),
        ("VERIFICATION", "VERIFY_RECORD_REDACTED", "support_3"),
        ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "analytics_5"),
        ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "data_flow_2"),
        ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_4"),
    ]


def test_complete_case_cites_evidence_for_every_check(tmp_path):
    ds, req = build(tmp_path, [
        {"file_id": "evidence_01", "captured_at": "2026-09-05T00:00:00Z", "data_store": "mixed", "records": [
            {"resource_id": "tombstone_6", "subject_reference": "user_900", "name": None, "email": None,
             "phone": None, "email_hash": None, "account_status": "DELETED"},
            {"resource_id": "data_flow_2", "subject_selector": "user_900", "enabled": False},
            AUDIT_REC]}])
    status, steps = run(ds, req)
    assert status == "COMPLETE"
    assert all(s[0] == "VERIFICATION" and s[3] for s in steps)
    assert ("VERIFICATION", "VERIFY_RECORD_REDACTED", "tombstone_6", "evidence_01") in steps


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
