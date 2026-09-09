"""Stage 2-C: wake_up (E10, MemoryPalace) — one-shot memory lift.

Glues two existing read aggregators (no LLM, deterministic):
  memory_recap (D1.2 recovery pack: session → pending → markers → day)
  inject critical set (S5: rehydrate/important/pinned/recent)
One shared budget, one answer — "I have woken up, here is what I know".
"""

from __future__ import annotations

from typing import Any


async def wake_up_blocks(
    mem: Any,
    rag: Any,
    user_id: str,
    budget: int = 2000,
) -> dict[str, Any]:
    """{recap: [continuity blocks], inject: [critical-set blocks], budget}.

    The budget is shared: recap takes at most half (session facts must not eat
    the inject-critical set), the remainder goes to the critical set. Empty
    sides are omitted.
    """
    from features.continuity import session_recap
    from features.inject import build_inject_blocks
    from shared.tokens import estimate_tokens

    recap_budget = budget // 2
    recap_blocks = await session_recap(mem, user_id, budget=recap_budget)
    used = sum(estimate_tokens(str(b.get("content", ""))) for b in recap_blocks)
    inject_budget = max(0, budget - used)
    inject_blocks = await build_inject_blocks(mem, rag, user_id, budget=inject_budget)

    return {
        "recap": recap_blocks,
        "inject": inject_blocks,
        "budget": budget,
        "used_tokens": used + sum(estimate_tokens(str(b.get("content", ""))) for b in inject_blocks),
    }


def render_wake_up_md(assembly: dict[str, Any]) -> str:
    """Markdown: recap blocks with [axis], inject blocks with [kind], cache:break between."""
    parts: list[str] = []
    for b in assembly.get("recap", []):
        content = str(b.get("content", "")).strip()
        if content:
            parts.append(f"- [{b.get('axis', 'recap')}] {content}")
    if assembly.get("inject"):
        parts.append("<cache:break>")
        for b in assembly["inject"]:
            content = str(b.get("content", "")).strip()
            if content:
                parts.append(f"- [{b.get('kind', 'memory')}] {content}")
    return "\n".join(parts) if parts else "—"
