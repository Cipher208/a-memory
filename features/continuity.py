"""D1.2 session continuity — the /new recovery pack.

Harness protocol: /new → `session_recap` (this module) → semantic search on
demand. The recap compresses "what was I doing" into ~2K tokens: last closed
session (summary/topics/deltas) → pending work (scratchpad, diff gaps,
staged proposals) → then the D1.1 zero-state tail (markers + day) with the
remaining budget. Takes PRE-RESOLVED mem — no mcp_server imports
(module-cycle rule).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_DAY_CUTOFF_S = 24 * 3600


async def _pending_proposals(user_id: str = "default", limit: int = 20) -> list[Any]:
    """Indirection for tests (inject.py pattern); missing table degrades to []."""
    try:
        from features.staging import list_pending

        return await list_pending(user_id, limit)
    except Exception:
        return []


async def session_recap(
    mem: Any,
    user_id: str,
    budget: int = 2000,
) -> list[dict[str, Any]]:
    """Build the new-session recovery pack within the token budget.

    Blocks: {axis, content, score}. Axes: recap_session, recap_pending, then
    the D1.1 zero-state tail (markers + day) with the remaining budget —
    ~2K tokens of recovery instead of re-reading raw history.
    """
    from shared.tokens import estimate_tokens

    blocks: list[dict[str, Any]] = []
    remaining = budget

    def _add(axis: str, score: float, content: str) -> None:
        nonlocal remaining
        content = content.strip()
        if not content:
            return
        cost = estimate_tokens(content)
        if cost > remaining:
            return
        blocks.append({"axis": axis, "content": content, "score": score})
        remaining -= cost

    # Axis 1: last closed session — summary, topics, state changes.
    try:
        from core.session import SessionStore

        sessions = await SessionStore().get_recent_sessions(user_id, 3)
        closed = next((s for s in sessions if s.ended_at and s.summary), None)
        if closed is not None:
            parts = [f"last session: {closed.summary[:160]}"]
            if closed.topics:
                parts.append(f"topics: {', '.join(str(t) for t in closed.topics[:5])}")
            if closed.state_deltas:
                parts.append(f"changes: {', '.join(str(k) for k in list(closed.state_deltas)[:5])}")
            _add("recap_session", 0.9, " | ".join(parts))
    except Exception as exc:
        logger.debug("recap session axis failed: %s", exc)

    # Axis 2: pending work — scratchpad notes, diff gaps, staged proposals.
    try:
        pend: list[str] = []
        from features.scratchpad import read_entries

        pend.extend(f"pad:{e['key']}={e['content'][:60]}" for e in read_entries(user_id, "user")[:5])
        gaps = await mem.l3.search_by_tag(user_id, "diff_gap", 5)
        fresh = [g for g in gaps if float(getattr(g, "created_at", 0) or 0) >= time.time() - _DAY_CUTOFF_S]
        if fresh:
            pend.append(f"diff_gaps: {len(fresh)} unreviewed")
        props = await _pending_proposals(user_id)
        if props:
            pend.append(f"staged proposals: {len(props)} awaiting review")
        _add("recap_pending", 0.8, " | ".join(pend))
    except Exception as exc:
        logger.debug("recap pending axis failed: %s", exc)

    # Tail: D1.1 zero-state (markers + day) within the remaining budget.
    from features.recall import recall_protocol

    blocks.extend(await recall_protocol(mem, None, user_id, query="", budget=remaining))
    return blocks
