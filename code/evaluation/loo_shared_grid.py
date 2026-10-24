"""Leave-one-out validation of the shared timing grid (used for action types with no learned grid).

Tombstone redactions are never seen next to a directly evidenced account, so the timing model has
no grid of its own for them and scores them on the grid shared by every learned action type. This
script tests that fallback on links that ARE proven, by recreating the tombstone situation: for each
directly evidenced request and each action type T the person has, the timing model is rebuilt
without that person and without any action of type T (so T is unlearned and falls back to the
shared grid); then only the person's T-actions choose among the window candidates whose worker
result matches the reported status. No labels are used.

Usage: python code/evaluation/loo_shared_grid.py
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))
from audit.accounts import COMPAT, in_window  # noqa: E402
from audit.clock import parse_ts  # noqa: E402
from audit.evidence import load_text_evidence  # noqa: E402
from audit.identity import IdentityIndex  # noqa: E402
from audit.timing import TimingModel  # noqa: E402


def leave_type_out(ds: Path = ROOT / "dataset") -> Counter:
    idx = IdentityIndex(ds)
    owners: dict[str, set[str]] = defaultdict(set)
    for o in load_text_evidence(ds):
        if not o.resource_id.startswith("__"):
            owners[o.resource_id] |= idx.owners(o.identifiers)
    acts = list(csv.DictReader(open(ds / "system_activity.csv", encoding="utf-8")))
    dels = [a for a in acts if a["event_type"] == "DELETE_RECORD" and a["target_system"] == "account_service"]
    worker = [a for a in acts if a["source_system"] == "deletion_worker" and a["target_system"] != "account_service"]
    res = Counter()
    for q in csv.DictReader(open(ds / "deletion_requests.csv", encoding="utf-8")):
        users = set(idx.resolve_email(q["email"]))
        direct = [a for a in dels if owners.get(a["resource_id"], set()) & users]
        if len(direct) != 1:
            continue
        truth = direct[0]["resource_id"]
        rep = parse_ts(q["reported_at"])
        cands = [a for a in dels if in_window(parse_ts(a["event_time"]), rep)
                 and a["reported_result"] == COMPAT.get(q["reported_status"])
                 and (not owners.get(a["resource_id"]) or a["resource_id"] == truth)]
        if len(cands) < 2 or truth not in {a["resource_id"] for a in cands}:
            continue
        mine = [a for a in worker if owners.get(a["resource_id"], set()) & users]
        others = {r: us for r, us in owners.items() if not us & users}  # forget this person
        for kind in sorted({TimingModel.kind(a) for a in mine}):
            model = TimingModel(dels, [a for a in worker if TimingModel.kind(a) != kind], others)
            acts_k = [a for a in mine if TimingModel.kind(a) == kind]
            fit = [a["resource_id"] for a in cands if model.score(acts_k, a["event_time"]) == len(acts_k)]
            res["correct (unique fit)" if fit == [truth] else
                "WRONG (true account rejected)" if truth not in fit else "tie stays"] += 1
    return res


def main() -> None:
    for k, v in sorted(leave_type_out().items()):
        print(f"{k:32} {v}")


if __name__ == "__main__":
    main()
