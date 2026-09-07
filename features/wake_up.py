"""Stage 2-C: wake_up (E10, MemoryPalace) — атомарный подъём памяти одной командой.

Склейка двух существующих read-агрегаторов (без LLM, детерминизм):
  memory_recap (D1.2 recovery pack: сессия → pending → markers → day)
  inject critical set (S5: rehydrate/important/pinned/recent)
Один общий бюджет, один ответ — «я проснулась, что я знаю».
"""

from __future__ import annotations

from typing import Any


async def wake_up_blocks(
    mem: Any,
    rag: Any,
    user_id: str,
    budget: int = 2000,
) -> dict[str, Any]:
    """{recap: [блоки continuity], inject: [блоки critical set], budget}.

    Бюджет общий: recap берёт не более половины (session-факты не съедают
    inject-критику), остаток — critical set. Пустые стороны опускаются.
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
    """Markdown: recap-блоки с [axis], inject-блоки с [kind], cache:break между."""
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
