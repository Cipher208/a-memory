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
    related: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        # S19: page → related facts (recall со страницы, гидратация вниз).
        # Рёбра wiki_fact_link минера: wiki_page-узел (content == file_path)
        # ↔ fact-узел (content == core_memory.value) — тот же контракт, что
        # miner_provenance. Private-факты не покидают стор.
        from shared.connection import connection_manager as _cm
        from shared.constants import DB_NAME as _DB

        conn = await _cm.get(_DB)
        rows = await (
            await conn.execute(
                "SELECT cm.key, cm.value, cm.importance FROM epi_edges e"
                " JOIN epi_nodes w ON w.node_id = e.source_id OR w.node_id = e.target_id"
                " JOIN epi_nodes f ON (f.node_id = e.source_id OR f.node_id = e.target_id) AND f.node_id != w.node_id"
                " JOIN core_memory cm ON cm.layer = ? AND cm.user_id = f.user_id AND cm.value = f.content"
                " WHERE w.node_type = 'wiki_page' AND w.content = ? AND f.node_type = 'fact' AND cm.visibility != 'private'"
                " ORDER BY cm.importance DESC LIMIT 10",
                (layer, entry.file_path),
            )
        ).fetchall()
        related = [{"key": str(r["key"]), "value": str(r["value"]), "importance": float(r["importance"])} for r in rows]
    from shared.uris import wiki_uri

    return {
        "status": "ok",
        "title": entry.title,
        "wiki_type": entry.wiki_type,
        "tags": list(entry.tags),
        "file_path": entry.file_path,
        "uri": wiki_uri(layer, entry.file_path),  # Stage 2-A: stable ref
        "content": entry.content,
        "related_facts": related,
        "related_count": len(related),
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
