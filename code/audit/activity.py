"""Link already-anonymized analytics profiles to a case through the deletion worker's activity.

Such a profile carries no identifier at all, so no evidence record can tie it to a person. The
only trace is the worker's ANONYMIZE_RECORD action in the batch of requests that share a
reported_at. Two observations from the labelled samples make the assignment tractable:

  * every missed anonymized profile belonged to a case that is otherwise COMPLETE (if the
    anonymization had failed, the profile would still carry identifiers and be linked directly);
  * the worker processes a batch newest request first (4/4 sample batches).

So, per batch: otherwise-COMPLETE cases without an analytics record, newest request first, are
paired with the batch's unowned ANONYMIZE actions in time order. Samples: 3 right, 2 wrong ids,
4 still missed (F1 0.973 -> 0.976). Every assignment is labelled in the trace as heuristic.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from .accounts import ts


class ActivityLinker:
    def __init__(self, dataset: Path, owners: dict[str, set[str]]):
        with open(dataset / "system_activity.csv", encoding="utf-8") as f:
            self.events = [a for a in csv.DictReader(f)
                           if a["source_system"] == "deletion_worker" and a["event_type"] == "ANONYMIZE_RECORD"
                           and not owners.get(a["resource_id"])]
        self.assigned: dict[str, tuple[str, str]] = {}

    def assign(self, cases: list[dict], needs_profile: dict[str, bool]) -> None:
        """needs_profile[case_id]: the case is otherwise COMPLETE and has no analytics record."""
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
            for q, a in zip(needy, evs):
                self.assigned[q["case_id"]] = (a["resource_id"], a["activity_id"])

    def resources_for(self, case_id: str) -> list[tuple[str, str]]:
        return [self.assigned[case_id]] if case_id in self.assigned else []
