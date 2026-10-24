"""Link resources that carry no identifiers at all (e.g. an analytics profile that was
already anonymized) to a case through the deletion worker's own activity.

The worker processes all requests that share a reported_at in one batch, in a fixed order.
We reconstruct that order from each case's account-deletion event and hand out the batch's
unowned policy actions (anonymize/redact/disable/block/delete) per system in the same order,
only to cases that have no evidence-linked resource in that system. This is a heuristic and
every assignment is labelled as such in the trace.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from .accounts import ts

POLICY_EVENTS = {"ANONYMIZE_RECORD", "REDACT_RECORD", "DELETE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE"}


class ActivityLinker:
    def __init__(self, dataset: Path, owners: dict[str, set[str]], cases: list[dict],
                 account_of: dict[str, str | None], linked_systems: dict[str, set[str]]):
        with open(dataset / "system_activity.csv", encoding="utf-8") as f:
            acts = list(csv.DictReader(f))
        acc_time = {a["resource_id"]: a["event_time"] for a in acts
                    if a["event_type"] == "DELETE_RECORD" and a["target_system"] == "account_service"}
        self.assigned: dict[str, list[tuple[str, str]]] = defaultdict(list)
        batches = defaultdict(list)
        for q in cases:
            batches[q["reported_at"]].append(q)
        for rep_s, qs in batches.items():
            rep = ts(rep_s)
            events = [a for a in acts if a["source_system"] == "deletion_worker" and a["event_type"] in POLICY_EVENTS
                      and a["target_system"] != "account_service" and not owners.get(a["resource_id"])
                      and rep - timedelta(hours=3) <= ts(a["event_time"]) <= rep + timedelta(minutes=15)]
            if not events:
                continue

            def order_key(q):
                acc = account_of.get(q["case_id"])
                t = acc_time.get(acc) if acc else None
                return (0, t, q["case_id"]) if t else (1, -ts(q["requested_at"]).timestamp(), q["case_id"])

            ordered = sorted(qs, key=order_key)
            by_sys = defaultdict(list)
            for a in sorted(events, key=lambda a: a["event_time"]):
                by_sys[a["target_system"]].append(a)
            for sysid, evs in by_sys.items():
                needy = [q for q in ordered if sysid not in linked_systems.get(q["case_id"], set())]
                seen = set()
                evs = [a for a in evs if not (a["resource_id"] in seen or seen.add(a["resource_id"]))]
                for q, a in zip(needy, evs):
                    self.assigned[q["case_id"]].append((a["resource_id"], a["activity_id"]))

    def resources_for(self, case_id: str) -> list[tuple[str, str]]:
        return self.assigned.get(case_id, [])
