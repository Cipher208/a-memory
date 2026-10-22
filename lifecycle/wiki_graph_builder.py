"""A1.5: knowledge graph from wiki — entries → concepts → epistemic graph.

Builder precedent: lifecycle/graph_builder.py (episodes → entities). This
sibling walks wiki pages and lands each as an epi_nodes fact (node_type
"wiki_page", content = file_path — the wiki path IS the concept identity),
linked by wiki_links → epi_edges (relation = link_type). Idempotent: nodes
dedup by (layer, user, content); edges by the epi_edges PK.

Every write goes through the write dispatch (`shared/write_dispatch.py`), the
process's only owner of transactions — with the raw handle this module used to
take, the build held one open transaction on the SHARED connection for its whole
run and ended in `await conn.commit()`, which published whatever a handler had
left uncommitted there: the #130/#142 class ("a commit() there publishes a
handler's uncommitted row"). Each page and each edge is now its own dispatch
transaction, so the base's write lock is released between them instead of being
held for the whole build.

Wired nightly next to build_from_episodes (hooks/user_hooks._nightly).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch, get_write_dispatch

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
    dispatch = get_write_dispatch(cm)

    # The wiki index is a different database from memory.db, so its reads stay
    # OUTSIDE every dispatch transaction below: those hold the base's write lock.
    ids: dict[str, int] = {}
    for row in rows:
        file_path = str(row["file_path"])
        ids[file_path] = await _ensure_node(dispatch, layer, user_id, file_path, float(row["importance"] or 0.5))

    links = 0
    for row in rows:
        src_path = str(row["file_path"])
        src_id = ids.get(src_path)
        if src_id is None:
            continue
        for link in await wm.index.get_links(src_path):
            if link.get("direction") != "out":
                continue  # in-links land via the other page's out-links
            dst_path = str(link["path"])
            # A page beyond this run's window can still hold a node from an
            # earlier build — the edge lands for it all the same.
            dst_id = ids.get(dst_path) or await _node_id(dispatch, layer, user_id, dst_path)
            if dst_id is None:
                continue
            links += await dispatch.write(
                DB_NAME,
                "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, 0.6, ?, '[]')",
                (src_id, dst_id, str(link["link_type"]), time.time()),
            )

    return {"pages": len(rows), "links": links}


async def _ensure_node(dispatch: WriteDispatch, layer: str, user_id: str, file_path: str, importance: float) -> int:
    """Dedup by exact content within (layer, user): the wiki path IS identity.

    Read-then-write, so it runs inside one dispatch transaction — the only
    supported way to keep the two statements a unit.
    """

    async def _run(conn: Any) -> int:
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

    return await dispatch.atomic(DB_NAME, _run)


async def _node_id(dispatch: WriteDispatch, layer: str, user_id: str, file_path: str) -> int | None:
    row = await dispatch.select_one(
        DB_NAME,
        "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND content=? LIMIT 1",
        (layer, user_id, file_path),
    )
    return int(row["node_id"]) if row else None
