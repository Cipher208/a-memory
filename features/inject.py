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
        l3_recent = mem.l3.get_recent(20) if hasattr(mem, "l3") and hasattr(mem.l3, "get_recent") else []
    except Exception:
        l3_recent = []
    gap_lines = [str(getattr(e, "content", "") or "").strip() for e in l3_recent if str(getattr(e, "content", "")).startswith("diff_gap:")]
    if gap_lines:
        content = " | ".join(gap_lines)[: max(0, remaining)]
        cost = estimate_tokens(content)
        if cost <= remaining:
            blocks.append({"kind": "gap", "content": content, "score": 0.5})
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
