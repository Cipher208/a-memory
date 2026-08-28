"""wiki_link tool — list or create typed links between wiki pages."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context  # noqa: TC002

from mcp_server.registry import _get_ctx
from mcp_server.tools.base import _validate_layer, _get_wiki

VALID_LINK_TYPES = {"review_of", "revises", "follows"}


async def wiki_link(
    layer: str = "user",
    user_id: str = "default",
    action: str = "list",
    from_path: str | None = None,
    to_path: str | None = None,
    link_type: str = "follows",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """List or add typed links between wiki pages."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)

    if action == "list":
        path = from_path
        links = await wiki.get_links(path) if path else []
        return {"status": "ok", "links": links}

    if action == "add":
        if not from_path or not to_path:
            return {"status": "error", "message": "from_path and to_path required for add"}
        if link_type not in VALID_LINK_TYPES:
            return {"status": "error", "message": f"invalid link_type: {link_type}"}
        link_id = await wiki.index.add_link(from_path, to_path, link_type)
        return {"status": "ok", "link_id": link_id}

    return {"status": "error", "message": f"unknown action: {action}"}
