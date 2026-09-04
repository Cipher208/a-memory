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

    # E11: disclosure triggers — operator rules surface matching content.
    if full:
        try:
            from features.disclosure import evaluate_disclosures

            for hit in evaluate_disclosures(user_id, query):
                await _add("triggered", 0.95, f"{hit['name']}: {hit['content']}")
        except Exception as exc:
            logger.debug("disclosure axis failed: %s", exc)

    # Axis 3+4: semantic hits and their graph expansion (one RAG call — the
    # B1.6 GraphRAG stage already appended 1-hop neighbors with source tags).
    if rag is not None:
        try:
            hits = await rag.search(query, user_id=user_id, limit=8)
            # G3: журнал co-retrieval — пары hit-id ('g:12'/'f:5') для минера #7.
            # Компромисс: пары любых hit-id с префиксом типа, минер отбирает
            # только графовые g:-пары (epi_nodes.node_id).
            try:
                from lifecycle.graph_miners import log_co_pairs
                from shared.connection import connection_manager

                await log_co_pairs(connection_manager, query, hits)
            except Exception as exc:
                logger.debug("co-pairs journal skipped: %s", exc)
            # D1.5 verification: a semantic hit with zero meaningful-token
            # overlap with the query is retrieval noise → dropped. Expand hits
            # are exempt — their relevance is structural (1-hop), not lexical.
            from features.verify import verify_hits

            expand_hits = [h for h in hits if str(h.get("source", "")) in ("graph", "graph_expand")]
            verified, dropped = verify_hits(query, [h for h in hits if h not in expand_hits])
            if dropped:
                logger.debug("verify dropped %d noise hit(s)", len(dropped))
            # E5: persist the aggregate so report card can score integrity.
            try:
                from features.audit_trail import AuditTrail

                await AuditTrail().log_verify(user_id, len(verified), len(dropped))
            except Exception as exc:
                logger.debug("verify log skipped: %s", exc)
            for h in verified:
                content = str(h.get("content") or h.get("value") or h.get("summary") or h.get("title") or "")
                if not content:
                    continue
                await _add("semantic", float(h.get("score", 0.0)), content)
            for h in expand_hits:
                content = str(h.get("content") or h.get("value") or h.get("summary") or h.get("title") or "")
                if not content:
                    continue
                await _add("expand", float(h.get("score", 0.0)), content)
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
