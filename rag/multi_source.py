"""MultiSourceRAG — unified search across rag_chunks + wiki_index (FileWiki).

Repository pattern: merges results from RAG engine and Wiki search,
deduplicates by (title, content_prefix), and reranks by score.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Per-source weight overrides by intent; sources absent from an intent's
# dict keep weight 1.0.
_INTENT_WEIGHTS: dict[str, dict[str, float]] = {
    "recent": {"episodic": 1.5, "rag": 1.2, "core": 0.8},
    "core": {"core": 1.5, "wiki": 1.2, "episodic": 0.8},
}

# Disjoint id-space offsets so wiki/episodic/graph ids can never collide
# with rag_pages.id (positive) or each other.
_ID_OFFSET_WIKI = 1_000_000
_ID_OFFSET_EPISODIC = 2_000_000
_ID_OFFSET_GRAPH = 3_000_000


class MultiSourceRAG:
    def __init__(self, rag: Any, wiki: Any, cm: Any | None = None):
        self.rag = rag
        self.wiki = wiki
        self.cm = cm

    async def search(
        self,
        query: str,
        user_id: str = "default",
        limit: int | None = None,
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
        if limit is None:
            from config import config

            limit = int(config.get("rag", "search_limit", default=10))

        weights = _INTENT_WEIGHTS.get(intent, {})
        plan = [
            ("include_rag", self._from_rag),
            ("include_wiki", self._from_wiki),
            ("include_episodic", self._from_episodic),
            ("include_core", self._from_core),
            ("include_graph", self._from_graph),
        ]
        flags = {
            "include_rag": include_rag,
            "include_wiki": include_wiki,
            "include_episodic": include_episodic,
            "include_core": include_core,
            "include_graph": include_graph,
        }

        results: list[dict[str, Any]] = []
        for flag_name, fetch in plan:
            if not flags[flag_name]:
                continue
            source = flag_name.removeprefix("include_")
            try:
                results.extend(await fetch(query, user_id, limit * 2, strategy, weights.get(source, 1.0)))
            except Exception as e:
                logger.warning("%s search failed: %s", source.capitalize(), e)

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

    async def _from_rag(self, query: str, user_id: str, limit: int, strategy: str, weight: float) -> list[dict[str, Any]]:
        rag_results: list[dict[str, Any]] = await self.rag.search(query, user_id=user_id, strategy=strategy, limit=limit)
        for r in rag_results:
            r["score"] = r.get("score", 0.5) * weight
        return rag_results

    async def _from_wiki(self, query: str, user_id: str, limit: int, strategy: str, weight: float) -> list[dict[str, Any]]:
        wiki_hits: list[dict[str, Any]] = await self.wiki.search(query, limit=limit)
        # Disjoint id-space: wiki uses negative ids to avoid collision with rag_pages.id
        results: list[dict[str, Any]] = []
        for w in wiki_hits:
            results.append(
                {
                    "id": -int(w.get("entry_id", 0)) - _ID_OFFSET_WIKI,
                    "page_id": None,
                    "title": w.get("title", ""),
                    "content": w.get("content", ""),
                    "wiki_type": f"wiki:{w.get('wiki_type', 'general')}",
                    "score": float(w.get("rank") or 0.5) * weight,
                    "source": "wiki_fts",
                    "memory_kind": None,
                }
            )
        return results

    async def _from_episodic(self, query: str, user_id: str, limit: int, strategy: str, weight: float) -> list[dict[str, Any]]:
        from core.episodic import EpisodicMemory

        episodic = EpisodicMemory(cm=self.cm)
        episodes: list[Any] = await episodic.search(user_id, query, limit=limit)
        from rag.actr import actr_activation

        now = time.time()
        return [
            {
                "id": -episode.episode_id - _ID_OFFSET_EPISODIC,
                "title": f"Episode {episode.episode_id}",
                "content": episode.summary,
                "score": episode.emotional_weight * weight * (1 + 0.3 * actr_activation(now, episode.created_at, 1)),
                "source": "episodic",
                "created_at": episode.created_at,
            }
            for episode in episodes
        ]

    async def _from_core(self, query: str, user_id: str, limit: int, strategy: str, weight: float) -> list[dict[str, Any]]:
        from core.memory import CoreMemory
        from rag.actr import actr_activation

        core = CoreMemory(cm=self.cm)
        facts = await core.search(user_id, query, limit=limit)

        # ACT-R frequency: one batched recall_useful count per entry.
        from shared.constants import DB_NAME

        now = time.time()
        entry_ids = [f["entry_id"] for f in facts if f.get("entry_id")]
        freq: dict[int, int] = {}
        if entry_ids:
            conn = await core._cm.get(DB_NAME)
            ph = ",".join("?" * len(entry_ids))
            cur = await conn.execute(
                f"""SELECT target_id, COUNT(*) c FROM audit_log
                    WHERE action='recall_useful' AND layer='core_memory'
                    AND target_id IN ({ph}) GROUP BY target_id""",
                tuple(str(i) for i in entry_ids),
            )
            freq = {int(r["target_id"]): int(r["c"]) for r in await cur.fetchall()}

        return [
            {
                "id": hash(f["key"]) % 10000000,
                "title": f["key"],
                "content": f["value"],
                "score": f["importance"]
                * weight
                * (1 + 0.3 * actr_activation(now, f.get("updated_at", now), freq.get(int(f.get("entry_id", 0)), 0))),
                "source": "core",
                "entry_id": f.get("entry_id"),
            }
            for f in facts
        ]

    async def _from_graph(self, query: str, user_id: str, limit: int, strategy: str, weight: float) -> list[dict[str, Any]]:
        if not self.cm:
            return []

        # Basic graph content search via LIKE (primitive)
        from shared.constants import DB_NAME

        conn = await self.cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT node_id, content, node_type, confidence FROM epi_nodes WHERE user_id=? AND content LIKE ? LIMIT ?",
            (user_id, f"%{query}%", limit),
        )
        graph_rows = await cur.fetchall()
        return [
            {
                "id": -r["node_id"] - _ID_OFFSET_GRAPH,
                "title": f"Graph Node {r['node_id']} ({r['node_type']})",
                "content": r["content"],
                "score": float(r["confidence"]) * weight,
                "source": "graph",
            }
            for r in graph_rows
        ]
