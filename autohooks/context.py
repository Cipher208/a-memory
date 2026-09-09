"""S18 tail context_assembly: pre-response context assembly — a single CLI entry point.

Contract (Avtohuki.md §3, Eli): the adapter calls `autohooks context --text <msg>`
BEFORE the agent answers and receives a ready inject — the agent does not think
about injection. Assembly: top-5 relevant memories (recall_protocol, axes
markers→session→semantic→expand→day) + fresh L1 (dialog continuity),
budget-capped.
"""

from __future__ import annotations

import json
from typing import Any


async def _collect_recent_l1(mem: Any, limit: int = 3) -> list[str]:
    """Fresh L1 records for dialog continuity; errors are swallowed (best-effort).

    get_recent may be sync or async depending on the mem object —
    both are supported (asyncio coroutine awaited, sync result as is).
    """
    try:
        getter = getattr(mem.l1, "get_recent", None) if hasattr(mem, "l1") else None
        if getter is None:
            return []
        result = getter(limit)
        entries = await result if hasattr(result, "__await__") else result
    except Exception:
        return []
    out: list[str] = []
    for e in entries or []:
        text = str(getattr(e, "text", "") or getattr(e, "content", "")).strip()
        if text:
            out.append(text)
    return out


async def assemble_context(
    mem: Any,
    rag: Any,
    user_id: str,
    text: str = "",
    budget: int = 2000,
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    """{relevant: [recall_protocol blocks], recent: [L1 lines], budget}.

    Recall blocks are already deduped by content and budget-capped inside the
    protocol; top_k truncates to 5 relevant blocks per the contract (the axes
    returned more).
    """
    from features.recall import recall_protocol
    from shared.tokens import estimate_tokens

    blocks = await recall_protocol(mem, rag, user_id, query=text, budget=budget)
    relevant = blocks[:top_k]
    recent = await _collect_recent_l1(mem)

    used = sum(estimate_tokens(str(b.get("content", ""))) for b in relevant)
    used += sum(estimate_tokens(t) for t in recent)
    return {"relevant": relevant, "recent": recent, "budget": budget, "used_tokens": used}


def render_context_md(assembly: dict[str, Any]) -> str:
    """Markdown for context insertion; empty assembly → '—' (the inject convention)."""
    parts: list[str] = []
    for b in assembly.get("relevant", []):
        content = str(b.get("content", "")).strip()
        if content:
            parts.append(f"- [{b.get('axis', 'memory')}] {content}")
    recent = [t for t in assembly.get("recent", []) if t]
    if recent:
        parts.append("<recent>")
        for t in recent:
            parts.append(f"- {t}")
        parts.append("</recent>")
    return "\n".join(parts) if parts else "—"


def render_context_json(assembly: dict[str, Any]) -> str:
    return json.dumps(assembly, ensure_ascii=False)
