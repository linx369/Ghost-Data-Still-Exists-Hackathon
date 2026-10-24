"""Screenshots and PDFs -> the same Observation records as the text parsers.

The model only *describes* what the artifact shows (records, identifiers, visible state);
it never decides deletion status. Output is cached per file, so this runs once.
PDF text is extracted locally with pypdf and sent as text; PNGs are sent as images.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .evidence import Observation, RESOURCE_RE, load_manifest, parse_activity_links, system_of
from .llm import LLM

SYSTEM = """You extract facts from internal business artifacts (admin-console screenshots, PDFs) of a
fictional e-commerce company for a data-deletion audit. Report only what is visibly shown; never guess.
Return JSON only, matching this shape:
{"records": [{
  "resource_id": "exact id as shown, e.g. account_5762, support_1034, backup_9287, audit_record_003, tombstone_12, analytics_77, data_flow_9, export_3, search_4, cache_5, email_delivery_6",
  "record_type": "one of USER_PROFILE, SEARCH_INDEX_ENTRY, SUPPORT_MESSAGE, SUPPORT_ATTACHMENT, ANALYTICS_PROFILE, PRODUCT_TELEMETRY, USER_EXPORT, MARKETING_PROFILE, EMAIL_DELIVERY_LOG, SECURITY_AUDIT_LOG, LEGAL_HOLD_RECORD, BACKUP_ARCHIVE, ACCOUNT_TOMBSTONE, DATA_FLOW, CACHE_ENTRY, OTHER",
  "user_ids": ["user_NN references tied to this record"],
  "emails": ["emails tied to this record"], "phones": ["phones"], "names": ["person names tied to this record"],
  "state": "one of PRESENT, ABSENT, REDACTED, ANONYMIZED, ENABLED, DISABLED, RESTORE_ALLOWED, RESTORE_BLOCKED, RETAINED, UNKNOWN",
  "shows_direct_identifiers": true/false,
  "observed_at": "ISO-8601 time the record state was observed if shown, else null",
  "note": "<= 20 words of supporting detail"
}]}
Also include stored objects/documents that are identified only by a file name or a document/statement
reference (e.g. a PDF in a storage bucket, a statement number): use "resource_id": "" and list those names
in "aliases". Records listed for other people (other rows in a table) must be included too, each with their
own identifiers. Every record may carry "aliases": file names, ticket numbers, statement or order references."""


class MMRecord(BaseModel):
    resource_id: str
    record_type: str = "OTHER"
    user_ids: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    state: str = "UNKNOWN"
    shows_direct_identifiers: bool = False
    observed_at: Optional[str] = None
    note: str = ""
    aliases: list[str] = Field(default_factory=list)


class MMResult(BaseModel):
    records: list[MMRecord] = Field(default_factory=list)


STATE_MAP = {"PRESENT": "FOUND", "ABSENT": "NOT_FOUND", "REDACTED": "REDACTED", "ANONYMIZED": "ANONYMIZED",
             "ENABLED": "ACTIVE", "DISABLED": "DISABLED", "RESTORE_ALLOWED": "ACTIVE",
             "RESTORE_BLOCKED": "BLOCKED", "RETAINED": "FOUND"}


def _content_for(path: Path):
    if path.suffix.lower() == ".png":
        data = base64.standard_b64encode(path.read_bytes()).decode()
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                {"type": "text", "text": f"Artifact file name: {path.name}. Extract the records."}]
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    return [{"type": "text", "text": f"Artifact file name: {path.name}\n---\n{text}\n---\nExtract the records."}]


def load_multimodal_evidence(dataset: Path, llm: LLM, log=print) -> list[Observation]:
    from concurrent.futures import ThreadPoolExecutor
    obs: list[Observation] = []
    # aliases (file names, statement numbers) -> resource ids, from the data-catalog link events
    alias_to_rid = {o.attrs["alias"].lower(): o.resource_id for o in parse_activity_links(dataset)
                    if o.attrs.get("alias") and o.attrs["alias"] != o.resource_id}
    rows = [r for r in load_manifest(dataset) if Path(r["file_path"]).suffix.lower() in (".png", ".pdf")]

    def run(row):
        p = dataset / row["file_path"]
        return row, *llm.ask_json(SYSTEM, _content_for(p), MMResult, tag=f"mm_{row['file_id']}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        answers = list(pool.map(run, rows))
    for row, res, meta in answers:
        if res is None:
            log(f"  multimodal: no answer for {row['file_id']} ({meta.get('errors')})")
            continue
        for r in res.records:
            rid = r.resource_id.strip()
            if not RESOURCE_RE.fullmatch(rid):
                rid = next((alias_to_rid[a.lower()] for a in r.aliases if a.lower() in alias_to_rid), "")
            if not RESOURCE_RE.fullmatch(rid):
                continue
            attrs = {"record_type": r.record_type if r.record_type != "OTHER" else "",
                     "result": STATE_MAP.get(r.state, ""),
                     "subject_reference": " ".join(r.user_ids + r.emails),
                     "phone": r.phones[0] if r.phones else None,
                     "name": r.names[0] if r.names else None,
                     "shows_direct_identifiers": r.shows_direct_identifiers,
                     "aliases": r.aliases, "note": r.note, "_model": meta.get("model"),
                     "_artifact": Path(row["file_path"]).name}
            # a screenshot/PDF shows state as of its capture time
            obs.append(Observation(row["file_id"], row["captured_at"], row["captured_at"],
                                   system_of(rid), rid, attrs, "multimodal", "primary"))
    return obs
