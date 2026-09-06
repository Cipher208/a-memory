"""S18-хвост context_assembly: pre-response сборка контекста — одна CLI-точка.

Контракт (Автохуки.md §3, Эли): адаптер вызывает `autohooks context --text <msg>`
ДО ответа агента и получает готовый inject — агента не думает об инжекте.
Сборка: top-5 релевантных воспоминаний (recall_protocol, оси markers→session→
semantic→expand→day) + свежие L1 (непрерывность диалога), бюджет-capped.
"""

from __future__ import annotations

import json
from typing import Any


async def _collect_recent_l1(mem: Any, limit: int = 3) -> list[str]:
    """Свежие L1-записи для непрерывности; ошибок не роняют (best-effort).

    get_recent может быть sync или async в зависимости от mem-объекта —
    поддерживаем оба (asyncio-корутина awaited, sync-результат как есть).
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
    """{relevant: [блоки recall_protocol], recent: [L1-строки], budget}.

    Recall-блоки уже dedup'нуты по контенту и budget-capped внутри протокола;
    top_k — усечение до 5 релевантных по контракту (оси отдали больше).
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
    """Markdown для вставки в контекст; пустой сбор → '—' (конвенция inject)."""
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
