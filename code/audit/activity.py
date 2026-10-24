"""Link already-anonymized analytics profiles to a case through the deletion worker's activity.

Such a profile carries no identifier at all, so no evidence record can tie it to a person. The
only trace is the worker's ANONYMIZE_RECORD action in the batch of requests that share a
reported_at. Every missed anonymized profile in the samples belonged to a case that is otherwise
COMPLETE (a failed anonymization would leave identifiers and be linked directly).

The worker's successful clean-up actions for a batch run in fixed slots before reported_at
(measured on proven links: placeholders/legal holds ~30 min, support redactions ~25, flow shut-offs
~15, restore blocks ~10 min before). A batch's anonymizations therefore lie in ACTION_WINDOW; a wider
window (the original 3 h) lets a batch take an earlier batch's anonymization.

So, per batch, otherwise-COMPLETE cases without an analytics record are paired with the batch's
unowned ANONYMIZE actions in ACTION_WINDOW (NEAR_WINDOW only if that slot is empty, exactly as
for accounts):
  1. "timing-grid": the action sits on the worker's timing grid relative to that case's account
     deletion ((delta mod 30 s) in {9..11, 20..22}; timing.py) and the pairing is unique;
  2. "unresolved-tie": what remains is paired deterministically (newest request first). Out of
     sample this order is chance (74 vs 76 proven pairs), so these links are reported as uncertain.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .clock import minutes_between, parse_ts
from .accounts import NEAR_WINDOW
from .timing import HORIZON_S

COARSE_GRID = {9, 10, 11, 20, 21, 22}
ACTION_WINDOW = (-35, -20)  # minutes relative to reported_at


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
            d = (parse_ts(a["event_time"]) - parse_ts(t0)).total_seconds()
            return 0 < d < HORIZON_S and int(d) % 30 in COARSE_GRID

        batches = defaultdict(list)
        for q in cases:
            batches[q["reported_at"]].append(q)
        for rep_s, qs in batches.items():
            evs = sorted(self._batch_events(rep_s), key=lambda a: (a["event_time"], a["activity_id"]))
            needy = sorted((q for q in qs if needs_profile.get(q["case_id"])),
                           key=lambda q: (-parse_ts(q["requested_at"]).timestamp(), int(q["case_id"].split("_")[1])))
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

    def _batch_events(self, rep_s: str) -> list[dict]:
        """The batch's anonymizations: the measured slot first; the near window (same rule as for
        accounts, for hand-written cases processed at reported_at) only if the slot is empty."""
        def within(w):
            return [a for a in self.events if w[0] <= minutes_between(a["event_time"], rep_s) <= w[1]]
        return within(ACTION_WINDOW) or within(NEAR_WINDOW)

    def resources_for(self, case_id: str) -> list[tuple[str, str]]:
        return [self.assigned[case_id]] if case_id in self.assigned else []
