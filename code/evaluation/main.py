"""Score predictions against labelled answers (sample_output.csv or our custom cases).

Metrics (all computed per case, then averaged):
  * status accuracy    - deletion_status exact match
  * step P/R/F1         - steps compared as tuples of every field except step_no
  * evidence-free F1    - same, ignoring evidence_id (isolates citation errors)
  * field accuracy      - steps aligned by position; per-field exact match
  * sequence accuracy   - the whole steps_json array is exactly right (incl. order)

Usage:
  python code/evaluation/main.py --pred out_sample.csv --gold dataset/sample_output.csv [--verbose]
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def _key(s: dict, with_evidence: bool = True) -> tuple:
    return tuple(str(s.get(f, "")) for f in FIELDS if with_evidence or f != "evidence_id")


def _prf(pred: list[dict], gold: list[dict], with_evidence: bool) -> tuple[int, int, int]:
    p = Counter(_key(s, with_evidence) for s in pred)
    g = Counter(_key(s, with_evidence) for s in gold)
    tp = sum((p & g).values())
    return tp, sum(p.values()), sum(g.values())


def load(path: str) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        return {r["case_id"]: {"status": r["deletion_status"], "steps": json.loads(r["steps_json"])}
                for r in csv.DictReader(f)}


def score(pred: dict, gold: dict) -> dict:
    n = len(gold)
    status_ok = seq_ok = 0
    tp = np_ = ng = tp2 = np2 = ng2 = 0
    field_hits = Counter()
    field_total = 0
    per_case = {}
    for cid, g in gold.items():
        p = pred.get(cid, {"status": "", "steps": []})
        s_ok = p["status"] == g["status"]
        q_ok = s_ok and [_key(x) for x in p["steps"]] == [_key(x) for x in g["steps"]] \
            and [x.get("step_no") for x in p["steps"]] == list(range(1, len(p["steps"]) + 1))
        status_ok += s_ok
        seq_ok += q_ok
        a, b, c = _prf(p["steps"], g["steps"], True)
        tp, np_, ng = tp + a, np_ + b, ng + c
        a2, b2, c2 = _prf(p["steps"], g["steps"], False)
        tp2, np2, ng2 = tp2 + a2, np2 + b2, ng2 + c2
        for i in range(max(len(p["steps"]), len(g["steps"]))):
            field_total += 1
            ps = p["steps"][i] if i < len(p["steps"]) else {}
            gs = g["steps"][i] if i < len(g["steps"]) else {}
            for f in FIELDS:
                field_hits[f] += str(ps.get(f, "∅")) == str(gs.get(f, "∅"))
        per_case[cid] = {"status_ok": s_ok, "seq_ok": q_ok, "tp": a, "pred": b, "gold": c}

    def f1(t, pp, gg):
        pr = t / pp if pp else 1.0
        rc = t / gg if gg else 1.0
        return pr, rc, (2 * pr * rc / (pr + rc) if pr + rc else 0.0)

    pr, rc, f = f1(tp, np_, ng)
    pr2, rc2, f2 = f1(tp2, np2, ng2)
    return {
        "cases": n,
        "status_accuracy": status_ok / n,
        "sequence_accuracy": seq_ok / n,
        "step_precision": pr, "step_recall": rc, "step_f1": f,
        "step_f1_ignoring_evidence": f2,
        "field_accuracy": {k: field_hits[k] / field_total for k in FIELDS} if field_total else {},
        "per_case": per_case,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    gold = load(a.gold)
    pred = load(a.pred)
    r = score(pred, {k: v for k, v in gold.items()})
    print(f"cases={r['cases']}  status_acc={r['status_accuracy']:.3f}  seq_acc={r['sequence_accuracy']:.3f}  "
          f"step P/R/F1={r['step_precision']:.3f}/{r['step_recall']:.3f}/{r['step_f1']:.3f}  "
          f"F1(no evidence)={r['step_f1_ignoring_evidence']:.3f}")
    print("field accuracy:", {k: round(v, 3) for k, v in r["field_accuracy"].items()})
    if a.verbose:
        for cid, c in r["per_case"].items():
            if not c["seq_ok"]:
                print(f"  MISS {cid}: status_ok={c['status_ok']} tp={c['tp']} pred={c['pred']} gold={c['gold']}")


if __name__ == "__main__":
    main()
