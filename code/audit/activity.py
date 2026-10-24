"""Link already-anonymized analytics profiles to a case through the deletion worker's activity.

Such a profile carries no identifier at all, so no evidence record can tie it to a person. The
only trace is the worker's ANONYMIZE_RECORD action in the batch of requests that share a
reported_at. Every missed anonymized profile in the samples belonged to a case that is otherwise
COMPLETE (a failed anonymization would leave identifiers and be linked directly).

So, per batch, otherwise-COMPLETE cases without an analytics record are paired with the batch's
unowned ANONYMIZE actions:
  1. "timing-grid": the action sits on the worker's timing grid relative to that case's account
     deletion ((delta mod 30 s) in {9..11, 20..22}; timing.py) and the pairing is unique;
  2. "unresolved-tie": what remains is paired deterministically (newest request first). Out of
     sample this order is chance (74 vs 76 proven pairs), so these links are reported as uncertain.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from .accounts import ts
from .timing import HORIZON_S, _ts

COARSE_GRID = {9, 10, 11, 20, 21, 22}


class ActivityLinker:
    def __init__(self, dataset: Path, owners: dict[str, set[str]]):
        with open(dataset / "system_activity.csv", encoding="utf-8") as f:
            self.events = [a for a in csv.DictReader(f)
                           if a["source_system"] == "deletion_worker" and a["event_type"] == "ANONYMIZE_RECORD"
                           and not owners.get(a["resource_id"])]
        self.assigned: dict[str, tuple[str, str]] = {}
        self.how: dict[str, str] = {}

    def assign(self, cases: list[dict], needs_profile: dict[str, bool], account_time: dict[str, str]) -> None:
        """needs_profile[case_id]: the case is otherwise COMPLETE and has no analytics record.
        account_time[case_id]: when the worker deleted that case's account (if known)."""

        def on_grid(a, cid):
            t0 = account_time.get(cid)
            if not t0:
                return False
            d = (_ts(a["event_time"]) - _ts(t0)).total_seconds()
            return 0 < d < HORIZON_S and int(d) % 30 in COARSE_GRID

        batches = defaultdict(list)
        for q in cases:
            batches[q["reported_at"]].append(q)
        for rep_s, qs in batches.items():
            rep = ts(rep_s)
            evs = sorted((a for a in self.events
                          if rep - timedelta(hours=3) <= ts(a["event_time"]) <= rep + timedelta(minutes=15)),
                         key=lambda a: (a["event_time"], a["activity_id"]))
            needy = sorted((q for q in qs if needs_profile.get(q["case_id"])),
                           key=lambda q: (-ts(q["requested_at"]).timestamp(), int(q["case_id"].split("_")[1])))
            # 1) timing-grid pairs that are unambiguous, repeatedly
            changed = True
            while changed:
                changed = False
                for q in list(needy):
                    fits = [a for a in evs if on_grid(a, q["case_id"])]
                    if len(fits) == 1 and sum(on_grid(fits[0], o["case_id"]) for o in needy) == 1:
                        self.assigned[q["case_id"]] = (fits[0]["resource_id"], fits[0]["activity_id"])
                        self.how[q["case_id"]] = "timing-grid"
                        needy.remove(q)
                        evs.remove(fits[0])
                        changed = True
            # 2) the rest cannot be told apart: deterministic pick, reported as uncertain
            for q, a in zip(needy, evs):
                self.assigned[q["case_id"]] = (a["resource_id"], a["activity_id"])
                self.how[q["case_id"]] = "unresolved-tie"

    def resources_for(self, case_id: str) -> list[tuple[str, str]]:
        return [self.assigned[case_id]] if case_id in self.assigned else []
