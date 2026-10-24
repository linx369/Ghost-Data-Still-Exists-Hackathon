"""Link each case to its primary account resource (account_service).

Evidence ladder, strongest first:
  1. direct   - an evidence record ties the account to one of the person's identifiers
  2. window   - the deletion worker touched the account in the 3h before reported_at, the
                account is not owned by anyone else, and the worker's result agrees with the
                workflow's reported_status (COMPLETE<->SUCCESS, PARTIALLY_COMPLETE<->PARTIAL,
                NOT_STARTED<->FAILED); solved jointly for all cases so one account = one case
     + signature - if several candidates remain, keep the ones whose deletion time fits the
                timing grid of this person's other worker actions (timing.py; learned from
                directly evidenced links, validated leave-one-out)
  3. unresolved tie - the evidence fits several requests equally well. The output format still
                needs an account (every deletion case verifies it), so a deterministic pick is made
                and labelled "unresolved-tie" in the trace and the evaluation report. It is NOT a
                rule: "newest request first" matched 4/4 sample batches but only 74 of 150 proven
                pairs out of sample, i.e. chance. Decision (Tom): keep the pick, report it as uncertain.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .timing import TimingModel

COMPAT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}

# When does the worker delete the account, relative to reported_at? Measured on the 238 test
# requests whose account is proven by identifiers: always 75-41 min before. A wider window pulls
# in the neighbouring batch's accounts. The hand-written sample cases sit within +-15 min; that
# window overlaps the next hour's batch, so it is only consulted when the main one is empty.
MAIN_WINDOW = (-80, -38)    # minutes relative to reported_at
NEAR_WINDOW = (-15, 15)


def _minutes(t: datetime, rep: datetime) -> float:
    return (t - rep).total_seconds() / 60


def in_window(t: datetime, rep: datetime) -> bool:
    return MAIN_WINDOW[0] <= _minutes(t, rep) <= MAIN_WINDOW[1]


def window_events(events: list[dict], rep: datetime) -> list[dict]:
    main = [a for a in events if in_window(ts(a["event_time"]), rep)]
    return main or [a for a in events if NEAR_WINDOW[0] <= _minutes(ts(a["event_time"]), rep) <= NEAR_WINDOW[1]]


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class AccountLinker:
    def __init__(self, dataset: Path, owners: dict[str, set[str]], cases: list[dict], users_of: dict[str, list[str]]):
        with open(dataset / "system_activity.csv", encoding="utf-8") as f:
            acts = list(csv.DictReader(f))
        self.deletes = [a for a in acts if a["event_type"] == "DELETE_RECORD" and a["target_system"] == "account_service"]
        worker = [a for a in acts if a["source_system"] == "deletion_worker" and a["target_system"] != "account_service"]
        self.timing = TimingModel(self.deletes, worker, owners)
        actions_of_user: dict[str, list[dict]] = defaultdict(list)  # worker actions on resources a user owns
        for a in worker:
            for u in owners.get(a["resource_id"], ()):
                actions_of_user[u].append(a)
        self.signature_used: set[str] = set()
        self.result: dict[str, tuple[str | None, str]] = {}
        evt = {a["resource_id"]: a for a in self.deletes}
        cand: dict[str, list[str]] = {}
        taken: set[str] = set()
        for q in cases:
            cid, users = q["case_id"], set(users_of.get(q["case_id"], []))
            direct = sorted(r for r, us in owners.items() if r.startswith("account_") and us & users)
            rep = ts(q["reported_at"])
            window = window_events(self.deletes, rep)
            if len(direct) == 1:
                self.result[cid] = (direct[0], "direct")
                taken.add(direct[0])
                continue
            if len(direct) > 1:  # several historical accounts: prefer the one the worker processed
                in_win = [d for d in direct if d in {a["resource_id"] for a in window}]
                pick = in_win[0] if len(in_win) == 1 else direct[0]
                self.result[cid] = (pick, "direct-multi")
                taken.add(pick)
                continue
            free = [a for a in window if not owners.get(a["resource_id"])]
            pref = [a for a in free if a["reported_result"] == COMPAT.get(q["reported_status"])] or free
            mine = [x for u in users for x in actions_of_user.get(u, [])]
            if len(pref) > 1 and mine:
                scores = {a["resource_id"]: self.timing.score(mine, a["event_time"]) for a in pref}
                best = max(scores.values())
                if best > 0:
                    pref = [a for a in pref if scores[a["resource_id"]] == best]
                    self.signature_used.add(cid)
            cand[cid] = [a["resource_id"] for a in pref]
        # joint propagation: a candidate claimed uniquely by one case is removed from others
        changed = True
        while changed:
            changed = False
            for cid, c in cand.items():
                if cid in self.result:
                    continue
                c2 = [x for x in c if x not in taken]
                cand[cid] = c2
                if len(c2) == 1:
                    self.result[cid] = (c2[0], "window+signature" if cid in self.signature_used else "window")
                    taken.add(c2[0])
                    changed = True
        # unresolved ties: arbitrary but deterministic pick within each group of identical candidates
        groups = defaultdict(list)
        qmap = {q["case_id"]: q for q in cases}
        for cid, c in cand.items():
            if cid not in self.result and c:
                groups[tuple(sorted(c))].append(cid)
        for c, cids in groups.items():
            events = sorted((x for x in c if x not in taken), key=lambda x: evt[x]["event_time"])
            order = sorted(cids, key=lambda k: (-ts(qmap[k]["requested_at"]).timestamp(), k))
            for k, acc in zip(order, events):
                self.result[k] = (acc, "unresolved-tie")
                taken.add(acc)
        # same person, other request (e.g. an earlier or later audit of the same user)
        by_user = {}
        for q in cases:
            acc, how = self.result.get(q["case_id"], (None, "none"))
            if acc and how in ("direct", "window", "window+signature"):
                for u in users_of.get(q["case_id"], []):
                    by_user.setdefault(u, acc)
        for q in cases:
            if q["case_id"] not in self.result:
                hit = next((by_user[u] for u in users_of.get(q["case_id"], []) if u in by_user), None)
                self.result[q["case_id"]] = (hit, "same-user-other-case") if hit else (None, "none")

    def activity_for(self, account_id: str | None) -> str:
        """activity_id of the worker's deletion of this account (last-resort citation)."""
        return next((a["activity_id"] for a in self.deletes if a["resource_id"] == account_id), "")

    def time_of(self, account_id: str) -> str:
        return next((a["event_time"] for a in self.deletes if a["resource_id"] == account_id), "")

    def account_for(self, case_id: str) -> tuple[str | None, str]:
        return self.result[case_id]
