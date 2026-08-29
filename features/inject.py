"""Critical-set inject builder (spec S5): budget-capped blocks, no LLM.

"Critical" (resolved conflict #2): ACT-R top-5 relevant (when text given),
recent L1 (24h), important core facts (importance >= inject.important_min).
"""

from __future__ import annotations

import time
from typing import Any


def _resolve_mem(app: Any, layer: str, user_id: str) -> Any:
    from mcp_server.tools.base import _get_memory

    return _get_memory(app, layer, user_id)


def _resolve_rag(app: Any, layer: str) -> Any:
    from mcp_server.tools.base import _get_rag

    return _get_rag(app, layer)


async def build_inject_blocks(app: Any, layer: str, user_id: str, text: str = "", budget: int = 2000) -> list[dict[str, Any]]:
    """Build inject blocks within the token budget. Blocks: {kind, content, score}."""
    from mcp_server.tools.base import _estimate_tokens

    mem = _resolve_mem(app, layer, user_id)
    blocks: list[dict[str, Any]] = []
    remaining = budget

    if text:
        rag = _resolve_rag(app, layer)
        hits = await rag.search(text, user_id=user_id, limit=5)
        for h in hits[:5]:
            content = str(h.get("content") or h.get("value") or h.get("summary") or h.get("title") or "")
            if not content:
                continue
            cost = _estimate_tokens(content)
            if cost > remaining:
                break
            blocks.append({"kind": "relevant", "content": content, "score": float(h.get("score", 0.0))})
            remaining -= cost

    cutoff = time.time() - 24 * 3600
    recent = [r for r in mem.l1.get_recent(10) if r.timestamp >= cutoff]
    if recent:
        content = "; ".join(f"{r.role}: {r.content[:80]}" for r in recent)
        cost = _estimate_tokens(content)
        if cost <= remaining:
            blocks.append({"kind": "recent", "content": content, "score": 0.0})
            remaining -= cost

    from config import config

    important_min = float(config.get("inject", "important_min", default=0.8))
    facts = await mem.l4.get_all(user_id, 50)
    important = [f for f in facts if f.importance >= important_min]
    if important:
        content = "; ".join(f"{f.key}={f.value[:80]}" for f in important)
        cost = _estimate_tokens(content)
        if cost <= remaining:
            blocks.append({"kind": "important", "content": content, "score": max(f.importance for f in important)})

    return blocks
