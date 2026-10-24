"""Leave-one-out validation of the account linker on unlabelled data.

For every request whose account is *directly* evidenced, hide that link and check whether the
time window + status match + timing signature recovers the same account. No labels are used.
Usage: python code/evaluation/loo_timing.py
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


def leave_one_out(ds: Path = ROOT / "dataset") -> Counter:
    """Counts of outcomes when each proven account link is hidden and re-derived."""
    idx = IdentityIndex(ds)
    owners: dict[str, set[str]] = defaultdict(set)
    for o in load_text_evidence(ds):
        if not o.resource_id.startswith("__"):
            owners[o.resource_id] |= idx.owners(o.identifiers)
    acts = list(csv.DictReader(open(ds / "system_activity.csv", encoding="utf-8")))
    dels = [a for a in acts if a["event_type"] == "DELETE_RECORD" and a["target_system"] == "account_service"]
    worker = [a for a in acts if a["source_system"] == "deletion_worker" and a["target_system"] != "account_service"]
    reqs = list(csv.DictReader(open(ds / "deletion_requests.csv", encoding="utf-8")))
    res = Counter()
    for q in reqs:
        users = set(idx.resolve_email(q["email"]))
        direct = [a for a in dels if owners.get(a["resource_id"], set()) & users]
        if len(direct) != 1:
            continue
        truth = direct[0]["resource_id"]
        # rebuild the model without this person's own links, so the check is out-of-sample
        held = {r: (us - users) for r, us in owners.items()}
        model = TimingModel(dels, worker, held)
        rep = parse_ts(q["reported_at"])
        win = [a for a in dels if in_window(parse_ts(a["event_time"]), rep)]
        if truth not in {a["resource_id"] for a in win}:
            res["account outside window"] += 1
            continue
        cands = [a for a in win if not (owners.get(a["resource_id"], set()) - users)
                 and a["reported_result"] == COMPAT[q["reported_status"]]]
        if len(cands) == 1:
            res["status match alone: correct" if cands[0]["resource_id"] == truth else "status match alone: WRONG"] += 1
            continue
        mine = [a for a in worker if owners.get(a["resource_id"], set()) & users]
        if not mine:
            res["no other worker actions (tie stays)"] += 1
            continue
        scores = {a["resource_id"]: model.score(mine, a["event_time"]) for a in cands}
        best = max(scores.values())
        top = [r for r, s in scores.items() if s == best]
        if best == 0 or len(top) > 1:
            res["signature: tie"] += 1
        else:
            res["signature: correct" if top[0] == truth else "signature: WRONG"] += 1
    return res


def main() -> None:
    for k, v in sorted(leave_one_out().items()):
        print(f"{k:<40} {v}")


if __name__ == "__main__":
    main()
