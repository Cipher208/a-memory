"""Critical-set inject builder (spec S5): budget-capped blocks, no LLM.

"Critical" (resolved conflict #2): ACT-R top-5 relevant (when text given),
recent L1 (24h), important core facts (importance >= inject.important_min).

Takes PRE-RESOLVED mem/rag objects — this module must not import
mcp_server (that recreates the base → context import cycle mypy chokes on);
transports (endpoint / MCP tool / dispatcher caller) do the resolution.
"""

from __future__ import annotations

import time
from typing import Any


async def _pending_proposals(user_id: str = "default", limit: int = 5) -> list[Any]:
    """Indirection for tests; returns pending proposals via features.staging."""
    try:
        from features.staging import list_pending

        return await list_pending(user_id, limit)
    except Exception:
        return []


async def build_inject_blocks(
    mem: Any,
    rag: Any,
    user_id: str,
    text: str = "",
    budget: int = 2000,
) -> list[dict[str, Any]]:
    """Build inject blocks within the token budget. Blocks: {kind, content, score}."""
    from config import config

    from shared.tokens import estimate_tokens

    blocks: list[dict[str, Any]] = []
    remaining = budget

    if text and rag is not None:
        hits = await rag.search(text, user_id=user_id, limit=5)
        for h in hits[:5]:
            content = str(h.get("content") or h.get("value") or h.get("summary") or h.get("title") or "")
            if not content:
                continue
            cost = estimate_tokens(content)
            if cost > remaining:
                break
            blocks.append({"kind": "relevant", "content": content, "score": float(h.get("score", 0.0))})
            remaining -= cost

    cutoff = time.time() - 24 * 3600
    recent = [r for r in mem.l1.get_recent(10) if r.timestamp >= cutoff]
    if recent:
        content = "; ".join(f"{r.role}: {r.content[:80]}" for r in recent)
        cost = estimate_tokens(content)
        if cost <= remaining:
            blocks.append({"kind": "recent", "content": content, "score": 0.0})
            remaining -= cost

    # diff_gap block: surface recent L3 episodes tagged diff_gap (C1.10 S4).
    try:
        gap_episodes = await mem.l3.search_by_tag(user_id, "diff_gap", limit=5)
    except Exception:
        gap_episodes = []
    gap_lines = [
        str(getattr(e, "summary", "") or "").strip()
        for e in gap_episodes
        if float(getattr(e, "created_at", 0) or 0) >= cutoff and str(getattr(e, "summary", "")).startswith("diff_gap:")
    ]
    if gap_lines:
        content = " | ".join(gap_lines)[: max(0, remaining)]
        cost = estimate_tokens(content)
        if cost <= remaining:
            blocks.append({"kind": "gap", "content": content, "score": 0.5})
            remaining -= cost

    # rehydrate block: compaction drift recovery (D3.5 S4)
    try:
        from features.rehydrate import rehydrate_enabled, recent_compaction

        compaction = None
        if rehydrate_enabled():
            window = float(config.get("rehydrate", "window_hours", default=6.0))
            compaction = recent_compaction(user_id, window)
    except Exception:
        compaction = None
    if compaction is not None:
        try:
            important_min = float(config.get("inject", "important_min", default=0.8))
            facts = await mem.l4.get_all(user_id, 50)
            top = [f for f in facts if f.importance >= important_min]
        except Exception:
            top = []
        if top:
            content = "; ".join(f"{f.key}={f.value[:80]}" for f in top)
            cost = estimate_tokens(content)
            if cost <= remaining:
                blocks.append({"kind": "rehydrate", "content": content, "score": 0.9})
                remaining -= cost

    # pending proposals: staged mutations awaiting review (C1.11 S5)
    try:
        pending = await _pending_proposals(user_id)
    except Exception:
        pending = []
    if pending:
        lines = []
        for p in pending[:5]:
            payload = p.get("payload", {})
            gist = str(payload.get("value") or payload.get("ids") or payload.get("items") or "")[:80]
            age_days = (time.time() - float(p.get("proposed_at", time.time()))) / 86400
            lines.append(f"#{p['id']} {p['kind']}: {gist} ({age_days:.0f}d)")
        header = f"{len(pending)} staged mutation(s) await review (expire in 7d). Decide: memory_proposals(action='decide', proposal_id=…, approve=true|false)"
        content = header + "\n" + "\n".join(lines)
        cost = estimate_tokens(content)
        if cost <= remaining:
            blocks.append({"kind": "proposals", "content": content, "score": 0.6})
            remaining -= cost

    important_min = float(config.get("inject", "important_min", default=0.8))
    facts = await mem.l4.get_all(user_id, 50)
    important = [f for f in facts if f.importance >= important_min]
    if important:
        content = "; ".join(f"{f.key}={f.value[:80]}" for f in important)
        cost = estimate_tokens(content)
        if cost <= remaining:
            blocks.append({"kind": "important", "content": content, "score": max(f.importance for f in important)})

    return blocks
