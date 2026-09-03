"""A1.5: knowledge graph from wiki — entries → concepts → epistemic graph.

Builder precedent: lifecycle/graph_builder.py (episodes → entities). This
sibling walks wiki pages and lands each as an epi_nodes fact (node_type
"wiki_page", content = file_path — the wiki path IS the concept identity),
linked by wiki_links → epi_edges (relation = link_type). Idempotent: nodes
dedup by (layer, user, content); edges by the epi_edges PK.

Wired nightly next to build_from_episodes (hooks/user_hooks._nightly).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager

logger = logging.getLogger(__name__)


async def build_from_wiki(
    cm: AsyncConnectionManager | None = None,
    user_id: str = "default",
    layer: str = "user",
) -> dict[str, int]:
    """Index every wiki page of `layer` into the epistemic graph.

    Returns {"pages": N, "links": M} — rows landed (dedup counted).
    """
    from wiki.manager import WikiManager

    cm = cm or connection_manager
    wm = WikiManager(layer=layer, cm=cm)
    rows = await wm.index.list_all(limit=200, status=None)
    conn = await cm.get("memory.db")
    pages = 0
    links = 0

    for row in rows:
        file_path = str(row["file_path"])
        await _ensure_node(conn, layer, user_id, file_path, float(row["importance"] or 0.5))
        pages += 1

    for row in rows:
        src_path = str(row["file_path"])
        src_id = await _node_id(conn, layer, user_id, src_path)
        if src_id is None:
            continue
        for link in await wm.index.get_links(src_path):
            if link.get("direction") != "out":
                continue  # in-links land via the other page's out-links
            dst_id = await _node_id(conn, layer, user_id, str(link["path"]))
            if dst_id is None:
                continue
            cur = await conn.execute(
                "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, 0.6, ?, '[]')",
                (src_id, dst_id, str(link["link_type"]), time.time()),
            )
            links += cur.rowcount

    await conn.commit()
    return {"pages": pages, "links": links}


async def _ensure_node(conn: Any, layer: str, user_id: str, file_path: str, importance: float) -> int:
    """Dedup by exact content within (layer, user): the wiki path IS identity."""
    cur = await conn.execute(
        "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND content=? LIMIT 1",
        (layer, user_id, file_path),
    )
    row = await cur.fetchone()
    if row:
        return int(row["node_id"])
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, confidence, created_at) VALUES (?, ?, ?, 'wiki_page', ?, ?)",
        (layer, user_id, file_path, importance, time.time()),
    )
    return int(cur.lastrowid or 0)


async def _node_id(conn: Any, layer: str, user_id: str, file_path: str) -> int | None:
    cur = await conn.execute(
        "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND content=? LIMIT 1",
        (layer, user_id, file_path),
    )
    row = await cur.fetchone()
    return int(row["node_id"]) if row else None
