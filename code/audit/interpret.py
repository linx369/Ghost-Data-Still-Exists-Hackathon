"""Interpret one Observation into a policy category and a normalized state.

This is deterministic and table-driven: each evidence format reports state differently
(HTTP 404, rows=0, result=NOT_FOUND, enabled=false, restore_allowed=0, null PII fields...).
We map all of them onto a small vocabulary that the policy engine reasons over:

  PRESENT    the record exists and still identifies the user
  ABSENT     the record does not exist
  CLEAN      the record exists but carries no prohibited identifiers (redacted/anonymized)
  ACTIVE     a data flow is enabled / a backup can still be restored
  STOPPED    a data flow is disabled / restore is blocked
  RETAINED   a policy-required record exists (security audit, legal hold)
"""
from __future__ import annotations

from .evidence import EMAIL_RE, Observation

PII_FIELDS = ("email", "phone", "email_hash", "full_name", "name", "requester_name", "requester_email",
              "requester_phone", "owner_email", "owner_phone", "customer_email", "display_name",
              "customer_reference", "user_id")
TRUTHY = {"true", "1", "yes", "y", True, 1}


def _truthy(v) -> bool:
    return (str(v).lower() in {"true", "1", "yes"}) if not isinstance(v, bool) else v


def _has_pii(a: dict, fields=PII_FIELDS) -> bool:
    for f in fields:
        v = a.get(f)
        if v not in (None, "", "null", "None"):
            return True
    for f in ("message_text", "message_excerpt", "matched_value"):
        if a.get(f) and (EMAIL_RE.search(str(a[f])) or f == "matched_value"):
            return True
    return False


def category(o: Observation) -> str:
    a, sysid = o.attrs, o.system_id
    rt = str(a.get("record_type", "")).upper()
    if rt and rt not in ("DATA_FLOW",):
        return rt
    ev = str(a.get("event", ""))
    if sysid == "account_service":
        return "USER_PROFILE"
    if sysid == "search_service":
        return "SEARCH_INDEX_ENTRY"
    if sysid == "support_platform":
        if "attachment" in ev or str(a.get("file", "")).endswith(".pdf") or a.get("attachment_name"):
            return "SUPPORT_ATTACHMENT"
        if a.get("source") == "legacy_contact_index" or ev.startswith("contact_query") or ev == "deletion_marker_check":
            return "SUPPORT_ATTACHMENT"  # subject-only contact copy: whole record must go
        return "SUPPORT_MESSAGE"
    if sysid == "analytics_platform":
        if "contact_lookup" in ev or "email_opt_in" in a:
            return "MARKETING_PROFILE"
        return "ANALYTICS_PROFILE"
    if sysid == "export_service":
        return "USER_EXPORT"
    if sysid == "integration_service":
        return "DATA_FLOW"
    if sysid == "backup_service":
        return "BACKUP_ARCHIVE"
    if sysid == "security_service":
        return "SECURITY_AUDIT_LOG"
    if sysid == "identity_service":
        return "ACCOUNT_TOMBSTONE"
    if sysid in ("legal_hold", "legal_service"):
        return "LEGAL_HOLD_RECORD"
    if sysid == "cache_service":
        return "CACHE_ENTRY"
    if sysid == "email_delivery_service":
        return "EMAIL_DELIVERY_LOG"
    if sysid == "marketing_platform":
        return "MARKETING_PROFILE"
    return "UNKNOWN"


def state(o: Observation) -> str | None:
    """Return the normalized state, or None when the observation says nothing about state."""
    a = o.attrs
    ev = str(a.get("event", ""))
    res = str(a.get("result", "")).upper()
    cat = category(o)

    # --- explicit result vocab (handover, diagnostics, manifests)
    if res:
        if res in ("NOT_FOUND", "ABSENT", "DELETED", "MISS"):
            return "ABSENT"
        if res in ("ANONYMIZED", "REDACTED"):
            return "CLEAN"
        if res in ("DISABLED", "BLOCKED", "STOPPED"):
            return "STOPPED"
        if res == "ACTIVE" and cat in ("DATA_FLOW", "BACKUP_ARCHIVE"):
            return "ACTIVE"
        if res in ("IDENTIFIABLE", "FOUND", "PRESENT", "HIT", "ACTIVE"):
            if cat in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
                return "RETAINED"
            return "PRESENT"

    # --- data flows
    if cat == "DATA_FLOW":
        for k in ("enabled",):
            if k in a and a[k] is not None:
                return "ACTIVE" if _truthy(a[k]) else "STOPPED"
        return None
    # --- backups
    if cat == "BACKUP_ARCHIVE":
        if "restore_allowed" in a and a["restore_allowed"] is not None:
            return "ACTIVE" if _truthy(a["restore_allowed"]) else "STOPPED"
        if "eligible" in a or "deletion_block" in a:
            blocked = _truthy(a.get("deletion_block", False)) or not _truthy(a.get("eligible", True))
            return "STOPPED" if blocked else "ACTIVE"
        return None
    if cat in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
        if str(a.get("rows", "1")) == "0":
            return "ABSENT"
        return "RETAINED"

    # --- lookups that report existence
    if str(a.get("http_status", "")) == "404" or str(a.get("rows", "")) == "0" or a.get("profile_present") is False:
        return "ABSENT"
    if str(a.get("result", "")).lower() == "miss" or str(a.get("hits", "")) == "0":
        return "ABSENT"
    if "available" in a and not _truthy(a["available"]):
        return "ABSENT"
    if "download_available" in a and not _truthy(a["download_available"]) and cat == "USER_EXPORT":
        return "ABSENT"
    status = str(a.get("account_status", "")).upper()
    if status == "DELETED" and cat == "USER_PROFILE":
        return "ABSENT"

    if ev.endswith("_started"):
        return None  # a query starting proves nothing

    # --- record exists; does it still identify the person?
    if cat in ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY", "SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG",
               "ACCOUNT_TOMBSTONE"):
        pii_fields = PII_FIELDS if cat != "ACCOUNT_TOMBSTONE" else (
            "name", "full_name", "email", "phone", "email_hash")
        if cat == "SUPPORT_MESSAGE":
            pii_fields = ("requester_name", "requester_email", "requester_phone", "email", "phone", "name")
        if cat == "ANALYTICS_PROFILE":
            pii_fields = ("email", "phone", "email_hash", "customer_reference", "user_id")
        return "PRESENT" if _has_pii(a, pii_fields) else "CLEAN"
    return "PRESENT"
