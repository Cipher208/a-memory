"""Sliding-window limiter for the declared-canon channel (specs S4/S10).

think() and the persona_owner auto_save branch share one budget per user:
declarations are rare by nature; 10/h protects L4 from a runaway client
while staying invisible for legitimate use.
"""

from __future__ import annotations

import time

CANON_MAX_PER_HOUR = 10

_canon_ts: dict[str, list[float]] = {}


def canon_rate_ok(user_id: str) -> bool:
    """Return True and consume one slot, or False when the hourly cap is hit."""
    now = time.time()
    window = [t for t in _canon_ts.get(user_id, []) if now - t < 3600]
    if len(window) >= CANON_MAX_PER_HOUR:
        _canon_ts[user_id] = window
        return False
    window.append(now)
    _canon_ts[user_id] = window
    return True
