"""Ghost Data Still Exists - deletion-audit agent (entry point).

Usage (from the repository root):
  python code/main.py                       # audit dataset/deletion_requests.csv -> ./output.csv (+ reports)
  python code/main.py --split sample        # audit the 32 samples -> code/evaluation/sample_predictions.csv
  python code/main.py --offline             # never call the API; use cached model answers only
  python code/main.py --explain case_63     # print the full evidence chain for one request
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit.clock import canon  # noqa: E402
from audit.engine import Auditor, CaseResult  # noqa: E402
from audit.evidence import SKIPPED, evidence_times, load_text_evidence  # noqa: E402
from audit.intent import classify  # noqa: E402
from audit.llm import LLM, USAGE  # noqa: E402
from audit.multimodal import load_multimodal_evidence  # noqa: E402
from audit.reports import write_uncertainty_report, write_usage_report  # noqa: E402


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        from dotenv import load_dotenv
        load_dotenv(env)


def read_cases(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:  # canonical UTC timestamps, so comparisons with evidence times are safe
        for k in ("requested_at", "reported_at", "audit_at"):
            r[k] = canon(r[k])
    return rows


def write_output(path: Path, results: list[CaseResult]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in sorted(results, key=lambda r: int(r.case_id.split("_")[1])):
            w.writerow([r.case_id, r.status, json.dumps(r.steps, ensure_ascii=False, separators=(",", ":"))])


def explain(r: CaseResult, q: dict) -> str:
    """Human-readable evidence chain for one request."""
    t = r.trace
    out = [f"{r.case_id}  ({q['email']})",
           f"  requested {q['requested_at']}  workflow said {q['reported_status']} at {q['reported_at']}  audit at {q['audit_at']}",
           f"  request: {q['request_text'][:160]}",
           f"  intent: {t['intent'].get('request_type', '?')} -> authorizes deletion = {t['intent'].get('authorizes_deletion')}"
           f"  ({t['intent'].get('source', '')})",
           f"  person: {', '.join(t['users']) or 'not in the identity table'}"]
    if "account" in t:
        out.append(f"  account: {t['account']['resource_id']}  (link: {t['account']['link']})")
    for rid, aid, how in t.get("activity_linked", []):
        out.append(f"  batch link: {rid} via {aid} ({how})")
    for d in t.get("resources", []):
        out.append(f"  - {d['resource']}: {d.get('decision', '')}  [{d['observations_citable']} citable / "
                   f"{d['observations_known']} known observations]")
    out += [f"  policy: {line}" for line in t.get("reasoning", [])]
    out.append(f"  RESULT: {r.status}")
    out += [f"    {s['step_no']:>2}. {s['phase']:<12} {s['operation']:<26} {s['resource_id']:<18} {s['evidence_id']}"
            for s in r.steps]
    out.append(f"  uncertain: {', '.join(t.get('uncertain', [])) or 'none'}")
    out.append(f"  verification: {'passed' if not t.get('verification') else '; '.join(t['verification'])}")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["test", "sample"], default="test")
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--trace", default=None, help="write per-case reasoning trace (JSONL)")
    ap.add_argument("--explain", default=None, metavar="CASE_ID", help="print the evidence chain for one request")
    a = ap.parse_args()
    ds = Path(a.dataset)
    load_env()

    sample = read_cases(ds / "sample_deletion_requests.csv")
    test = read_cases(ds / "deletion_requests.csv")
    if a.explain:
        a.split = "sample" if any(q["case_id"] == a.explain for q in sample) else "test"
    cases = sample if a.split == "sample" else test

    # perceive: parse every evidence file; the model reads screenshots/PDFs and request intent
    llm = LLM(offline=a.offline)
    observations = load_text_evidence(ds) + load_multimodal_evidence(ds, llm)
    intents = classify(cases, llm)
    # link -> reconstruct -> decide -> batch -> verify  (audit/engine.py)
    auditor = Auditor(ds, observations, sample + test, intents, evidence_times(ds))
    results = [auditor.audit(q) for q in cases]

    if a.explain:
        q = next(q for q in cases if q["case_id"] == a.explain)
        print(explain(auditor.audit(q), q))
        return

    out = Path(a.out) if a.out else (ROOT / "output.csv" if a.split == "test"
                                     else ROOT / "code" / "evaluation" / "sample_predictions.csv")
    write_output(out, results)
    if a.trace:
        with open(a.trace, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps({"case_id": r.case_id, "status": r.status, **r.trace}, ensure_ascii=False) + "\n")
    if a.split == "test" and not a.out:
        write_usage_report(ROOT / "code" / "evaluation" / "usage_report.md", len(results), a.split, a.offline)
        write_uncertainty_report(ROOT / "code" / "evaluation" / "uncertainty_report.md", results)
    flagged = [e for r in results for e in r.trace.get("verification", [])]
    for msg in SKIPPED:
        print("WARNING skipped evidence file:", msg)
    for msg in flagged:
        print("WARNING verification:", msg)
    print(f"wrote {len(results)} rows -> {out}  (verification: {len(flagged)} problem(s))")
    print("model usage:", json.dumps(USAGE.as_dict()))


if __name__ == "__main__":
    main()
