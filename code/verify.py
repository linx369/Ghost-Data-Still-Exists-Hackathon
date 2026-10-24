"""One command that re-checks every claim in the reports, and writes the measured results into
code/evaluation/evaluation_report.md (between the VERIFY markers), so the report cannot drift
from what the code actually does.

  python code/verify.py            # offline: no API key needed (uses the cached model answers)
  python code/verify.py --fresh    # also re-run from an EMPTY model cache and compare (API calls, ~$2.50)

Checks: unit/stress tests, sample scoring, leave-one-out validation of account linking,
that the pipeline reproduces the submitted output.csv byte for byte, the output contract,
and the uncertainty counts. Exits non-zero if any check fails.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CODE = Path(__file__).resolve().parent
ROOT = CODE.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "evaluation"))

from loo_timing import leave_one_out  # noqa: E402
from validate import validate  # noqa: E402


def _scorer():
    """code/evaluation/main.py (the starter's scorer entry point) shares its name with code/main.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("scorer", CODE / "evaluation" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.load, mod.score


load_csv, score = _scorer()

REPORT = CODE / "evaluation" / "evaluation_report.md"
START, END = "<!-- VERIFY:START -->", "<!-- VERIFY:END -->"


def run_pipeline(*args: str, env: dict | None = None) -> None:
    subprocess.run([sys.executable, str(CODE / "main.py"), *args], check=True, capture_output=True,
                   env={**os.environ, **(env or {})})


def run_tests() -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", str(CODE / "tests"), "-q"], capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or ["no output"])[-1]
    return r.returncode == 0, last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="also reproduce output.csv from an empty model cache")
    a = ap.parse_args()
    checks: list[tuple[str, bool, str]] = []

    ok, line = run_tests()
    checks.append(("Unit + stress tests", ok, line))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        run_pipeline("--split", "sample", "--offline", "--out", str(tmp / "sample.csv"))
        s = score(load_csv(str(tmp / "sample.csv")), load_csv(str(ROOT / "dataset" / "sample_output.csv")))
        checks.append(("Sample status accuracy is 1.000", s["status_accuracy"] == 1.0, f"{s['status_accuracy']:.3f}"))
        checks.append(("Sample step F1 at least 0.99 (regression guard)", s["step_f1"] >= 0.99, f"{s['step_f1']:.3f}"))

        loo = leave_one_out()
        checks.append(("Leave-one-out: no wrong account link", sum(v for k, v in loo.items() if "WRONG" in k) == 0,
                       f"{sum(v for k, v in loo.items() if 'WRONG' in k)} wrong"))

        run_pipeline("--offline", "--out", str(tmp / "out.csv"), "--trace", str(tmp / "trace.jsonl"))
        same = (tmp / "out.csv").read_bytes() == (ROOT / "output.csv").read_bytes()
        checks.append(("Pipeline reproduces the submitted output.csv byte for byte", same, "identical" if same else "DIFFERS"))

        n, errs = validate(ROOT / "output.csv")
        checks.append(("output.csv satisfies the contract", not errs, f"{n} rows, {len(errs)} problems"))

        traces = [json.loads(line) for line in open(tmp / "trace.jsonl", encoding="utf-8")]
        if a.fresh:
            cache = tmp / "empty_cache"
            cache.mkdir()
            run_pipeline("--out", str(tmp / "fresh.csv"), env={"AUDIT_CACHE_DIR": str(cache)})
            fresh = (tmp / "fresh.csv").read_bytes() == (ROOT / "output.csv").read_bytes()
            checks.append(("From an empty model cache, output.csv is reproduced", fresh, "identical" if fresh else "DIFFERS"))

    status = Counter(r["deletion_status"] for r in csv.DictReader(open(ROOT / "output.csv", encoding="utf-8")))
    acc_links = Counter(t["account"]["link"] for t in traces if "account" in t)
    an_links = Counter(x[2] for t in traces for x in t.get("activity_linked", []))
    uncertain = sum(1 for t in traces if t.get("uncertain"))
    write_report(s, loo, checks, status, acc_links, an_links, uncertain)

    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print(f"evaluation report updated: {REPORT.relative_to(ROOT)}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def write_report(s, loo, checks, status, acc_links, an_links, uncertain) -> None:
    fa = s["field_accuracy"]
    pc = s["per_case"]
    tp = sum(c["tp"] for c in pc.values())
    pred = sum(c["pred"] for c in pc.values())
    gold = sum(c["gold"] for c in pc.values())
    seq = sum(c["seq_ok"] for c in pc.values())
    lines = [
        START,
        f"_Measured by `python code/verify.py` on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
        "Everything between these markers is generated; do not edit by hand._",
        "",
        "**Checks**",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
        *[f"| {name} | {'PASS' if ok else '**FAIL**'} | {detail} |" for name, ok, detail in checks],
        "",
        "**Sample scores (32 labelled cases)**",
        "",
        "| Metric | Score |",
        "|---|---:|",
        f"| Case-level status accuracy | **{s['status_accuracy']:.3f}** ({sum(c['status_ok'] for c in pc.values())}/{s['cases']}) |",
        f"| Step precision | **{s['step_precision']:.3f}** ({tp}/{pred}) |",
        f"| Step recall | **{s['step_recall']:.3f}** ({tp}/{gold}) |",
        f"| Step F1 | **{s['step_f1']:.3f}** |",
        f"| Step F1 ignoring `evidence_id` | {s['step_f1_ignoring_evidence']:.3f} |",
        f"| Sequence accuracy (whole `steps_json` exactly right) | **{s['sequence_accuracy']:.3f}** ({seq}/{s['cases']}) |",
        "",
        "Field-level accuracy (steps aligned by position):",
        "",
        "| " + " | ".join(fa) + " |",
        "|" + "---:|" * len(fa),
        "| " + " | ".join(f"{v:.3f}" for v in fa.values()) + " |",
        "",
        "**Leave-one-out validation of account linking on the unlabelled test requests** (each proven",
        "account link is hidden and re-derived; the timing model is rebuilt without that person):",
        "",
        "| Outcome | Requests |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in sorted(loo.items())],
        "",
        "**Full run on the 518 requests**",
        "",
        "| Status | Requests |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in sorted(status.items())],
        "",
        "| Account link | Requests |  | Anonymized-analytics link | Requests |",
        "|---|---:|---|---|---:|",
    ]
    a_items, n_items = sorted(acc_links.items()), sorted(an_links.items())
    for i in range(max(len(a_items), len(n_items))):
        ak, av = a_items[i] if i < len(a_items) else ("", "")
        nk, nv = n_items[i] if i < len(n_items) else ("", "")
        lines.append(f"| {'`' + ak + '`' if ak else ''} | {av} |  | {'`' + nk + '`' if nk else ''} | {nv} |")
    lines += ["", f"Requests containing at least one uncertain step: **{uncertain}** "
              "(listed in [`uncertainty_report.md`](uncertainty_report.md)).", END]
    text = REPORT.read_text(encoding="utf-8")
    block = "\n".join(lines)
    if START in text and END in text:
        text = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n## Verified results\n\n" + block + "\n"
    REPORT.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
