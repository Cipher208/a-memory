"""MultiSourceRAG — unified search across rag_chunks + wiki_index (FileWiki).

Repository pattern: merges results from RAG engine and Wiki search,
deduplicates by (title, content_prefix), and reranks by score.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MultiSourceRAG:
    def __init__(self, rag: Any, wiki: Any, cm: Any | None = None):
        self.rag = rag
        self.wiki = wiki
        self.cm = cm

    async def search(
        self,
        query: str,
        user_id: str = "default",
        limit: int = 10,
        include_rag: bool = True,
        include_wiki: bool = True,
        strategy: str = "hybrid",
    ) -> list[dict[str, Any]]:
        """Search across RAG and Wiki sources, merge and deduplicate.

        Args:
            query: Search query
            user_id: User identifier
            limit: Max results to return
            include_rag: Include RAG results (default True)
            include_wiki: Include Wiki results (default True)
            strategy: RAG search strategy (fts, mib, hybrid, auto)
        """
        results: list[dict[str, Any]] = []

        if include_rag:
            try:
                rag_results = await self.rag.search(query, user_id=user_id, strategy=strategy, limit=limit * 2)
                results.extend(rag_results)
            except Exception as e:
                logger.warning("RAG search failed: %s", e)

        if include_wiki:
            try:
                wiki_hits = await self.wiki.search(query, user_id=user_id, limit=limit * 2)
                for w in wiki_hits:
                    # Disjoint id-space: wiki uses negative ids to avoid collision with rag_pages.id
                    results.append(
                        {
                            "id": -int(w.get("entry_id", 0)),
                            "page_id": None,
                            "title": w.get("title", ""),
                            "content": w.get("content", ""),
                            "wiki_type": f"wiki:{w.get('wiki_type', 'general')}",
                            "score": float(w.get("rank") or 0.5),
                            "source": "wiki_fts",
                            "memory_kind": None,
                        }
                    )
            except Exception as e:
                logger.warning("Wiki search failed: %s", e)

        # Dedup by (title, content_prefix) — RAG + Wiki may store same record twice
        dedup: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for r in results:
            key = (r.get("title", ""), (r.get("content") or "")[:200])
            if key in seen:
                continue
            seen.add(key)
            dedup.append(r)

        # Rerank: priority — explicit score; degraded (None) → 0
        dedup.sort(key=lambda r: -(r.get("score") or 0.0))
        return dedup[:limit]
