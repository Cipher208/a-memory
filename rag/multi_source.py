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
        include_episodic: bool = True,
        include_core: bool = True,
        include_graph: bool = True,
        strategy: str = "hybrid",
        intent: str = "balanced",
    ) -> list[dict[str, Any]]:
        """Search across RAG, Wiki, Episodic, Core and Graph sources, merge and deduplicate.

        Args:
            query: Search query
            user_id: User identifier
            limit: Max results to return
            include_rag: Include RAG results (default True)
            include_wiki: Include Wiki results (default True)
            include_episodic: Include L3 episodic results (default True)
            include_core: Include L4 core results (default True)
            include_graph: Include Graph results (default True)
            strategy: RAG search strategy (fts, mib, hybrid, auto)
            intent: weight bias ("recent", "core", "balanced")

        """
        results: list[dict[str, Any]] = []

        # Weights based on intent
        w_rag = 1.0
        w_wiki = 1.0
        w_episodic = 1.0
        w_core = 1.0
        w_graph = 1.0

        if intent == "recent":
            w_episodic = 1.5
            w_rag = 1.2
            w_core = 0.8
        elif intent == "core":
            w_core = 1.5
            w_wiki = 1.2
            w_episodic = 0.8

        if include_rag:
            try:
                rag_results = await self.rag.search(query, user_id=user_id, strategy=strategy, limit=limit * 2)
                for r in rag_results:
                    r["score"] = r.get("score", 0.5) * w_rag
                results.extend(rag_results)
            except Exception as e:
                logger.warning("RAG search failed: %s", e)

        if include_wiki:
            try:
                wiki_hits = await self.wiki.search(query, limit=limit * 2)
                for w in wiki_hits:
                    # Disjoint id-space: wiki uses negative ids to avoid collision with rag_pages.id
                    results.append(
                        {
                            "id": -int(w.get("entry_id", 0)) - 1000000,
                            "page_id": None,
                            "title": w.get("title", ""),
                            "content": w.get("content", ""),
                            "wiki_type": f"wiki:{w.get('wiki_type', 'general')}",
                            "score": float(w.get("rank") or 0.5) * w_wiki,
                            "source": "wiki_fts",
                            "memory_kind": None,
                        }
                    )
            except Exception as e:
                logger.warning("Wiki search failed: %s", e)

        if include_episodic:
            try:
                from core.episodic import EpisodicMemory
                episodic = EpisodicMemory(cm=self.cm)
                episodes = await episodic.search(user_id, query, limit=limit * 2)
                for episode in episodes:
                    results.append({
                        "id": -episode.episode_id - 2000000,
                        "title": f"Episode {episode.episode_id}",
                        "content": episode.summary,
                        "score": episode.emotional_weight * w_episodic,
                        "source": "episodic",
                        "created_at": episode.created_at
                    })
            except Exception as e:
                logger.warning("Episodic search failed: %s", e)

        if include_core:
            try:
                from core.memory import CoreMemory
                core = CoreMemory(cm=self.cm)
                facts = await core.search(user_id, query, limit=limit * 2)
                for f in facts:
                    results.append({
                        "id": hash(f["key"]) % 10000000,
                        "title": f["key"],
                        "content": f["value"],
                        "score": f["importance"] * w_core,
                        "source": "core",
                    })
            except Exception as e:
                logger.warning("Core search failed: %s", e)

        if include_graph and self.cm:
            try:
                # Basic graph content search via LIKE (primitive)
                from shared.constants import DB_NAME
                conn = await self.cm.get(DB_NAME)
                cur = await conn.execute(
                    "SELECT node_id, content, node_type, confidence FROM epi_nodes WHERE user_id=? AND content LIKE ? LIMIT ?",
                    (user_id, f"%{query}%", limit * 2)
                )
                graph_rows = await cur.fetchall()
                for r in graph_rows:
                    results.append({
                        "id": -r["node_id"] - 3000000,
                        "title": f"Graph Node {r['node_id']} ({r['node_type']})",
                        "content": r["content"],
                        "score": float(r["confidence"]) * w_graph,
                        "source": "graph",
                    })
            except Exception as e:
                logger.warning("Graph search failed: %s", e)

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
