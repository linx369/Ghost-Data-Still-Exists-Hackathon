"""Stress tests for unusual situations (Tom's priority 2): several requests for the same person,
overlapping batches, unresolvable ties, malformed or missing evidence, and the timing signature
deciding (or refusing to decide) between accounts.

Run:  python -m pytest code/tests -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import evidence as ev  # noqa: E402
from audit.engine import Auditor  # noqa: E402
from audit.evidence import load_text_evidence  # noqa: E402
from test_engine import AUDIT_REC, REQ, USER, build  # noqa: E402


def act(i, t, rid, etype="DELETE_RECORD", target="account_service", result="SUCCESS"):
    return {"activity_id": f"activity_{i}", "event_time": t, "event_type": etype, "source_system": "deletion_worker",
            "target_system": target, "resource_id": rid, "reported_result": result, "details": f"worker {rid}"}


def person(n):
    return {**USER, "user_id": f"user_{n}", "name": f"Person {n}", "email": f"p{n}@example.com",
            "phone": f"+1 555 01{n:02d}", "email_hash": f"{n:x}".rjust(64, "0")}


def req(n, **kw):
    return {**REQ, "case_id": f"case_{n}", "email": f"p{n}@example.com", **kw}


def audit_all(ds, reqs):
    a = Auditor(ds, load_text_evidence(ds), reqs, {q["case_id"]: {"authorizes_deletion": True} for q in reqs})
    return {q["case_id"]: a.audit(q) for q in reqs}


def account_of(result):
    return result.trace["account"]


def test_same_person_two_requests_share_their_account(tmp_path):
    """The same person asks twice; both audits must find the same, directly evidenced account."""
    snap = {"file_id": "evidence_01", "captured_at": "2026-09-25T00:00:00Z", "data_store": "account_service",
            "records": [{"resource_id": "account_1", "customer_reference": "user_900", "email": USER["email"],
                         "account_status": "ACTIVE"}]}
    ds, r1 = build(tmp_path, [snap])
    r2 = {**r1, "case_id": "case_2", "requested_at": "2026-09-20T10:00:00Z", "reported_at": "2026-09-21T12:00:00Z",
          "audit_at": "2026-09-30T12:00:00Z"}
    r1 = {**r1, "audit_at": "2026-09-30T12:00:00Z"}
    out = audit_all(ds, [r1, r2])
    for cid in ("case_1", "case_2"):
        assert account_of(out[cid]) == {"resource_id": "account_1", "link": "direct"}
        assert out[cid].status == "INCOMPLETE"  # the profile is still ACTIVE after both requests


def test_overlapping_batches_do_not_steal_accounts(tmp_path):
    """Batches at 10:00 and 11:00: the 11:00 batch's account is deleted at 10:00, inside the
    10:00 batch's +-15 min fallback window. The measured main window must keep them apart."""
    users = [person(1), person(2)]
    acts = [act(1, "2026-09-02T09:00:00Z", "account_10"),   # batch 10:00 (-60 min)
            act(2, "2026-09-02T10:00:00Z", "account_20")]   # batch 11:00 (-60 min); 0 min for batch 10:00
    ds, _ = build(tmp_path, [], activity=acts, users=users)
    a = req(1, reported_at="2026-09-02T10:00:00Z")
    b = req(2, reported_at="2026-09-02T11:00:00Z")
    out = audit_all(ds, [a, b])
    assert account_of(out["case_1"])["resource_id"] == "account_10"
    assert account_of(out["case_2"])["resource_id"] == "account_20"


def test_true_tie_is_labelled_and_deterministic(tmp_path):
    """Two identical requests, two identical deletions: nothing separates them. The output must be
    deterministic and the link must be labelled as a fallback, never as evidence."""
    users = [person(1), person(2)]
    acts = [act(1, "2026-09-02T11:00:00Z", "account_10"), act(2, "2026-09-02T11:05:00Z", "account_20")]
    ds, _ = build(tmp_path, [], activity=acts, users=users)
    reqs = [req(1), req(2)]
    first, second = audit_all(ds, reqs), audit_all(ds, reqs)
    for cid in ("case_1", "case_2"):
        assert account_of(first[cid])["link"] == "unresolved-tie"
        assert first[cid].trace["uncertain"] == [account_of(first[cid])["resource_id"]]
        assert first[cid].steps == second[cid].steps
    assert {account_of(first[c])["resource_id"] for c in first} == {"account_10", "account_20"}


def test_timing_signature_breaks_a_tie_only_with_learned_support(tmp_path):
    """Person 1 has a support ticket redacted by the worker exactly 30:10 after account_10's deletion.
    Person 3 (directly evidenced) teaches the model that redactions sit at +10 s mod 60."""
    users = [person(1), person(2), person(3)]
    snaps = [{"file_id": "evidence_01", "captured_at": "2026-09-05T00:00:00Z", "data_store": "mixed", "records": [
        {"resource_id": "support_1", "subject_reference": "user_1", "message_text": "no pii"},
        {"resource_id": "support_3", "subject_reference": "user_3", "message_text": "no pii"},
        {"resource_id": "support_4", "subject_reference": "user_3", "message_text": "no pii"},
        {"resource_id": "account_30", "customer_reference": "user_3", "account_status": "DELETED"}]}]
    acts = [act(1, "2026-09-02T10:50:00Z", "account_10"), act(2, "2026-09-02T10:55:17Z", "account_20"),
            act(3, "2026-09-02T11:20:10Z", "support_1", "REDACT_RECORD", "support_platform"),
            act(4, "2026-08-01T10:00:00Z", "account_30"),
            act(5, "2026-08-01T10:30:10Z", "support_3", "REDACT_RECORD", "support_platform"),
            act(6, "2026-08-01T10:45:10Z", "support_4", "REDACT_RECORD", "support_platform")]
    ds, _ = build(tmp_path, snaps, activity=acts, users=users)
    out = audit_all(ds, [req(1), req(2)])
    assert account_of(out["case_1"]) == {"resource_id": "account_10", "link": "window+signature"}
    assert account_of(out["case_2"])["resource_id"] == "account_20"


def test_malformed_evidence_file_is_skipped_not_fatal(tmp_path):
    good = {"file_id": "evidence_01", "captured_at": "2026-09-05T00:00:00Z", "data_store": "security_service",
            "records": [AUDIT_REC]}
    ds, r = build(tmp_path, [good])
    (ds / "evidence" / "records" / "broken.json").write_text("{not json")
    with open(ds / "evidence_files.csv", "a", encoding="utf-8") as f:
        f.write("evidence_02,evidence/records/broken.json,test,2026-09-05T00:00:00Z\n")
    ev.SKIPPED.clear()
    out = audit_all(ds, [r])
    assert any("evidence_02" in s for s in ev.SKIPPED)
    assert out["case_1"].status == "COMPLETE"
    assert ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_1") in {
        (s["phase"], s["operation"], s["resource_id"]) for s in out["case_1"].steps}


def test_no_evidence_and_no_activity_does_not_crash(tmp_path):
    ds, r = build(tmp_path, [], activity=[act(1, "2025-01-01T00:00:00Z", "account_999")])
    out = audit_all(ds, [r])
    assert out["case_1"].status in ("COMPLETE", "INCOMPLETE")
    assert account_of(out["case_1"])["link"] == "none"
    json.dumps(out["case_1"].steps)  # always serialisable


def test_full_run_is_identical_across_hash_seeds(tmp_path):
    """Regression: a citation tie was once broken by set iteration order, which changes with
    PYTHONHASHSEED. Two full offline runs under different seeds must be byte-identical."""
    import os
    import subprocess
    root = CODE.parent
    if not (root / "dataset" / "deletion_requests.csv").exists() or not (CODE / "cache").exists():
        import pytest
        pytest.skip("needs the dataset and the model-answer cache")
    outs = []
    for seed in ("1", "987"):
        out = tmp_path / f"out_{seed}.csv"
        subprocess.run([sys.executable, str(CODE / "main.py"), "--offline", "--out", str(out)], check=True,
                       env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True)
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]


def test_verification_flags_a_citation_that_does_not_mention_the_record(tmp_path):
    """Stage 5 grounding: citing a file that never mentions the record must be flagged."""
    from audit.engine import CaseResult
    snap = {"file_id": "evidence_01", "captured_at": "2026-09-05T00:00:00Z", "data_store": "security_service",
            "records": [AUDIT_REC]}
    ds, r = build(tmp_path, [snap])
    a = Auditor(ds, load_text_evidence(ds), [r], {r["case_id"]: {"authorizes_deletion": True}})
    good = a.audit(r)
    assert good.trace["verification"] == []
    bad = CaseResult("case_1", "COMPLETE", [{"step_no": 1, "phase": "VERIFICATION", "system_id": "search_service",
                                              "resource_id": "search_9", "operation": "VERIFY_RECORD_ABSENT",
                                              "state": "ABSENT", "evidence_id": "evidence_01"}])
    assert any("does not mention search_9" in e for e in a._verify(r, bad))


def test_presence_seen_only_after_audit_never_becomes_a_finding(tmp_path):
    """A file captured after audit_at shows the account still ACTIVE: it may not support a finding."""
    late = {"file_id": "evidence_09", "captured_at": "2026-09-20T00:00:00Z", "data_store": "account_service",
            "records": [{"resource_id": "account_1", "customer_reference": "user_900", "email": USER["email"],
                         "account_status": "ACTIVE"}]}
    ds, r = build(tmp_path, [late])
    late_path = ds / "evidence" / "records" / "evidence_09.json"
    doc = json.loads(late_path.read_text())
    doc["snapshots"][0]["captured_at"] = "2026-09-05T00:00:00Z"   # the fact is from before audit_at...
    late_path.write_text(json.dumps(doc))                         # ...but the file was captured after it
    out = audit_all(ds, [r])["case_1"]
    assert all(s["phase"] != "FINDING" for s in out.steps)
    assert all(s["evidence_id"] != "evidence_09" for s in out.steps)
