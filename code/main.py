"""Ghost Data Still Exists - deletion-audit agent (entry point).

Usage (from the repository root):
  python code/main.py                       # audit dataset/deletion_requests.csv -> ./output.csv
  python code/main.py --split sample        # audit the 32 samples -> code/evaluation/sample_predictions.csv
  python code/main.py --offline             # never call the API; use cached model answers only
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit.engine import Auditor  # noqa: E402
from audit.evidence import SKIPPED, load_text_evidence  # noqa: E402
from audit.intent import classify  # noqa: E402
from audit.llm import LLM, USAGE  # noqa: E402
from audit.multimodal import load_multimodal_evidence  # noqa: E402


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        from dotenv import load_dotenv
        load_dotenv(env)


def read_cases(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_output(path: Path, results) -> None:
    def key(r):
        return int(r.case_id.split("_")[1])
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in sorted(results, key=key):
            w.writerow([r.case_id, r.status, json.dumps(r.steps, ensure_ascii=False, separators=(",", ":"))])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["test", "sample"], default="test")
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--trace", default=None, help="write per-case reasoning trace (JSONL)")
    a = ap.parse_args()
    ds = Path(a.dataset)

    sample = read_cases(ds / "sample_deletion_requests.csv")
    test = read_cases(ds / "deletion_requests.csv")
    cases = sample if a.split == "sample" else test
    load_env()
    llm = LLM(offline=a.offline)
    observations = load_text_evidence(ds) + load_multimodal_evidence(ds, llm)
    intents = classify(cases, llm)  # only the requests being audited
    auditor = Auditor(ds, observations, sample + test, intents)
    results = [auditor.audit(q) for q in cases]

    out = Path(a.out) if a.out else (ROOT / "output.csv" if a.split == "test"
                                     else ROOT / "code" / "evaluation" / "sample_predictions.csv")
    write_output(out, results)
    if a.trace:
        with open(a.trace, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps({"case_id": r.case_id, "status": r.status, **r.trace}, ensure_ascii=False) + "\n")
    if a.split == "test" and not a.out:
        sys.path.insert(0, str(ROOT / "code" / "evaluation"))
        from uncertainty import write_uncertainty_report
        from usage import write_usage_report
        write_usage_report(ROOT / "code" / "evaluation" / "usage_report.md", len(results), a.split, a.offline)
        write_uncertainty_report(ROOT / "code" / "evaluation" / "uncertainty_report.md", results)
    for msg in SKIPPED:
        print("WARNING skipped evidence file:", msg)
    print(f"wrote {len(results)} rows -> {out}")
    print("model usage:", json.dumps(USAGE.as_dict()))


if __name__ == "__main__":
    main()
