"""D1.2 session continuity — the /new recovery pack.

Harness protocol: /new → `session_recap` (this module) → semantic search on
demand. The recap compresses "what was I doing" into ~2K tokens:
last closed session (summary/topics/deltas) → latest day-close checkpoint
(BOTH layers) → notes.md handoff tail (agent-specific, config-gated) →
pending work (scratchpad, diff gaps, staged proposals) → then the D1.1
zero-state tail (markers + day) with the remaining budget. Takes
PRE-RESOLVED mem — no mcp_server imports (module-cycle rule).
"""

from __future__ import annotations

import logging
import os
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


def _pick_substantive(sessions: list[Any]) -> Any | None:
    """Prefer the most substantive closed session: message_count, then recency.

    Diagnostic CLI runs close with message_count=0 seconds after a real work
    session — recency alone let a 6-minute audit session shadow a full work
    day (F1 regression, 2026-09-12).
    """
    closed = [s for s in sessions if getattr(s, "ended_at", None) and getattr(s, "summary", None)]
    if not closed:
        return None
    return max(
        closed,
        key=lambda s: (int(getattr(s, "message_count", 0) or 0), float(getattr(s, "started_at", 0) or 0)),
    )


async def _latest_checkpoints(mem: Any, user_id: str, limit: int = 3) -> list[Any]:
    """Latest session_summary-tagged L3 episodes across BOTH layers.

    The day-close protocol (session_ended) persists to the dispatch layer;
    opencode sessions write user-layer, deliberate checkpoints may land in
    agent-layer. The mem facade is user-scoped — the agent side needs its
    own EpisodicStore. Missing agent store degrades silently to user-only.
    """
    stores: list[Any] = [mem.l3]
    try:
        from core.episodic import EpisodicMemory

        stores.append(EpisodicMemory(layer="agent"))
    except Exception as exc:
        logger.debug("agent-layer episode store unavailable: %s", exc)
    out: list[Any] = []
    for store in stores:
        try:
            out.extend(await store.search_by_tag(user_id, "session_summary", limit))
        except Exception as exc:
            logger.debug("checkpoint search failed on %s: %s", type(store).__name__, exc)
    return sorted(out, key=lambda e: float(getattr(e, "created_at", 0) or 0), reverse=True)


def _latest_notes_tail(glob_pattern: str, max_lines: int = 12) -> str:
    """Tail of the newest notes.md matching the agent-specific glob.

    The per-session notes.md handoff is an opencode convention; other agents
    simply don't set continuity.notes_glob and this stays off. Every session
    gets a template notes.md, so files without content beyond the 2-line
    template header are skipped — a fresh empty session must not shadow the
    real handoff (same shadowing bug as recap_session, filesystem edition).
    """
    import glob as _glob

    files = sorted(
        (f for f in _glob.glob(os.path.expanduser(glob_pattern)) if os.path.isfile(f)),
        key=os.path.getmtime,
        reverse=True,
    )
    from pathlib import Path

    for f in files:
        try:
            lines = Path(f).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        body = [ln.rstrip() for ln in lines if ln.strip()]
        if body and body[0].lstrip().startswith("# Session notes"):
            body = body[1:]
        if body and body[0].lstrip().startswith("_Append-only"):
            body = body[1:]
        if not body:
            continue
        return "\n".join(body[-max_lines:])
    return ""


async def session_recap(
    mem: Any,
    user_id: str,
    budget: int = 2000,
) -> list[dict[str, Any]]:
    """Build the new-session recovery pack within the token budget.

    Blocks: {axis, content, score}. Axes: recap_session, recap_checkpoint,
    recap_notes, recap_pending, then the D1.1 zero-state tail (markers + day)
    with the remaining budget — ~2K tokens of recovery instead of re-reading
    raw history.
    """
    from shared.tokens import estimate_tokens

    blocks: list[dict[str, Any]] = []
    remaining = budget
    failed: list[str] = []  # fail-loud: axes that died must say so, not vanish

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

    # Axis 1: last substantive closed session — summary, topics, state changes.
    try:
        from core.session import SessionStore

        sessions = await SessionStore().get_recent_sessions(user_id, 3)
        closed = _pick_substantive(sessions)
        if closed is not None:
            parts = [f"last session: {closed.summary[:160]}"]
            if closed.topics:
                parts.append(f"topics: {', '.join(str(t) for t in closed.topics[:5])}")
            if closed.state_deltas:
                parts.append(f"changes: {', '.join(str(k) for k in list(closed.state_deltas)[:5])}")
            _add("recap_session", 0.9, " | ".join(parts))
    except Exception as exc:
        logger.warning("recap session axis failed: %s", exc)
        failed.append(f"session: {exc}")

    # Axis 2: latest day-close checkpoint — session_summary episodes, BOTH layers.
    try:
        checkpoints = await _latest_checkpoints(mem, user_id)
        if checkpoints:
            _add("recap_checkpoint", 0.95, f"checkpoint: {str(checkpoints[0].summary)[:220]}")
    except Exception as exc:
        logger.warning("recap checkpoint axis failed: %s", exc)
        failed.append(f"checkpoint: {exc}")

    # Axis 3: notes.md handoff tail (opencode-only; off without notes_glob).
    try:
        from config import config

        glob_pattern = str(config.get("continuity", "notes_glob", default="") or "")
        if glob_pattern:
            tail = _latest_notes_tail(glob_pattern)
            if tail:
                _add("recap_notes", 0.85, f"notes tail: {tail}")
    except Exception as exc:
        logger.warning("recap notes axis failed: %s", exc)
        failed.append(f"notes: {exc}")

    # Axis 4: pending work — scratchpad notes, diff gaps, staged proposals.
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
        logger.warning("recap pending axis failed: %s", exc)
        failed.append(f"pending: {exc}")

    # Fail-loud marker: an injected pack missing axes must say which died —
    # a silently thin pack reads as "nothing to report" to the agent.
    if failed:
        _add("recap_degraded", 1.0, "axes failed: " + "; ".join(failed[:4]))

    # Tail: D1.1 zero-state (markers + day) within the remaining budget.
    from features.recall import recall_protocol

    blocks.extend(await recall_protocol(mem, None, user_id, query="", budget=remaining))
    return blocks
