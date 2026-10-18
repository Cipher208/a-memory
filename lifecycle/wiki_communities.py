"""A1.6: community detection over the wiki concept graph (networkx louvain).

No new dependency — networkx ships in the uv.lock transitively; lazy import.
Builds an undirected graph from epi_edges wiki_page nodes and reports
communities (clusters of related pages) for MOC/category suggestions.
"""

from __future__ import annotations

from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import get_write_dispatch


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
        import networkx as nx  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover — networkx rides the lockfile
        raise RuntimeError("networkx is required for community detection") from exc

    cm = cm or connection_manager
    dispatch = get_write_dispatch(cm)
    rows = await dispatch.query(
        DB_NAME,
        "SELECT source_id, target_id, relation FROM epi_edges e"
        " JOIN epi_nodes s ON s.node_id = e.source_id AND s.layer=? AND s.user_id=? AND s.node_type='wiki_page'"
        " JOIN epi_nodes t ON t.node_id = e.target_id AND t.layer=? AND t.user_id=? AND t.node_type='wiki_page'",
        (layer, user_id, layer, user_id),
    )

    graph = nx.Graph()
    node_path: dict[int, str] = {}
    node_rows = await dispatch.query(
        DB_NAME,
        "SELECT node_id, content FROM epi_nodes WHERE layer=? AND user_id=? AND node_type='wiki_page'",
        (layer, user_id),
    )
    for r in node_rows:
        node_path[int(r["node_id"])] = str(r["content"])
        graph.add_node(int(r["node_id"]))
    for r in rows:
        graph.add_edge(int(r["source_id"]), int(r["target_id"]))

    communities: list[dict[str, Any]] = []
    if graph.number_of_nodes():
        for cluster in nx.community.louvain_communities(graph, seed=42):
            members = sorted(node_path.get(n, f"?{n}") for n in cluster)
            communities.append({"size": len(members), "pages": members})
    communities.sort(key=lambda c: -c["size"])
    return {"communities": communities, "node_count": graph.number_of_nodes()}
