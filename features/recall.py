"""D1.1 /recall protocol — multi-axis recall (markers → session → semantic → expand → day).

Proportional: empty query = zero-state (markers + day only, ~3 lines);
non-empty query = full report. "Conscious markers outrank session chatter" —
dream-marker facts (importance 0.95) rank above everything.

Takes PRE-RESOLVED mem/rag objects — no mcp_server imports (module-cycle
rule); transports (CLI / MCP tool / dispatcher caller) do the resolution.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_DAY_CUTOFF_S = 24 * 3600


def _norm(content: str) -> str:
    return " ".join(str(content).split())[:80]


async def recall_protocol(
    mem: Any,
    rag: Any,
    user_id: str,
    query: str = "",
    budget: int = 2000,
) -> list[dict[str, Any]]:
    """Build multi-axis recall blocks within the token budget.

    Block: {axis, content, score}. Axes: markers, session, semantic, expand,
    day. Empty query = zero-state (markers + day only). Dedup by normalized
    content, first (highest-priority) axis wins.
    """
    from shared.tokens import estimate_tokens

    blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    remaining = budget
    cutoff = time.time() - _DAY_CUTOFF_S
    full = bool(query and query.strip())

    async def _add(axis: str, score: float, content: str, extra_keys: tuple[str, ...] = ()) -> None:
        nonlocal remaining
        content = str(content).strip()
        if not content:
            return
        key = _norm(content)
        if key in seen:
            return
        cost = estimate_tokens(content)
        if cost > remaining:
            return
        seen.add(key)
        for k in extra_keys:
            seen.add(_norm(k))
        blocks.append({"axis": axis, "content": content, "score": score})
        remaining -= cost

    # Axis 1: conscious markers — dream facts (0.95) + dream_skill episodes,
    # merged into ONE block (markers outrank everything; parts are registered
    # for dedupe so later axes can't re-surface the same content).
    try:
        parts: list[str] = []
        facts = await mem.l4.get_all(user_id, 50)
        marker_facts = [f for f in facts if str(f.key).startswith("dream_") or f.importance >= 0.95]
        parts.extend(f"{f.key}={f.value[:80]}" for f in marker_facts)
        try:
            skill_eps = await mem.l3.search_by_tag(user_id, "dream_skill", 5)
            parts.extend(
                str(getattr(e, "summary", "") or "").strip()
                for e in skill_eps
                if float(getattr(e, "created_at", 0) or 0) >= cutoff and str(getattr(e, "summary", "")).strip()
            )
        except Exception as exc:
            logger.debug("recall axis failed: %s", exc)
        parts = [p for p in parts if p]
        values = [f.value[:80] for f in marker_facts]
        if parts:
            await _add("markers", 1.0, "; ".join(parts), extra_keys=tuple(parts) + tuple(values))
    except Exception as exc:
        logger.debug("recall axis failed: %s", exc)

    if not full:
        # Zero-state: markers + day digest only (~3 lines).
        try:
            day_eps = await mem.l3.search_by_tag(user_id, "auto_save", 5)
            fresh = [
                str(getattr(e, "summary", "") or "").strip()
                for e in day_eps
                if float(getattr(e, "created_at", 0) or 0) >= cutoff and str(getattr(e, "summary", "")).strip()
            ]
            if fresh:
                await _add("day", 0.4, " | ".join(fresh))
        except Exception as exc:
            logger.debug("recall axis failed: %s", exc)
        return blocks

    # Axis 2: session — recent L1 chatter + latest session summary.
    try:
        recent = [r for r in mem.l1.get_recent(10) if float(getattr(r, "timestamp", 0)) >= cutoff]
        if recent:
            await _add(
                "session",
                0.5,
                "; ".join(f"{r.role}: {r.content[:80]}" for r in recent),
            )
    except Exception as exc:
        logger.debug("recall axis failed: %s", exc)
    try:
        from core.session import SessionStore

        summary = await SessionStore().get_session_summary(user_id)
        # get_session_summary returns a "No sessions yet." sentinel when empty.
        if summary and summary.strip() != "No sessions yet.":
            await _add("session", 0.55, f"last session: {str(summary)[:160]}")
    except Exception as exc:
        logger.debug("recall axis failed: %s", exc)

    # Axis 3+4: semantic hits and their graph expansion (one RAG call — the
    # B1.6 GraphRAG stage already appended 1-hop neighbors with source tags).
    if rag is not None:
        try:
            hits = await rag.search(query, user_id=user_id, limit=8)
            for h in hits:
                source = str(h.get("source", ""))
                content = str(h.get("content") or h.get("value") or h.get("summary") or h.get("title") or "")
                if not content:
                    continue
                score = float(h.get("score", 0.0))
                axis = "expand" if source in ("graph", "graph_expand") else "semantic"
                await _add(axis, score, content)
        except Exception as exc:
            logger.debug("recall axis failed: %s", exc)

    # Axis 5: day — the last 24h of captured memory.
    try:
        day_eps = await mem.l3.search_by_tag(user_id, "auto_save", 5)
        fresh = [
            str(getattr(e, "summary", "") or "").strip()
            for e in day_eps
            if float(getattr(e, "created_at", 0) or 0) >= cutoff and str(getattr(e, "summary", "")).strip()
        ]
        if fresh:
            await _add("day", 0.4, " | ".join(fresh))
    except Exception as exc:
        logger.debug("recall axis failed: %s", exc)

    return blocks
