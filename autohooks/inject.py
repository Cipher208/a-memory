# autohooks/inject.py
"""Session-start inject: dispatch session_started, render the critical set (spec S5)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from autohooks.config import AgentConfig


def _collect_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """fire() returns {"results": [<handler returns>]}; session_started returns {"blocks": [...]}."""
    blocks: list[dict[str, Any]] = []
    for r in result.get("results", []):
        if isinstance(r, dict) and isinstance(r.get("blocks"), list):
            blocks.extend(r["blocks"])
    return blocks


def _render_md(blocks: list[dict[str, Any]]) -> str:
    if not blocks:
        return "—"
    lines: list[str] = []
    for b in blocks:
        kind = b.get("kind", "memory")
        content = str(b.get("content", "")).strip()
        if content:
            lines.append(f"- [{kind}] {content}")
    return "\n".join(lines) if lines else "—"


async def run_inject(
    cfg: AgentConfig,
    mem: Any,
    graph: Any,
    rag: Any,
    text: str = "",
    fmt: str = "md",
    *,
    dispatch: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    budget: int | None = None,
) -> str:
    """Dispatch session_started (its handler builds the budget-capped critical set) and render it."""
    from config import config

    from hooks.external import dispatch_event

    dispatch = dispatch or dispatch_event
    budget = budget if budget is not None else int(config.get("inject", "token_budget", default=2000))
    result = await dispatch(
        "session_started",
        cfg.layer,
        cfg.user_id,
        {"text": text, "budget": budget},
        mem,
        graph,
        rag,
    )
    blocks = _collect_blocks(result)
    if fmt == "json":
        return json.dumps({"blocks": blocks, "budget": budget}, ensure_ascii=False)
    return _render_md(blocks)
