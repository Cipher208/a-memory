"""D2.2 store-pipeline tool: promote distilled memory into skill pages."""

from __future__ import annotations

from typing import Any

# Runtime import: MCPServer evaluates tool annotations at registration;
# hiding Context under TYPE_CHECKING breaks tools/list (fix 419d577).
from mcp.server.mcpserver import Context  # noqa: TC002

from mcp_server.registry import _get_ctx
from shared.metrics import metrics
from shared.constants import METRIC_TOOL_CALLS

from .base import _validate_layer, _get_memory, _get_wiki


async def memory_skill_promote(
    episode_ids: list[int] | None = None,
    title: str = "",
    content: str = "",
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Promote distilled memory into a skill page (D2.2 store pipeline).

    Two modes:
    - `episode_ids`: promote existing episodes verbatim into skill drafts
      (idempotent via the `skill_promoted` tag; provenance footer added).
    - `title` + `content`: write an agent-distilled skill directly
      (provenance: agent-authored). Skills are wiki pages of type `skill`;
      lint caps them at 4KB.
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    mem = _get_memory(app, layer, user_id)
    wiki = _get_wiki(app, layer)

    from features.skill_pipeline import promote_episodes

    if episode_ids:
        res = await promote_episodes(mem, wiki, user_id, [int(i) for i in episode_ids])
        metrics.inc(METRIC_TOOL_CALLS)
        return {"mode": "episodes", **res}
    if not title or not content:
        raise ValueError("provide episode_ids or both title and content")
    path = await wiki.add("skill", title, content, tags=["agent_authored"])
    metrics.inc(METRIC_TOOL_CALLS)
    return {"mode": "authored", "title": title, "path": path, "status": "ok"}
