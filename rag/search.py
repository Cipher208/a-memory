"""RAG search strategies — FTS5, binary, hybrid, RRF."""

import logging
from contextlib import suppress
from typing import Any
from collections.abc import Callable

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

try:
    from rag.quantize import hamming_distance, hamming_to_score

    _HAS_BINARY = True
except ImportError:
    _HAS_BINARY = False


async def search_fts5(
    cm: AsyncConnectionManager, query: str, user_id: str, limit: int, fts_available: bool, layer: str = "user"
) -> list[dict[str, Any]]:
    """FTS5 search with LIKE fallback. Layer-scoped: never returns other layers' pages."""
    conn = await cm.get(DB_NAME)
    if fts_available:
        try:
            cur = await conn.execute(
                """SELECT wp.id, wp.title, wp.content, wp.wiki_type, fts.rank
                   FROM rag_fts fts JOIN rag_pages wp ON fts.rowid = wp.id
                   WHERE rag_fts MATCH ? AND wp.user_id = ? AND wp.layer = ?
                   ORDER BY fts.rank DESC LIMIT ?""",
                (query, user_id, layer, limit),
            )
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0],
                    "title": r[1],
                    "content": r[2][:500] + "..." if len(r[2]) > 500 else r[2],
                    "wiki_type": r[3],
                    "score": abs(r[4]) if r[4] else 0.0,
                    "source": "fts5",
                }
                for r in rows
            ]
        except Exception as e:
            # FTS5 syntax errors (special chars in query) degrade to LIKE — say so.
            logger.warning("FTS5 query failed (%s) — falling back to LIKE", e)

    escaped_query = query.replace("%", "\\%").replace("_", "\\_")
    cur = await conn.execute(
        "SELECT id, title, content, wiki_type FROM rag_pages WHERE user_id=? AND layer=? AND (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\') LIMIT ?",
        (user_id, layer, f"%{escaped_query}%", f"%{escaped_query}%", limit),
    )
    rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "page_id": r[0],
            "title": r[1] or "",
            "content": r[2] or "",
            "wiki_type": r[3],
            "score": None,
            "source": "fts5_like_fallback",
        }
        for r in rows
    ]


async def search_binary(
    cm: AsyncConnectionManager,
    query: str,
    user_id: str,
    limit: int,
    binary_for_fn: Callable[[list[float]], bytes],
    binary_dim: int,
    layer: str = "user",
) -> list[dict[str, Any]]:
    """Exhaustive linear scan over binary embeddings. Layer-scoped."""
    if not _HAS_BINARY:
        return []

    from shared.embeddings import embed_text

    q_emb = await embed_text(query)
    q_bin = binary_for_fn(q_emb)
    if q_bin is None:
        return []

    conn = await cm.get(DB_NAME)
    cursor = await conn.execute(
        """
        SELECT c.id, c.page_id, c.content, c.bin_embedding,
               p.title, p.wiki_type
        FROM rag_chunks c
        JOIN rag_pages p ON p.id = c.page_id
        WHERE p.user_id = ?
          AND p.layer = ?
          AND c.bin_embedding IS NOT NULL
        """,
        (user_id, layer),
    )

    scored = []
    BATCH_SIZE = 1000
    while True:
        rows = await cursor.fetchmany(BATCH_SIZE)
        if not rows:
            break
        for r in rows:
            d = hamming_distance(q_bin, r["bin_embedding"])
            scored.append(
                {
                    "id": r["id"],
                    "page_id": r["page_id"],
                    "title": r["title"],
                    "content": r["content"][:1024],
                    "wiki_type": r["wiki_type"],
                    "score": hamming_to_score(d, binary_dim),
                    "source": "mib",
                }
            )
    scored.sort(key=lambda x: (-x["score"], x["id"]))
    return scored[:limit]


async def search_rrf(
    cm: AsyncConnectionManager,
    query: str,
    user_id: str,
    limit: int,
    k: int = 60,
    binary_for_fn: Callable[[list[float]], bytes] | None = None,
    binary_dim: int = 384,
    fts_available: bool = True,
    layer: str = "user",
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion — merge FTS5 and binary results."""
    fts_results = await search_fts5(cm, query, user_id, limit=limit * 3, fts_available=fts_available, layer=layer)
    fts_ranks = {doc["id"]: rank for rank, doc in enumerate(fts_results)}

    bin_ranks = {}
    if binary_for_fn:
        with suppress(Exception):
            bin_results = await search_binary(cm, query, user_id, limit * 3, binary_for_fn, binary_dim, layer=layer)
            bin_ranks = {r["id"]: rank for rank, r in enumerate(bin_results)}

    merged = _calculate_rrf_scores(fts_ranks, bin_ranks, k)
    sorted_ids = sorted(merged.keys(), key=lambda x: -merged[x])[:limit]
    if not sorted_ids:
        return []

    return await _fetch_rrf_results(cm, sorted_ids, merged, fts_ranks, bin_ranks)


def _calculate_rrf_scores(fts_ranks: dict[int, int], bin_ranks: dict[int, int], k: int) -> dict[int, float]:
    """Calculate merged RRF scores."""

    def rrf(rank: int) -> float:
        return 1.0 / (k + rank + 1)

    merged = {}
    for doc_id in set(fts_ranks.keys()) | set(bin_ranks.keys()):
        score = 0.0
        if doc_id in fts_ranks:
            score += rrf(fts_ranks[doc_id])
        if doc_id in bin_ranks:
            score += rrf(bin_ranks[doc_id])
        merged[doc_id] = score
    return merged


async def _fetch_rrf_results(
    cm: AsyncConnectionManager,
    sorted_ids: list[int],
    merged: dict[int, float],
    fts_ranks: dict[int, int],
    bin_ranks: dict[int, int],
) -> list[dict[str, Any]]:
    """Fetch final metadata for RRF results from DB."""
    conn = await cm.get(DB_NAME)
    placeholders = ",".join(["?"] * len(sorted_ids))
    # skylos: ignore [SKY-D211] - Static ID list from internal merged ranks.
    sql = f"SELECT id, title, content, wiki_type FROM rag_pages WHERE id IN ({placeholders})"
    cur = await conn.execute(sql, tuple(sorted_ids))
    rows = await cur.fetchall()
    by_id = {r[0]: r for r in rows}

    results = []
    for doc_id in sorted_ids:
        row = by_id.get(doc_id)
        if row:
            source = _determine_source(doc_id, fts_ranks, bin_ranks)
            content = row[2]
            results.append(
                {
                    "id": row[0],
                    "title": row[1],
                    "content": content[:500] + "..." if len(content) > 500 else content,
                    "wiki_type": row[3],
                    "score": merged[doc_id],
                    "source": source,
                }
            )
    return results


def _determine_source(doc_id: int, fts_ranks: dict[int, int], bin_ranks: dict[int, int]) -> str:
    has_fts = doc_id in fts_ranks
    has_bin = doc_id in bin_ranks
    return "rrf(fts+mib)" if (has_fts and has_bin) else ("fts5" if has_fts else "mib")


def auto_strategy(query: str) -> str:
    """Pick strategy based on query length."""
    if len(query.split()) <= 2:
        return "fts"
    return "hybrid"


def materialize_candidates(results: list[dict[str, Any]]) -> list[Any]:
    """Convert raw search dicts to ScoredCandidate objects for the Scorer."""
    from rag.scoring import ScoredCandidate

    seen: dict[int, ScoredCandidate] = {}
    for r in results:
        rid = r["id"]
        if rid in seen:
            existing = seen[rid]
            if r.get("source") == "mib" and existing.bin_score is None:
                existing.bin_score = r["score"]
            if r["score"] is not None:
                existing.rrf_score = max(existing.rrf_score or 0.0, r["score"])
        else:
            seen[rid] = ScoredCandidate(
                id=rid,
                page_id=r.get("page_id", rid),
                title=r["title"],
                content=r["content"],
                wiki_type=r.get("wiki_type"),
                rrf_score=r["score"] or 0.0,
                bin_score=r["score"] if r.get("source") == "mib" else None,
                source=r.get("source", ""),
            )
    return list(seen.values())


def format_result(c: Any) -> dict[str, Any]:
    """Convert a ScoredCandidate back to a result dict."""
    content: str = str(c.content)
    if len(content) > 500:
        content = content[:500] + "..."
    return {
        "id": int(c.id),
        "title": str(c.title),
        "content": content,
        "wiki_type": str(c.wiki_type),
        "score": float(c.final_score or c.rrf_score),
        "source": str(c.source),
    }
