"""Time handling in one place.

Every timestamp is normalized to one canonical UTC form (YYYY-MM-DDTHH:MM:SSZ) when it is read, so
the rest of the code can compare timestamps safely whatever format a source file used.
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(s: str) -> datetime:
    """Parse an ISO-8601 timestamp (Z or offset, optional fraction) into an aware UTC datetime."""
    d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def canon(s: str | None) -> str | None:
    """Canonical UTC string, or the input unchanged if it is not a timestamp."""
    if not s:
        return s
    try:
        return parse_ts(s).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return s


def minutes_between(later: str, earlier: str) -> float:
    return (parse_ts(later) - parse_ts(earlier)).total_seconds() / 60
