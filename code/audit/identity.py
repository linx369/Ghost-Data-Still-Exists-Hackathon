"""Identity resolution: current snapshot + audit-log history -> every identifier a user ever had."""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .evidence import norm_phone


@dataclass
class Person:
    user_id: str
    name: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)
    # audit_ids of identity changes, with the time each one happened
    changes: list[dict] = field(default_factory=list)

    @property
    def strong_ids(self) -> set[str]:
        """Identifiers that can link a record to this person on their own."""
        return {self.user_id} | self.emails | self.phones | self.hashes

    @property
    def name_key(self) -> str:
        return "name:" + self.name.strip().lower()


class IdentityIndex:
    def __init__(self, dataset: Path):
        self.people: dict[str, Person] = {}
        with open(dataset / "user_identities.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                p = Person(r["user_id"], r["name"])
                p.emails.add(r["email"].lower())
                p.phones.add(norm_phone(r["phone"]))
                p.hashes.add(r["email_hash"].lower())
                self.people[p.user_id] = p
        with open(dataset / "user_audit_log.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                p = self.people.get(r["user_id"])
                if not p:
                    continue
                p.changes.append(r)
                for v in (r["old_value"], r["new_value"]):
                    if r["field_name"] == "email":
                        p.emails.add(v.lower())
                    elif r["field_name"] == "phone":
                        p.phones.add(norm_phone(v))
                    elif r["field_name"] == "email_hash":
                        p.hashes.add(v.lower())
        self.by_id: dict[str, set[str]] = defaultdict(set)
        for p in self.people.values():
            for i in p.strong_ids:
                self.by_id[i].add(p.user_id)

    def resolve_email(self, email: str) -> list[str]:
        return sorted(self.by_id.get(email.lower(), set()))

    def owners(self, identifiers: set[str]) -> set[str]:
        out: set[str] = set()
        for i in identifiers:
            out |= self.by_id.get(i, set())
        return out
