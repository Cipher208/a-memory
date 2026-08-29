"""Memory transition state machine + telemetry (B1.5).

Every layer transition the codebase performs (consolidation promotions,
archival, compression) goes through record_transition: a valid transition
is persisted to memory_transitions and counted in metrics; an invalid one
is logged and skipped — live paths must never break on telemetry.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from shared.constants import DB_NAME
from shared.metrics import metrics

logger = logging.getLogger(__name__)

# The real state graph: states are storage tiers, edges are performed moves.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "staging": {"l4"},
    "episode": {"l4", "archived"},
    "l4": {"archived"},
    "l2_session": {"archived"},
    "archived": set(),  # terminal
}


async def record_transition(
    cm: Any,
    user_id: str,
    from_state: str,
    from_ref: str,
    to_state: str,
    to_ref: str,
    reason: str = "",
) -> int:
    """Persist one transition (e.g. episode → l4). Returns rows written (0 = invalid, logged)."""
    if to_state not in VALID_TRANSITIONS.get(from_state, set()):
        logger.warning("invalid memory transition rejected: %s→%s (%s → %s)", from_state, to_state, from_ref, to_ref)
        return 0

    conn = await cm.get(DB_NAME)
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            from_ref TEXT NOT NULL,
            to_ref TEXT NOT NULL,
            reason TEXT,
            ts REAL NOT NULL)"""
    )
    kind = f"{from_state}->{to_state}"
    await conn.execute(
        "INSERT INTO memory_transitions (user_id, kind, from_ref, to_ref, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, kind, from_ref, to_ref, reason, time.time()),
    )
    await conn.commit()
    metrics.inc(f"transition_{kind.replace('->', '_to_')}")
    return 1
