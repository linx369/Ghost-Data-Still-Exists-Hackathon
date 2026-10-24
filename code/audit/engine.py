"""Case engine: for one request, reconstruct every relevant resource's state at audit_at.

Time rules
  * known:    an observation can inform the answer if the fact it records happened at or
              before audit_at (log lines carry their own timestamps).
  * citable:  an evidence_id can be cited only if the file was captured at or before audit_at.
State is taken from citable observations when any exist, otherwise from known ones.
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .accounts import AccountLinker
from .activity import ActivityLinker
from .evidence import Observation, system_of
from .identity import IdentityIndex
from .interpret import category, state
from .policy import ResourceState, load_rules, plan

# Same timestamp, conflicting readings: the riskier one wins (a replica that still holds
# the row beats a primary that lost it = replication drift).
STATE_RANK = {"PRESENT": 5, "ACTIVE": 5, "RETAINED": 4, "CLEAN": 3, "STOPPED": 3, "ABSENT": 1}


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list[dict]
    trace: dict = field(default_factory=dict)


class Auditor:
    def __init__(self, dataset: Path, observations: list[Observation], cases: list[dict], intents: dict[str, dict]):
        self.dataset = dataset
        self.idx = IdentityIndex(dataset)
        self.rules = load_rules(dataset)
        self.intents = intents
        self.scopes = [o for o in observations if o.resource_id == "__scope__" and o.system_id == "account_service"]
        self.obs = [o for o in observations if not o.resource_id.startswith("__")]
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        for o in self.obs:
            self.by_resource[o.resource_id].append(o)
        self.owners: dict[str, set[str]] = defaultdict(set)
        for o in self.obs:
            self.owners[o.resource_id] |= self.idx.owners(o.identifiers)
        self.users_of = {q["case_id"]: self.idx.resolve_email(q["email"]) for q in cases}
        self.accounts = AccountLinker(dataset, self.owners, cases, self.users_of)
        self.activity = ActivityLinker(dataset, self.owners, cases,
                                       {c: a for c, (a, _) in self.accounts.result.items()},
                                       self._linked_systems(cases))

    # ------------------------------------------------------------------ helpers
    def _ids(self, cid: str, email: str) -> set[str]:
        people = [self.idx.people[u] for u in self.users_of[cid]]
        return set().union(*(p.strong_ids for p in people)) if people else {email.lower()}

    def _linked(self, q: dict) -> set[str]:
        ids = self._ids(q["case_id"], q["email"])
        return {o.resource_id for o in self.obs if o.observed_at <= q["audit_at"] and o.identifiers & ids}

    def _linked_systems(self, cases: list[dict]) -> dict[str, set[str]]:
        return {q["case_id"]: {system_of(r) for r in self._linked(q)} for q in cases}

    def _account_scope_evidence(self, audit_at: str, prefer: list[str]) -> str:
        """Evidence proving the account is gone when no record mentions it: a snapshot
        whose scope includes account_service and holds no row for it."""
        cands = [s for s in self.scopes if s.file_captured_at <= audit_at and s.observed_at <= audit_at]
        for p in prefer:
            if any(s.evidence_id == p for s in cands):
                return p
        if not cands:
            return ""
        return max(cands, key=lambda s: (s.observed_at, s.evidence_id)).evidence_id

    # -------------------------------------------------------------------- audit
    def audit(self, q: dict) -> CaseResult:
        cid, audit_at = q["case_id"], q["audit_at"]
        intent = self.intents.get(cid, {"authorizes_deletion": True, "source": "default"})
        trace: dict = {"intent": intent, "users": self.users_of[cid]}
        if not intent.get("authorizes_deletion", True):
            trace["reasoning"] = ["request does not authorize deletion -> NOT_APPLICABLE"]
            return CaseResult(cid, "NOT_APPLICABLE", [], trace)

        linked = self._linked(q)
        acc, how = self.accounts.account_for(cid)
        trace["account"] = {"resource_id": acc, "link": how}
        if acc:
            linked = {r for r in linked if not r.startswith("account_")} | {acc}
        extra = self.activity.resources_for(cid) if os.environ.get("ACTIVITY_LINK", "0") == "1" else []
        trace["activity_linked"] = extra
        linked |= {r for r, _ in extra}

        resources: list[ResourceState] = []
        for rid in sorted(linked):
            known = [o for o in self.by_resource.get(rid, []) if o.observed_at <= audit_at]
            citable = [o for o in known if o.file_captured_at <= audit_at]
            pool = [(o, state(o)) for o in (citable or known)]
            pool = [(o, s) for o, s in pool if s]
            if not pool:
                if rid == acc:
                    resources.append(ResourceState("account_service", rid, "USER_PROFILE", "ABSENT", "",
                                                   "no observation; absence from scoped snapshot"))
                continue
            latest_t = max(o.observed_at for o, _ in pool)
            cur = [(o, s) for o, s in pool if o.observed_at == latest_t]
            o, s = max(cur, key=lambda x: (STATE_RANK.get(x[1], 0), x[0].evidence_id))
            resources.append(ResourceState(o.system_id or system_of(rid), rid, category(o), s,
                                           o.evidence_id if o.file_captured_at <= audit_at else ""))
        # evidence for an account that no record mentions: reuse the case's main evidence file
        main = [r.evidence_id for r in resources if r.evidence_id]
        prefer = sorted(set(main), key=lambda e: -main.count(e))
        for r in resources:
            if r.resource_id == acc and not r.evidence_id:
                r.evidence_id = self._account_scope_evidence(audit_at, prefer)
        status, steps, why = plan(resources, self.rules)
        trace["reasoning"] = why
        return CaseResult(cid, status, steps, trace)
