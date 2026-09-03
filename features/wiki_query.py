"""A1.4: wiki_query — BFS traversal over typed wiki_links (relationship context).

Recursive-CTE walk over the wiki_links table (pattern: graph/epistemic
get_neighbors). Layer-scoped; returns nodes with depth + link_type.
"""

from __future__ import annotations

from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME


async def wiki_query_bfs(
    start_path: str,
    depth: int = 2,
    layer: str = "user",
    link_type: str | None = None,
    direction: str = "out",
    limit: int = 50,
) -> dict[str, Any]:
    """BFS from `start_path` over wiki_links. direction: out | in | both."""
    cm = connection_manager
    conn = await cm.get(DB_NAME)
    depth = max(1, min(int(depth), 5))

    type_filter = "AND link_type = ?" if link_type else ""
    if direction == "out":
        base = f"SELECT to_path, link_type, 1 FROM wiki_links WHERE from_path = ? AND layer = ? {type_filter}"
        recursive = f"JOIN wiki_links l ON l.from_path = g.path AND l.layer = ? {type_filter}"
    elif direction == "in":
        base = f"SELECT from_path, link_type, 1 FROM wiki_links WHERE to_path = ? AND layer = ? {type_filter}"
        recursive = f"JOIN wiki_links l ON l.to_path = g.path AND l.layer = ? {type_filter}"
    else:  # both
        base = (
            f"SELECT to_path, link_type, 1 FROM wiki_links WHERE from_path = ? AND layer = ? {type_filter}"
            f" UNION SELECT from_path, link_type, 1 FROM wiki_links WHERE to_path = ? AND layer = ? {type_filter}"
        )
        recursive = f"JOIN wiki_links l ON (l.from_path = g.path OR l.to_path = g.path) AND l.layer = ? {type_filter}"

    base_params: list[Any] = [start_path, layer] + ([link_type] if link_type else [])
    if direction == "both":
        base_params = base_params  # already includes both sides
    rec_params: list[Any] = [layer] + ([link_type] if link_type else [])

    sql = f"""
    WITH RECURSIVE walk(path, link_type, d) AS (
        {base}
        UNION
        SELECT CASE WHEN l.from_path = g.path THEN l.to_path ELSE l.from_path END,
               l.link_type, g.d + 1
        FROM walk g {recursive}
        WHERE g.d < ?
    )
    SELECT path, MIN(d) as depth, MIN(link_type) as link_type
    FROM walk WHERE path != ?
    GROUP BY path ORDER BY depth, path LIMIT ?
    """
    params = base_params + rec_params + [depth, start_path, limit]
    cur = await conn.execute(sql, tuple(params))
    rows = await cur.fetchall()
    return {
        "start": start_path,
        "depth": depth,
        "nodes": [{"path": r[0], "depth": int(r[1]), "link_type": r[2]} for r in rows],
    }
