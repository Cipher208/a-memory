from __future__ import annotations

from mcp_server.registry import _get_ctx
from .base import _validate_layer, _get_wiki
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context

async def wiki_add(
    layer: str = "user",
    title: str = "",
    content: str = "",
    wiki_type: str = "concept",
    tags: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Add or update a wiki page."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    await wiki.save(title, content, wiki_type, tags)
    return {"status": "ok", "title": title}

async def wiki_search(
    layer: str = "user",
    query: str = "",
    limit: int = 10,
    ctx: Context | None = None,
) -> dict:
    """Search wiki pages."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    results = await wiki.search(query, limit)
    return {"results": [{"title": r.title, "type": r.wiki_type, "tags": r.tags} for r in results], "count": len(results)}

async def wiki_list(
    layer: str = "user",
    wiki_type: str = "",
    limit: int = 20,
    ctx: Context | None = None,
) -> dict:
    """List wiki pages."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    if wiki_type:
        pages = await wiki.list_by_type(wiki_type, limit)
    else:
        pages = await wiki.list_all(limit)
    return {"pages": [{"title": p.title, "type": p.wiki_type, "tags": p.tags} for p in pages], "count": len(pages)}

async def wiki_delete(
    layer: str = "user",
    title: str = "",
    ctx: Context | None = None,
) -> dict:
    """Delete a wiki page."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    deleted = await wiki.delete(title)
    return {"status": "ok" if deleted else "not_found", "title": title}
