"""The audit pipeline: for every request, five explicit stages.

  1. link         which records belong to this person (identifiers, catalog links, account linker)
  2. reconstruct  each record's state at audit_at, and the evidence that proves it
  3. decide       deletion policy -> status + ordered steps            (policy.py)
  4. batch        records with no identifier at all (already-anonymized analytics) are linked from the
                  worker's batch activity, using the stage-3 drafts of the whole batch (activity.py)
  5. verify       every answer is checked before it is written: the output contract (contract.py)
                  and grounding (each cited evidence item really mentions the record it is cited for)

Time rules
  * known:    an observation can inform the answer if the fact it records happened at or before
              audit_at and after the request (an older sighting predates the deletion run).
  * citable:  an evidence id can be cited only if it was captured at or before audit_at.
  * a finding must rest on citable evidence; a fact known only from a later file may support a
    retained-record check but never a finding.
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .accounts import AccountLinker
from .activity import ActivityLinker
from .contract import check_case
from .evidence import Observation, system_of
from .identity import IdentityIndex
from .interpret import category, state
from .policy import ResourceState, load_rules, plan, treatment

# Same timestamp, conflicting readings: the riskier one wins (a replica that still holds
# the row beats a primary that lost it = replication drift).
STATE_RANK = {"PRESENT": 5, "ACTIVE": 5, "RETAINED": 4, "CLEAN": 3, "STOPPED": 3, "ABSENT": 1}
FINDING_STATES = {"DELETE": {"PRESENT", "CLEAN"}, "REDACT": {"PRESENT"}, "ANONYMIZE": {"PRESENT"},
                  "DISABLE": {"ACTIVE"}, "BLOCK": {"ACTIVE"}}


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list[dict]
    trace: dict = field(default_factory=dict)


class Auditor:
    def __init__(self, dataset: Path, observations: list[Observation], cases: list[dict], intents: dict[str, dict],
                 evidence_time: dict[str, str] | None = None):
        self.cases = cases
        self.idx = IdentityIndex(dataset)
        self.rules = load_rules(dataset)
        self.intents = intents
        self.evidence_time = evidence_time
        self.scopes = [o for o in observations if o.resource_id == "__scope__"]
        self.obs = [o for o in observations if not o.resource_id.startswith("__")]
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        self.mentions: dict[str, set[str]] = defaultdict(set)  # evidence id -> resources it mentions
        for o in self.obs:
            self.by_resource[o.resource_id].append(o)
            self.mentions[o.evidence_id].add(o.resource_id)
        self.owners: dict[str, set[str]] = defaultdict(set)
        for o in self.obs:
            self.owners[o.resource_id] |= self.idx.owners(o.identifiers)
        self.users_of = {q["case_id"]: self.idx.resolve_email(q["email"]) for q in cases}
        self.accounts = AccountLinker(dataset, self.owners, cases, self.users_of)
        self.activity = ActivityLinker(dataset, self.owners)
        self.activity_deletes = {a["activity_id"]: a["resource_id"] for a in self.accounts.deletes}
        # the workflow's own intake record: accounts whose request it classified as non-deletion
        self.intake_nondeletion = {a["resource_id"]: a["activity_id"] for a in self.accounts.intake}
        self._results: dict[str, CaseResult] | None = None

    # ===================================================================== pipeline
    def run(self) -> dict[str, CaseResult]:
        """Stages 1-3 for every request (drafts), stage 4 for the batch, stage 5 for every answer."""
        drafts = {q["case_id"]: self._decide(q, extra=[]) for q in self.cases}
        if os.environ.get("ACTIVITY_LINK", "1") == "1":
            needs = {c: r.status == "COMPLETE" and not any(s["system_id"] == "analytics_platform" for s in r.steps)
                     for c, r in drafts.items()}
            times = {c: self.accounts.time_of(a) for c, (a, _) in self.accounts.result.items() if a}
            self.activity.assign(self.cases, needs, times)
        final = {}
        for q in self.cases:
            extra = self.activity.resources_for(q["case_id"])
            r = self._decide(q, extra) if extra else drafts[q["case_id"]]
            r.trace["verification"] = self._verify(q, r)
            final[q["case_id"]] = r
        self._results = final
        return final

    def audit(self, q: dict) -> CaseResult:
        if self._results is None:
            self.run()
        return self._results[q["case_id"]]

    # ============================================================ stage 1: link
    def _ids(self, cid: str, email: str) -> set[str]:
        people = [self.idx.people[u] for u in self.users_of[cid]]
        return set().union(*(p.strong_ids for p in people)) if people else {email.lower()}

    def _link(self, q: dict, notes: list) -> set[str]:
        ids = self._ids(q["case_id"], q["email"])
        by_link, by_content = set(), set()
        for o in self.obs:
            if o.observed_at <= q["audit_at"] and o.identifiers & ids:
                (by_link if o.attrs.get("_link_only") else by_content).add(o.resource_id)
        # A catalog link can conflict with what the record itself displays. Rule (Tom's decision):
        # drop the link only if the record shows another *known* person; if it shows nobody we know,
        # keep the link (it is the only grounded owner) and record the conflict in the trace.
        for rid in by_link - by_content:
            shown = {i for o in self.by_resource[rid] if not o.attrs.get("_link_only")
                     for i in o.identifiers if "@" in i}
            if not shown or shown & ids:
                continue
            other = self.idx.owners(shown)
            if other:
                by_link.discard(rid)
            notes.append(f"{rid}: catalog link {'dropped, record belongs to ' + str(sorted(other)) if other else 'kept'}; "
                         f"record displays {sorted(shown)}")
        return by_content | by_link

    # ===================================================== stage 2: reconstruct
    def _reconstruct(self, rid: str, q: dict, is_account: bool) -> tuple[ResourceState | None, dict]:
        """State of one record at audit_at, the evidence for it, and what was considered."""
        audit_at = q["audit_at"]
        known = [o for o in self.by_resource.get(rid, []) if q["requested_at"] <= o.observed_at <= audit_at]
        citable = [o for o in known if o.file_captured_at <= audit_at]
        pool = [(o, s) for o, s in ((o, state(o)) for o in (citable or known)) if s]
        detail = {"resource": rid, "observations_known": len(known), "observations_citable": len(citable)}
        if not pool:
            if not is_account:
                detail["decision"] = "no state observed after the request: not reported"
                return None, detail
            detail["decision"] = "account never observed after the request: absent (proved by a scoped snapshot)"
            return ResourceState("account_service", rid, "USER_PROFILE", "ABSENT", ""), detail
        latest_t = max(o.observed_at for o, _ in pool)
        o, s = max(((o, s) for o, s in pool if o.observed_at == latest_t),
                   key=lambda x: (STATE_RANK.get(x[1], 0), x[0].evidence_id))
        cat, sysid = category(o), o.system_id or system_of(rid)
        cites = o.file_captured_at <= audit_at
        detail.update({"latest": latest_t, "evidence": o.evidence_id, "source": o.source, "state": s, "category": cat})
        if not cites and s in FINDING_STATES.get(treatment(cat, self.rules), set()):
            # only a later file shows the problem: it may not support a finding
            if not is_account:
                detail["decision"] = "violation seen only in a file captured after audit_at: not reported"
                return None, detail
            detail["decision"] = "presence seen only in a file captured after audit_at; judged on admissible evidence"
            return ResourceState("account_service", rid, "USER_PROFILE", "ABSENT", ""), detail
        detail["decision"] = f"state {s} from {o.evidence_id}" + ("" if cites else " (not citable: captured after audit_at)")
        return ResourceState(sysid, rid, cat, s, o.evidence_id if cites else ""), detail

    def _account_scope_evidence(self, audit_at: str, prefer: list[str]) -> str:
        """Evidence proving the account is gone when no record mentions it: a snapshot whose scope
        includes account_service and holds no row for it (preferring the case's own evidence)."""
        cands = [s for s in self.scopes if s.system_id == "account_service"
                 and s.file_captured_at <= audit_at and s.observed_at <= audit_at]
        for p in prefer:
            if any(s.evidence_id == p for s in cands):
                return p
        return max(cands, key=lambda s: (s.observed_at, s.evidence_id)).evidence_id if cands else ""

    # ========================================================== stages 1-3 per request
    def _decide(self, q: dict, extra: list[tuple[str, str]]) -> CaseResult:
        cid = q["case_id"]
        intent = self.intents.get(cid, {"authorizes_deletion": True, "source": "default"})
        trace: dict = {"intent": intent, "users": self.users_of[cid]}
        if not intent.get("authorizes_deletion", True):
            trace["reasoning"] = ["request does not authorize deletion -> NOT_APPLICABLE"]
            trace["uncertain"] = []
            return CaseResult(cid, "NOT_APPLICABLE", [], trace)

        notes: list[str] = []
        linked = self._link(q, notes)
        acc, how = self.accounts.account_for(cid)
        trace["account"] = {"resource_id": acc, "link": how}
        if acc:
            linked = {r for r in linked if not r.startswith("account_")} | {acc}
        trace["activity_linked"] = [[r, a, self.activity.how.get(cid, "")] for r, a in extra]
        linked |= {r for r, _ in extra}

        resources, details = [], []
        for rid in sorted(linked):
            res, detail = self._reconstruct(rid, q, rid == acc)
            details.append(detail)
            if res:
                resources.append(res)
        main = [r.evidence_id for r in resources if r.evidence_id]
        prefer = sorted(set(main), key=lambda e: (-main.count(e), e))  # stable: ties by id
        for r in resources:
            if r.resource_id == acc and not r.evidence_id:
                # grounded fallbacks only: a snapshot covering accounts, else the worker's deletion row
                r.evidence_id = (self._account_scope_evidence(q["audit_at"], prefer)
                                 or self.accounts.activity_for(acc))
        status, steps, why = plan(resources, self.rules)
        trace["resources"] = details
        trace["reasoning"] = notes + why
        trace["uncertain"] = sorted(
            ([acc] if how == "unresolved-tie" and any(s["resource_id"] == acc for s in steps) else [])
            + [r for r, _ in extra if self.activity.how.get(cid) == "unresolved-tie"
               and any(s["resource_id"] == r for s in steps)])
        return CaseResult(cid, status, steps, trace)

    # ============================================================ stage 5: verify
    def _grounded(self, s: dict) -> bool:
        """Does the cited evidence actually mention the record (or, for an absence check, cover its system)?"""
        ev, rid = s["evidence_id"], s["resource_id"]
        if not ev:
            return True
        if ev in self.activity_deletes:
            return self.activity_deletes[ev] == rid
        if rid in self.mentions.get(ev, ()):
            return True
        return s["operation"] == "VERIFY_RECORD_ABSENT" and any(
            sc.evidence_id == ev and sc.system_id == s["system_id"] for sc in self.scopes)

    def _intake_disagrees(self, q: dict, r: CaseResult) -> str | None:
        """Independent cross-check of the model's intent against the workflow's intake record."""
        mine = set(self.users_of[r.case_id])
        hits = [aid for acc, aid in self.intake_nondeletion.items() if self.owners.get(acc, set()) & mine]
        model_says_na = r.status == "NOT_APPLICABLE"
        if hits and not model_says_na:
            return f"{r.case_id}: intake record {hits[0]} says non-deletion, the model says deletion"
        if model_says_na and not hits:
            return f"{r.case_id}: the model says non-deletion, no intake record confirms it"
        return None

    def _verify(self, q: dict, r: CaseResult) -> list[str]:
        errs = [f"{r.case_id}: step {s['step_no']} cites {s['evidence_id']}, which does not mention {s['resource_id']}"
                for s in r.steps if not self._grounded(s)]
        disagreement = self._intake_disagrees(q, r)
        if disagreement:
            errs.append(disagreement)
        if self.evidence_time is not None:
            errs += check_case(r.case_id, r.status, r.steps, q["audit_at"], self.evidence_time)
        return errs
