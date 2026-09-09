"""S17 addendum 11: HDBSCAN clustering over MIB embedding vectors (role 3).

Cross-check against louvain communities: embedding clusters confirm the graph
clusters when node pairs of a cluster lie in the same louvain community (the
share of such pairs). Reported to graph_enrich; consumer is the nightly
digest. Best-effort: sklearn absent / too few nodes → empty report with reason.
"""

from __future__ import annotations

import logging
import struct
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

_MIN_CLUSTER_NODES = 12  # HDBSCAN is stable from ~2x min_cluster_size; below that — noise
_MIN_CLUSTER_SIZE = 2
_BIT_DIM = 384  # MIB dim (hash fallback and e5-small agree; quantize will catch drift)


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _bits_from_blob(blob: bytes) -> int:
    from lifecycle.graph_miners import _bits_int as _to_int
    from rag.quantize import embed_to_binary

    floats = list(struct.unpack(f"{len(blob) // 4}f", blob[: (len(blob) // 4) * 4]))
    return _to_int(embed_to_binary(floats, dim=_BIT_DIM))


async def _load_bits(conn: Any, layer: str) -> tuple[list[int], list[int]]:
    """Return (node_ids, bits) of layer nodes with cached non-zero MIB vectors."""
    rows = await (
        await conn.execute(
            "SELECT n.node_id, n.content FROM epi_nodes n WHERE n.layer=? AND n.content NOT LIKE '[{%' AND n.content NOT LIKE '%tool_use_id%'",
            (layer,),
        )
    ).fetchall()
    from shared.embeddings import EmbeddingCache, _get_model

    cache = EmbeddingCache()
    await cache.ensure()
    tag = cache._cache_model_tag(_get_model())
    ids: list[int] = []
    bits: list[int] = []
    for r in rows:
        nid, content = int(r["node_id"]), str(r["content"])
        cached = await (
            await conn.execute("SELECT embedding FROM embedding_cache WHERE text_hash=? AND model_name=?", (cache._hash_text(content), tag))
        ).fetchone()
        if cached is None:
            continue
        blob = bytes(cached[0])
        if not blob:
            continue  # junk vector (addendum 10) — not taken into clustering
        b = _bits_from_blob(blob)
        if b:
            ids.append(nid)
            bits.append(b)
    return ids, bits


def _louvain_partition(edges: list[tuple[int, int]], ids: list[int]) -> dict[int, int]:
    """Compute louvain communities over edges between layer nodes (networkx from the lockfile)."""
    import networkx as nx  # type: ignore[import-untyped]

    g = nx.Graph()
    g.add_nodes_from(ids)
    for a, b in edges:
        g.add_edge(a, b)
    part: dict[int, int] = {}
    for idx, cluster in enumerate(nx.community.louvain_communities(g, seed=42)):
        for nid in cluster:
            part[nid] = idx
    return part


async def cluster_embeddings(
    cm: AsyncConnectionManager | None = None,
    layer: str = "user",
    user_id: str = "default",
) -> dict[str, Any]:
    """Run HDBSCAN over normalized Hamming distances of MIB bits + cross-check with louvain.

    Returns {"clusters": [{"size": N, "nodes": [id…], "louvain_agreement": F}],
    "node_count": M, "skipped": reason?}. Best-effort: empty when sklearn is
    absent or the number of cached vectors is too small.
    """
    cm = cm or connection_manager
    conn = await cm.get(DB_NAME)
    ids, bits = await _load_bits(conn, layer)
    if len(ids) < _MIN_CLUSTER_NODES:
        return {"clusters": [], "node_count": len(ids), "skipped": f"<{_MIN_CLUSTER_NODES} cached vectors"}
    try:
        from sklearn.cluster import HDBSCAN  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover — sklearn lives in dev-extra
        logger.debug("sklearn absent — HDBSCAN skipped: %s", exc)
        return {"clusters": [], "node_count": len(ids), "skipped": "no sklearn"}

    n = len(ids)
    # square matrix of pairwise normalized Hamming distances (precomputed)
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = _hamming(bits[i], bits[j]) / _BIT_DIM
            dist[i][j] = dist[j][i] = d
    labels = HDBSCAN(min_cluster_size=_MIN_CLUSTER_SIZE, metric="precomputed").fit(dist).labels_.tolist()

    ph = ",".join("?" * n)
    edge_rows = await (
        await conn.execute(
            f"SELECT source_id, target_id FROM epi_edges WHERE source_id IN ({ph}) AND target_id IN ({ph})",
            (*ids, *ids),
        )
    ).fetchall()
    try:
        louvain_of = _louvain_partition([(int(r["source_id"]), int(r["target_id"])) for r in edge_rows], ids)
    except Exception as exc:
        logger.debug("louvain side failed: %s", exc)
        louvain_of = {}

    clusters: list[dict[str, Any]] = []
    for label in sorted(set(labels)):
        if label < 0:
            continue  # noise
        members = [ids[i] for i in range(n) if labels[i] == label]
        if len(members) < _MIN_CLUSTER_SIZE:
            continue
        same = total = 0
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = louvain_of.get(members[i]), louvain_of.get(members[j])
                if a is None or b is None:
                    continue
                total += 1
                same += 1 if a == b else 0
        agreement = round(same / total, 3) if total else None
        clusters.append({"size": len(members), "nodes": members, "louvain_agreement": agreement})
    clusters.sort(key=lambda c: -c["size"])
    return {"clusters": clusters, "node_count": n}
