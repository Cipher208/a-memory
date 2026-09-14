"""UTC provenance stamps for stored rows entering LLM context.

Undated past rows get laundered by first-turn reasoning into "current user
words" (live confabulation incident, 2026-09-13). Every render of stored
past content must prefix its UTC source date so the reader sees testimony,
not law.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_date_prefix(ts: Any) -> str:
    """'YYYY-MM-DD ' for a POSIX timestamp (any numeric-ish type); '' otherwise."""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d ")
    except (TypeError, ValueError, OSError):
        return ""


def row_stamp(obj: Any) -> str:
    """Date prefix from a row object's updated_at || created_at (duck-typed)."""
    ts = getattr(obj, "updated_at", None) or getattr(obj, "created_at", None)
    return utc_date_prefix(ts)
