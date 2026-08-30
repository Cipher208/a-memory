from __future__ import annotations

import contextlib

from mcp_server.registry import _get_ctx
from .base import _validate_layer, _get_wiki
from typing import Any

# Runtime import: MCPServer evaluates tool annotations at registration;
# hiding Context under TYPE_CHECKING breaks tools/list (fix 419d577).
from mcp.server.mcpserver import Context  # noqa: TC002


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
        "results": [
            {
                "title": str(r.get("title", "")),
                "type": str(r.get("wiki_type", "")),
                "tags": list(r.get("tags", [])),
                "snippet": str(r.get("content", ""))[:200],
            }
            for r in results
        ],
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
        "pages": [{"title": str(p.title), "type": str(p.wiki_type), "tags": list(p.tags), "path": str(p.file_path)} for p in pages],
        "count": len(pages),
    }


async def wiki_read(
    layer: str = "user",
    path: str = "",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Read a wiki page's full content (progressive-disclosure read leg, D2.1).

    `path` comes from wiki_list / wiki_search results. Skills live under
    wiki_type="skill" as plain Markdown (SKILL.md convention). Skill reads
    are audit-logged (`skill_read`) — D2.4 usage-driven reinforcement.
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    entry = await wiki.get(path)
    if entry is None:
        return {"status": "not_found", "path": path}
    with contextlib.suppress(Exception):
        if entry.wiki_type == "skill":
            import sqlite3 as _sqlite3
            import time as _time

            from shared.connection import connection_manager

            with _sqlite3.connect(str(connection_manager.base_dir / "memory.db")) as _conn:
                _conn.execute(
                    "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp)"
                    " VALUES ('default', 'skill_read', 'wiki', ?, '{}', ?)",
                    (str(entry.file_path), _time.time()),
                )
                _conn.commit()  # telemetry is best-effort, never blocks the read
    return {
        "status": "ok",
        "title": entry.title,
        "wiki_type": entry.wiki_type,
        "tags": list(entry.tags),
        "file_path": entry.file_path,
        "content": entry.content,
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
