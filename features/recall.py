"""D1.1 /recall protocol — multi-axis recall (markers → session → semantic → expand → day).

Proportional: empty query = zero-state (markers + day only, ~3 lines);
non-empty query = full report. "Conscious markers outrank session chatter" —
dream-marker facts (importance 0.95) rank above everything.

Takes PRE-RESOLVED mem/rag objects — no mcp_server imports (module-cycle
rule); transports (CLI / MCP tool / dispatcher caller) do the resolution.

Structure: recall_protocol is the orchestrator owning the budget/dedupe
accumulator (_add); each axis is a private collector that returns candidate
blocks and preserves its original exception granularity (a failing sub-axis
never aborts the whole recall).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_DAY_CUTOFF_S = 24 * 3600

# A collector returns candidate blocks as (axis, score, content, extra_keys);
# the orchestrator decides admission via the shared _add accumulator.
_Candidate = tuple[str, float, str, tuple[str, ...]]


def _norm(content: str) -> str:
    return " ".join(str(content).split())[:80]


def _hit_text(hit: dict[str, Any]) -> str:
    return str(hit.get("content") or hit.get("value") or hit.get("summary") or hit.get("title") or "")


async def _collect_markers(mem: Any, user_id: str, cutoff: float) -> tuple[str, tuple[str, ...]] | None:
    """Axis 1: conscious markers — dream facts (0.95) + fresh dream_skill episodes.

    Everything is merged into ONE block (markers outrank everything; parts are
    registered for dedupe so later axes can't re-surface the same content).
    Returns (joined content, dedupe keys) or None when nothing surfaced.
    """
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
        if not parts:
            return None
        values = [f.value[:80] for f in marker_facts]
        return "; ".join(parts), tuple(parts) + tuple(values)
    except Exception as exc:
        logger.debug("recall axis failed: %s", exc)
        return None


async def _collect_day(mem: Any, user_id: str, cutoff: float) -> str | None:
    """Axis day: the last 24h of auto_save episodes, joined; None when none fresh."""
    try:
        day_eps = await mem.l3.search_by_tag(user_id, "auto_save", 5)
        fresh = [
            str(getattr(e, "summary", "") or "").strip()
            for e in day_eps
            if float(getattr(e, "created_at", 0) or 0) >= cutoff and str(getattr(e, "summary", "")).strip()
        ]
        if fresh:
            return " | ".join(fresh)
    except Exception as exc:
        logger.debug("recall axis failed: %s", exc)
    return None


async def _collect_session(mem: Any, user_id: str, cutoff: float) -> list[tuple[float, str]]:
    """Axis 2: recent L1 chatter (0.5) + latest session summary (0.55).

    Two independent try blocks: a failure in one piece must not drop the other.
    """
    out: list[tuple[float, str]] = []
    try:
        recent = [r for r in mem.l1.get_recent(10) if float(getattr(r, "timestamp", 0)) >= cutoff]
        if recent:
            out.append((0.5, "; ".join(f"{r.role}: {r.content[:80]}" for r in recent)))
    except Exception as exc:
        logger.debug("recall axis failed: %s", exc)
    try:
        from core.session import SessionStore

        summary = await SessionStore().get_session_summary(user_id)
        # get_session_summary returns a "No sessions yet." sentinel when empty.
        if summary and summary.strip() != "No sessions yet.":
            out.append((0.55, f"last session: {str(summary)[:160]}"))
    except Exception as exc:
        logger.debug("recall axis failed: %s", exc)
    return out


async def _collect_triggered(user_id: str, query: str) -> list[str]:
    """E11: disclosure triggers — operator rules surface matching content (score 0.95)."""
    out: list[str] = []
    try:
        from features.disclosure import evaluate_disclosures

        for hit in evaluate_disclosures(user_id, query):
            out.append(f"{hit['name']}: {hit['content']}")
    except Exception as exc:
        logger.debug("disclosure axis failed: %s", exc)
    return out


async def _collect_semantic(rag: Any, user_id: str, query: str) -> list[_Candidate]:
    """Axes 3+4: semantic hits and their graph expansion (one RAG call).

    Side effects preserved: zero-result journal (S17 #6), co-retrieval pairs
    (G3), verify audit log (D1.5/E5). D1.5: a semantic hit with zero
    meaningful-token overlap with the query is retrieval noise → dropped;
    expand hits are exempt — their relevance is structural (1-hop), not lexical.
    """
    out: list[_Candidate] = []
    try:
        hits = await rag.search(query, user_id=user_id, limit=8)
        # S17 #6: a failed query (0 hits) → miner signal of "what to model
        # next" (recall_zero_results journal, consumer — MINERS zero_results).
        if not hits:
            try:
                from lifecycle.graph_miners import log_zero_result
                from shared.connection import connection_manager

                await log_zero_result(connection_manager, "user", user_id, query)
            except Exception as exc:
                logger.debug("zero-result journal skipped: %s", exc)
        # G3: co-retrieval journal — pairs of hit ids ('g:12'/'f:5') for
        # miner #7. Pairs of any hit ids with their type prefix; miner #7
        # builds edges from g:-pairs (epi_nodes) and f:-pairs via the
        # rag_pages.path → wiki-node mapping.
        try:
            from lifecycle.graph_miners import log_co_pairs
            from shared.connection import connection_manager

            await log_co_pairs(connection_manager, query, hits)
        except Exception as exc:
            logger.debug("co-pairs journal skipped: %s", exc)
        # D1.5 verification (see docstring).
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
            content = _hit_text(h)
            if content:
                out.append(("semantic", float(h.get("score", 0.0)), content, ()))
        for h in expand_hits:
            content = _hit_text(h)
            if content:
                out.append(("expand", float(h.get("score", 0.0)), content, ()))
    except Exception as exc:
        logger.debug("recall axis failed: %s", exc)
    return out


async def recall_protocol(
    mem: Any,
    rag: Any,
    user_id: str,
    query: str = "",
    budget: int = 2000,
) -> list[dict[str, Any]]:
    """Build multi-axis recall blocks within the token budget.

    Block: {axis, content, score}. Axes: markers, session, semantic, expand,
    day (+triggered on full queries). Empty query = zero-state (markers + day
    only). Dedup by normalized content, first (highest-priority) axis wins.
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

    markers = await _collect_markers(mem, user_id, cutoff)
    if markers is not None:
        await _add("markers", 1.0, markers[0], extra_keys=markers[1])

    if not full:
        # Zero-state: markers + day digest only (~3 lines).
        day = await _collect_day(mem, user_id, cutoff)
        if day is not None:
            await _add("day", 0.4, day)
        return blocks

    for score, content in await _collect_session(mem, user_id, cutoff):
        await _add("session", score, content)
    for content in await _collect_triggered(user_id, query):
        await _add("triggered", 0.95, content)
    if rag is not None:
        for axis, score, content, _extra in await _collect_semantic(rag, user_id, query):
            await _add(axis, score, content)
    day = await _collect_day(mem, user_id, cutoff)
    if day is not None:
        await _add("day", 0.4, day)

    return blocks
