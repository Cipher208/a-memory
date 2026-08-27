"""wiki_summarize — 6-perspective digest of wiki pages.

Maps one of 6 analytical perspectives to a single existing wiki_type and
returns a token-budgeted digest of matching pages. Lives under the `wiki_`
prefix so ARIEL_EXPOSE=primitives,wiki auto-includes it (mcp_server/server.py:32-35).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002 — runtime: MCPServer evaluates this annotation at registration (fix 419d577)

from mcp_server.registry import _get_ctx
from mcp_server.tools.base import (
    _validate_layer,
    _get_wiki,
    _truncate_to_budget,
    DEFAULT_TOKEN_BUDGET,
)

logger = logging.getLogger(__name__)

# Single source of truth for perspective -> (layer, wiki_type).
# To add a new perspective: one line here, one test, one doc line.
# Many-to-one is intentional — 6 perspectives is a curated set, not a
# full coverage of all 15 wiki types. Uncovered types stay reachable
# through `wiki_search`.
PERSPECTIVE_TO_TYPE: dict[str, tuple[Literal["user", "agent"], str]] = {
    "practical": ("agent", "decision_log"),  # what was decided / what to do
    "epistemic": ("agent", "learning_journal"),  # facts, lessons, what was learned
    "psychological": ("agent", "emotional_context"),  # emotions, mood, reflection
    "social": ("user", "relationships"),  # people, contacts, social context
    "temporal": ("user", "retrospective"),  # past, changes, history
    "metacognitive": ("agent", "principle_log"),  # rules, self-model
}

Perspective = Literal[
    "practical",
    "epistemic",
    "psychological",
    "social",
    "temporal",
    "metacognitive",
]


def _validate_perspective(perspective: str) -> str:
    if perspective not in PERSPECTIVE_TO_TYPE:
        raise ValueError(f"Unknown perspective: {perspective!r}. Must be one of {tuple(PERSPECTIVE_TO_TYPE)}")
    return perspective


async def wiki_summarize(
    perspective: str,
    layer: Literal["user", "agent"] = "agent",
    query: str = "",
    limit: int = 10,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Return a token-budgeted digest of wiki pages for one perspective.

    - Empty `query` -> list all pages of the perspective's wiki_type in `layer`.
    - Non-empty `query` -> FTS5 search inside `layer`, filtered to the perspective's type.
    - Result is truncated to DEFAULT_TOKEN_BUDGET (2000) tokens.
    """
    app = _get_ctx(ctx)
    perspective = _validate_perspective(perspective)
    _validate_layer(layer)
    wiki = _get_wiki(app, layer)

    default_layer, wiki_type = PERSPECTIVE_TO_TYPE[perspective]

    if query:
        raw = await wiki.search(query, limit)
        raw = [r for r in raw if r.get("wiki_type") == wiki_type]
    else:
        entries = await wiki.list_by_type(wiki_type, limit)
        raw = [{"title": e.title, "wiki_type": e.wiki_type, "tags": e.tags, "content": e.content[:500]} for e in entries]

    # Build a token-budgeted digest
    parts: list[str] = []
    for r in raw[:limit]:
        title = r.get("title", "Untitled")
        tags = list(r.get("tags", []))
        snippet = (r.get("content", "") or "")[:500]
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        parts.append(f"- **{title}**{tag_str}\n  {snippet}\n")

    digest = "\n".join(parts) if parts else f"(no pages found for perspective={perspective}, type={wiki_type})"
    digest, truncated = _truncate_to_budget(digest, DEFAULT_TOKEN_BUDGET)

    return {
        "perspective": perspective,
        "layer": default_layer,
        "wiki_type": wiki_type,
        "pages": [
            {
                "title": str(r.get("title", "")),
                "type": str(r.get("wiki_type", "")),
                "tags": list(r.get("tags", [])),
            }
            for r in raw
        ],
        "count": len(raw),
        "truncated": truncated,
        "digest": digest,
    }
