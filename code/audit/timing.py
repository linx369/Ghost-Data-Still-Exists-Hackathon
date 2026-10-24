"""The deletion worker's timing signature, learned from the data at runtime.

For a request, the worker's actions (redact a ticket, delete a search entry, block a restore...)
are scheduled a fixed grid away from that request's account deletion: for each action type,
(t_action - t_account_delete) mod 60 s falls in a small, type-specific set of values
(e.g. export deletions at 51-52 s, search deletions at 21-22 s, restore blocks at 9-10 s).

The sets are learned from the dataset's own *directly evidenced* links (accounts and resources
whose owner is proven by identifiers), so no labels are used. Leave-one-out check on the test
requests: hide a directly evidenced account and recover it from the deletion time window.
Results are reported in evaluation_report.md. Used only to break ties the evidence leaves open;
every use is labelled in the trace.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .clock import parse_ts

HORIZON_S = 4 * 3600
MIN_SUPPORT = 2


def residue(t_action: str, t_account: str) -> int | None:
    d = (parse_ts(t_action) - parse_ts(t_account)).total_seconds()
    return int(d) % 60 if 0 < d < HORIZON_S else None


class TimingModel:
    def __init__(self, deletes: list[dict], worker: list[dict], owners: dict[str, set[str]]):
        base = defaultdict(list)  # user -> deletion times of accounts proven to be theirs
        for a in deletes:
            us = owners.get(a["resource_id"], set())
            if len(us) == 1:
                base[next(iter(us))].append(a["event_time"])
        seen: dict[tuple, Counter] = defaultdict(Counter)
        for a in worker:
            us = owners.get(a["resource_id"], set())
            if len(us) != 1:
                continue
            for t0 in base.get(next(iter(us)), []):
                r = residue(a["event_time"], t0)
                if r is not None:
                    seen[self.kind(a)][r] += 1
        self.allowed = {k: {r for r, n in c.items() if n >= MIN_SUPPORT} for k, c in seen.items()}

    @staticmethod
    def kind(a: dict) -> tuple[str, str]:
        return a["event_type"], a["target_system"]

    def score(self, actions: list[dict], t_account: str) -> int:
        """How many of a person's worker actions sit on the learned grid for this account deletion."""
        return sum(residue(a["event_time"], t_account) in self.allowed.get(self.kind(a), set()) for a in actions)
