"""Evidence ingestion: turn every text evidence file into normalized observations.

An Observation is one statement of the form "at time T, evidence E showed resource R
in system S with these attributes". Nothing here decides anything; it only describes.
Screenshots and PDFs are handled by `multimodal.py`, which produces the same shape.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
RESOURCE_RE = re.compile(
    r"\b(?:account|search|support|analytics|export|data_flow|backup|audit_record|tombstone|cache|email_delivery|marketing|telemetry)_\d+\b"
)
TRACE_RE = re.compile(r"\b(?:tr|rb)-[\w-]+")
KV_RE = re.compile(r'([A-Za-z_][\w]*)=("(?:[^"\\]|\\.)*"|[^\s|]+)')
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")

# resource-id prefix -> system id (verified against every step in sample_output.csv)
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "support": "support_platform",
    "analytics": "analytics_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "marketing": "marketing_platform",
    "telemetry": "product_telemetry",
}


def system_of(resource_id: str) -> str:
    prefix = resource_id.rsplit("_", 1)[0]
    return PREFIX_SYSTEM.get(prefix, prefix)


@dataclass
class Observation:
    evidence_id: str          # file_id / activity_id / audit_id that can be cited
    file_captured_at: str     # manifest capture time (citation cutoff)
    observed_at: str          # time the statement refers to
    system_id: str
    resource_id: str
    attrs: dict = field(default_factory=dict)   # raw key/values describing the record
    source: str = ""          # parser + location, for traces
    layer: str = ""           # e.g. primary / read_replica / failover_replica

    @property
    def identifiers(self) -> set[str]:
        """Every identifier-looking value in the record, lowercased."""
        out: set[str] = set()
        for k, v in self.attrs.items():
            if k.startswith("_") or k == "integrity_seal":
                continue
            for s in map(str, _iter_values(v)):
                out.update(m.lower() for m in EMAIL_RE.findall(s))
                out.update(USER_RE.findall(s))
                if "hash" in k:
                    out.update(HASH_RE.findall(s.lower()))
        for k in ("phone", "requester_phone", "owner_phone", "customer_phone"):
            if self.attrs.get(k):
                out.add(norm_phone(self.attrs[k]))
        for k in ("full_name", "name", "requester_name", "display_name", "customer_name"):
            if self.attrs.get(k):
                out.add("name:" + str(self.attrs[k]).strip().lower())
        return out


def norm_phone(p) -> str:
    return "phone:" + re.sub(r"\D", "", str(p))


def _iter_values(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _iter_values(v)
    elif isinstance(x, list):
        for v in x:
            yield from _iter_values(v)
    elif x is not None:
        yield x


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and k in ("document", "profile"):
            out.update(_flatten(v))
            out[k + "_present"] = True
        elif v is None and k == "profile":
            out["profile_present"] = False
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- SQL
def parse_sql(path: Path, fid: str, captured: str) -> list[Observation]:
    con = sqlite3.connect(":memory:")
    con.executescript(path.read_text(encoding="utf-8"))
    con.row_factory = sqlite3.Row
    obs: list[Observation] = []
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    scope = {}
    if "snapshot_scope" in tables:
        scope = {r["origin_system"]: r["captured_at"] for r in con.execute("select * from snapshot_scope")}
    for t in tables:
        if t == "snapshot_scope":
            continue
        layer = _layer_from_table(t)
        for row in con.execute(f'select * from "{t}"'):
            d = dict(row)
            if "record_json" in d:  # replica tables wrap the record as JSON
                rec = json.loads(d.pop("record_json"))
                d = {**d, **_flatten(rec)}
            rid = d.get("resource_id")
            if not rid:
                continue
            sysid = system_of(rid)
            obs.append(Observation(
                evidence_id=fid, file_captured_at=captured,
                observed_at=d.get("captured_at") or scope.get(sysid) or captured,
                system_id=sysid, resource_id=rid, attrs={**d, "_table": t},
                source=f"sql:{t}", layer=layer))
    # Tables that were captured but hold no row for a resource still tell us the
    # snapshot scope; record it so absence can be proven for systems in scope.
    for sysid, at in scope.items():
        obs.append(Observation(fid, captured, at, sysid, "__scope__", {"scope": True}, "sql:scope", "primary"))
    for t in tables:
        if t == "snapshot_scope":
            continue
        obs.append(Observation(fid, captured, captured, "__table__", "__scope__",
                               {"table": t, "layer": _layer_from_table(t)}, "sql:table", _layer_from_table(t)))
    return obs


def _layer_from_table(t: str) -> str:
    for key in ("failover_replica", "read_replica", "reporting_replica", "replica_eu_west",
                "replica_ap_south", "primary"):
        if key in t:
            return key
    return "primary"


# -------------------------------------------------------------------------- JSON
def parse_json(path: Path, fid: str, captured: str) -> list[Observation]:
    data = json.loads(path.read_text(encoding="utf-8"))
    obs: list[Observation] = []

    def emit(rec: dict, when: str, ctx: str, store: str | None = None):
        rid = rec.get("resource_id")
        if not rid:
            return
        attrs = _flatten(rec)
        if store:
            attrs["_data_store"] = store
        obs.append(Observation(fid, captured, rec.get("observed_at") or rec.get("recorded_at") or when,
                               rec.get("system") or system_of(rid), rid, attrs, f"json:{ctx}", "primary"))

    top_when = data.get("captured_at") or data.get("exported_at") or captured
    if "snapshots" in data:
        for snap in data["snapshots"]:
            when = snap.get("captured_at") or top_when
            for rec in snap.get("records", []):
                emit(rec, when, f"{snap.get('data_store')}.{snap.get('collection')}", snap.get("data_store"))
            # an empty / partial collection proves absence for that store at that time
            obs.append(Observation(fid, captured, when, snap.get("data_store", ""), "__scope__",
                                   {"collection": snap.get("collection"), "scope": True}, "json:scope", "primary"))
    if "service_exports" in data:
        for sysid, groups in data["service_exports"].items():
            for gname, recs in groups.items():
                for rec in recs:
                    emit(rec, top_when, f"{sysid}.{gname}", sysid)
            obs.append(Observation(fid, captured, top_when, sysid, "__scope__", {"scope": True}, "json:scope", "primary"))
    if "records" in data:
        for rec in data["records"]:
            emit(rec, top_when, data.get("collection") or data.get("origin") or "records")
    return obs


# --------------------------------------------------------------------------- logs
def _parse_line(line: str) -> tuple[str | None, dict]:
    m = TS_RE.match(line)
    ts = m.group(1) if m else None
    rest = line[m.end():] if m else line
    kv: dict = {}
    js = rest.strip()
    if js.startswith("{"):
        try:
            kv = json.loads(js)
        except json.JSONDecodeError:
            kv = {}
    else:
        for k, v in KV_RE.findall(rest):
            if v.startswith('"'):
                v = v[1:-1].replace('\\"', '"')
            kv[k] = v
        if "message" in kv:  # cloud runtime: key/values nested inside message="..."
            for k, v in KV_RE.findall(kv.pop("message")):
                kv.setdefault(k, v.strip('"'))
        if "|" in rest and not kv.get("event"):  # pipe format: ts | LVL | svc | trace | event | k=v
            parts = [p.strip() for p in rest.split("|")]
            if len(parts) >= 5:
                kv.setdefault("service", parts[2])
                kv.setdefault("event", parts[4])
        bracket = re.findall(r"\[([^\]]+)\]", rest)
        if bracket and ":" in rest and not kv.get("event"):  # [LVL] [svc] [trace] event: k=v
            ev = re.search(r"\]\s+([\w]+):", rest)
            if ev:
                kv["event"] = ev.group(1)
            if len(bracket) >= 2:
                kv.setdefault("service", bracket[1])
    tm = TRACE_RE.search(rest)
    if tm:
        kv.setdefault("_trace", tm.group(0))
    for k in ("trace_id", "requestId"):
        if k in kv and TRACE_RE.match(str(kv[k])):
            kv["_trace"] = kv[k]
    return ts, kv


LOOKUP_KEYS = ("lookup_key", "match_key", "correlation_key", "subject_key", "subject_ref",
               "subject_reference", "query")


def parse_log(path: Path, fid: str, captured: str) -> list[Observation]:
    obs: list[Observation] = []
    trace_ctx: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ts, kv = _parse_line(line)
        tr = kv.get("_trace")
        if tr:
            ctx = trace_ctx.setdefault(tr, {})
            for k in LOOKUP_KEYS:
                if kv.get(k) not in (None, "", "null"):
                    ctx.setdefault("lookup", kv[k])
        rid = kv.get("resource_id")
        if not rid or not RESOURCE_RE.fullmatch(str(rid)):
            continue
        attrs = {k: (None if v in ("null", "None") else v) for k, v in kv.items()}
        if tr and trace_ctx.get(tr, {}).get("lookup") and not any(attrs.get(k) for k in LOOKUP_KEYS):
            attrs["lookup_key"] = trace_ctx[tr]["lookup"]
        attrs["_line"] = line[:400]
        obs.append(Observation(fid, captured, ts or captured, kv.get("system") or system_of(rid), rid,
                               attrs, f"log:{kv.get('event','')}", "primary"))
    return obs


# ------------------------------------------------------------------ handover text
HANDOVER_RE = re.compile(r"^-\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(.+)$")


def parse_text(path: Path, fid: str, captured: str) -> list[Observation]:
    obs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = HANDOVER_RE.match(line.strip())
        if m:
            ts, sysid, rid, rtype, result, subj = m.groups()
            obs.append(Observation(fid, captured, ts, sysid, rid,
                                   {"record_type": rtype, "result": result, "subject_reference": subj.strip()},
                                   "text:handover", "primary"))
    return obs


# ------------------------------------------------------------------------ loader
def load_manifest(dataset: Path) -> list[dict]:
    with open(dataset / "evidence_files.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_text_evidence(dataset: Path) -> list[Observation]:
    obs: list[Observation] = []
    for row in load_manifest(dataset):
        p = dataset / row["file_path"]
        fid, cap = row["file_id"], row["captured_at"]
        suffix = p.suffix.lower()
        if suffix == ".sql":
            obs += parse_sql(p, fid, cap)
        elif suffix == ".json":
            obs += parse_json(p, fid, cap)
        elif suffix == ".log":
            obs += parse_log(p, fid, cap)
        elif suffix == ".txt":
            obs += parse_text(p, fid, cap)
    return obs
