"""A1.6: community detection over the wiki concept graph (networkx louvain).

No new dependency — networkx ships in the uv.lock transitively; lazy import.
Builds an undirected graph from epi_edges wiki_page nodes and reports
communities (clusters of related pages) for MOC/category suggestions.
"""

from __future__ import annotations

from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


async def detect_communities(
    cm: AsyncConnectionManager | None = None,
    layer: str = "user",
    user_id: str = "default",
) -> dict[str, Any]:
    """Cluster wiki_page nodes by their epi_edges connectivity.

    Returns {"communities": [{"size": N, "pages": [file_path…]}…],
    "node_count": M} — singletons are merged into "noise" buckets only in
    spirit: they appear as 1-page communities (caller may filter).
    """
    try:
        import networkx as nx
    except ImportError as exc:  # pragma: no cover — networkx rides the lockfile
        raise RuntimeError("networkx is required for community detection") from exc

    cm = cm or connection_manager
    conn = await cm.get(DB_NAME)
    rows = await (
        await conn.execute(
            "SELECT source_id, target_id, relation FROM epi_edges e"
            " JOIN epi_nodes s ON s.node_id = e.source_id AND s.layer=? AND s.user_id=? AND s.node_type='wiki_page'"
            " JOIN epi_nodes t ON t.node_id = e.target_id AND t.layer=? AND t.user_id=? AND t.node_type='wiki_page'",
            (layer, user_id, layer, user_id),
        )
    ).fetchall()

    graph = nx.Graph()
    node_path: dict[int, str] = {}
    node_rows = await (
        await conn.execute(
            "SELECT node_id, content FROM epi_nodes WHERE layer=? AND user_id=? AND node_type='wiki_page'",
            (layer, user_id),
        )
    ).fetchall()
    for nid, content in node_rows:
        node_path[int(nid)] = str(content)
        graph.add_node(int(nid))
    for src, dst, _rel in rows:
        graph.add_edge(int(src), int(dst))

    communities: list[list[str]] = []
    if graph.number_of_nodes():
        for cluster in nx.community.louvain_communities(graph, seed=42):
            members = sorted(node_path.get(n, f"?{n}") for n in cluster)
            communities.append({"size": len(members), "pages": members})
    communities.sort(key=lambda c: -c["size"])
    return {"communities": communities, "node_count": graph.number_of_nodes()}
