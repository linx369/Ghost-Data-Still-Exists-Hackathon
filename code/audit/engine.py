"""Case engine: for one request, reconstruct every relevant resource's state at audit_at."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .accounts import AccountLinker
from .evidence import Observation, system_of
from .identity import IdentityIndex
from .interpret import category, state
from .policy import ResourceState, load_rules, plan

# When two observations of a resource carry the same timestamp, the riskier reading wins:
# a replica that still holds the row beats a primary that lost it (replication drift).
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
        self.obs = [o for o in observations if not o.resource_id.startswith("__")]
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        for o in self.obs:
            self.by_resource[o.resource_id].append(o)
        # who does each resource belong to, according to any evidence
        self.owners: dict[str, set[str]] = defaultdict(set)
        for o in self.obs:
            self.owners[o.resource_id] |= self.idx.owners(o.identifiers)
        self.users_of = {q["case_id"]: self.idx.resolve_email(q["email"]) for q in cases}
        self.accounts = AccountLinker(dataset, self.owners, cases, self.users_of)

    def audit(self, q: dict) -> CaseResult:
        cid, audit_at = q["case_id"], q["audit_at"]
        intent = self.intents.get(cid, {"authorizes_deletion": True, "source": "default"})
        trace: dict = {"intent": intent, "users": self.users_of[cid]}
        if not intent.get("authorizes_deletion", True):
            return CaseResult(cid, "NOT_APPLICABLE", [], trace)

        people = [self.idx.people[u] for u in self.users_of[cid]]
        ids = set().union(*(p.strong_ids for p in people)) if people else {q["email"].lower()}

        def visible(o: Observation) -> bool:
            return o.file_captured_at <= audit_at and o.observed_at <= audit_at

        # 1. resources linked to the person by evidence available at audit time
        linked = {o.resource_id for o in self.obs if visible(o) and o.identifiers & ids}
        # drop resources whose identifiers point at somebody else as well (decoys)
        mine = {u for u in self.users_of[cid]}
        linked = {r for r in linked if not (self.owners[r] - mine) or (self.owners[r] & mine)}

        # 2. primary account
        acc, how = self.accounts.account_for(cid)
        trace["account"] = {"resource_id": acc, "link": how}
        account_ids = {r for r in linked if r.startswith("account_")}
        if acc:
            account_ids = {acc} | {r for r in account_ids if r == acc}
            linked = {r for r in linked if not r.startswith("account_")} | {acc}

        # 3. state of each resource at audit_at
        resources: list[ResourceState] = []
        for rid in sorted(linked):
            obs = [o for o in self.by_resource.get(rid, []) if visible(o)]
            stated = [(o, state(o)) for o in obs]
            stated = [(o, s) for o, s in stated if s]
            if not stated:
                if rid == acc:  # never observed after deletion: the worker deleted it; absence unproven
                    resources.append(ResourceState("account_service", rid, "USER_PROFILE", "ABSENT",
                                                   self._scope_evidence(audit_at), "no direct observation"))
                continue
            latest_t = max(o.observed_at for o, _ in stated)
            cur = [(o, s) for o, s in stated if o.observed_at == latest_t]
            o, s = max(cur, key=lambda x: (STATE_RANK.get(x[1], 0), x[0].evidence_id))
            cat = category(o)
            resources.append(ResourceState(o.system_id if o.system_id else system_of(rid), rid, cat, s,
                                           o.evidence_id))
        status, steps, why = plan(resources, self.rules)
        trace["reasoning"] = why
        return CaseResult(cid, status, steps, trace)

    def _scope_evidence(self, audit_at: str) -> str:
        return ""
