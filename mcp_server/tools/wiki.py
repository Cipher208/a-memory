from __future__ import annotations

from mcp_server.registry import _get_ctx
from .base import _validate_layer, _get_wiki
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context


async def wiki_add(
    layer: str = "user",
    title: str = "",
    content: str = "",
    wiki_type: str = "concept",
    tags: list[str] | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Add or update a wiki page."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    await wiki.add(wiki_type, title, content, tags)
    return {"status": "ok", "title": title}


async def wiki_search(
    layer: str = "user",
    query: str = "",
    limit: int = 10,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Search wiki pages."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    results = await wiki.search(query, limit)
    return {
        "results": [{"title": str(r.get("title", "")), "type": str(r.get("wiki_type", "")), "tags": list(r.get("tags", []))} for r in results],
        "count": len(results),
    }


async def wiki_list(
    layer: str = "user",
    wiki_type: str = "",
    limit: int = 20,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """List wiki pages."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    if wiki_type:
        pages = await wiki.list_by_type(wiki_type, limit)
    else:
        pages = await wiki.list_all(limit)
    return {
        "pages": [{"title": str(p.title), "type": str(p.wiki_type), "tags": list(p.tags)} for p in pages],
        "count": len(pages),
    }


async def wiki_delete(
    layer: str = "user",
    title: str = "",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Delete a wiki page."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    deleted = await wiki.delete(title)
    return {"status": "ok" if deleted else "not_found", "title": title}
