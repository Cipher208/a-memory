"""MultiSourceRAG — unified search across rag_chunks + wiki_index (FileWiki).

Repository pattern: merges results from RAG engine and Wiki search,
deduplicates by (title, content_prefix), and reranks by score.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# Entity source (S10, 6th RRF source): query tokens resolved through synonym
# canon-classes (rag/synonyms) → nodes whose content contains ANY class member.
# Deterministic, LLM-free; fixed score below typical FTS hits.
_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+")
_ENTITY_SCORE = 0.45

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

# E15: memory_type weights — working-memory kinds outrank plain facts.
# Overridable via config `retrieval.kind_weights` (merged over defaults).
_KIND_WEIGHT_DEFAULTS: dict[str, float] = {"instruction": 1.1, "rule": 1.1, "commitment": 1.1}


def _kind_weight(kind: str | None) -> float:
    from config import config

    overrides = config.get("retrieval", "kind_weights", default=None) or {}
    table = {**_KIND_WEIGHT_DEFAULTS, **overrides}
    return float(table.get(str(kind or "fact"), 1.0))


_ACTR_FLOOR = 1.0
_ACTR_CEIL = 1.3  # текущий максимум (1 + 0.3 * actr) сохраняется для top-факта


def _minmax_actr(activations: list[float]) -> list[float]:
    """Per-query min-max ACT-R множителей в [_ACTR_FLOOR, _ACTR_CEIL] (S18, v17 #5).

    Без min-max факт с actr=0 и actr=1 различаются в 1.3× только в одном
    множителе поверх разных importance — разброс немасштабируем. Min-max
    держит span [1.0, 1.3] относительно ЛУЧШЕГО факта выдачи: топ всегда
    получает максимум, худший — 1.0 (нейтрально, без наказания).
    Вырожденный случай (все равны / пусто) → нейтральный 1.0.
    """
    if not activations:
        return []
    lo, hi = min(activations), max(activations)
    if hi - lo < 1e-12:
        return [_ACTR_FLOOR] * len(activations)
    span = _ACTR_CEIL - _ACTR_FLOOR
    return [_ACTR_FLOOR + span * (a - lo) / (hi - lo) for a in activations]


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
        include_entities: bool | None = None,
        strategy: str = "hybrid",
        intent: str = "balanced",
    ) -> list[dict[str, Any]]:
        """Search across RAG, Wiki, Episodic, Core, Graph and Entity sources, merge and deduplicate.

        Args:
            query: Search query
            user_id: User identifier
            limit: Max results to return
            include_rag: Include RAG results (default True)
            include_wiki: Include Wiki results (default True)
            include_episodic: Include L3 episodic results (default True)
            include_core: Include L4 core results (default True)
            include_graph: Include Graph results (default True)
            include_entities: Include entity-expanded results (default: config rag.entity_rrf, true)
            strategy: RAG search strategy (fts, mib, hybrid, auto)
            intent: weight bias ("recent", "core", "balanced")

        """
        if limit is None:
            from config import config

            limit = int(config.get("rag", "search_limit", default=10))
        if include_entities is None:
            from config import config

            include_entities = bool(config.get("rag", "entity_rrf", default=True))

        weights = _INTENT_WEIGHTS.get(intent, {})
        plan = [
            ("include_rag", self._from_rag),
            ("include_wiki", self._from_wiki),
            ("include_episodic", self._from_episodic),
            ("include_core", self._from_core),
            ("include_graph", self._from_graph),
            ("include_entities", self._from_entities),
        ]
        flags = {
            "include_rag": include_rag,
            "include_wiki": include_wiki,
            "include_episodic": include_episodic,
            "include_core": include_core,
            "include_graph": include_graph,
            "include_entities": include_entities,
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

        # GraphRAG (B1.6): expand primary graph hits with 1-hop neighbors.
        results = await self._expand_graph(results, user_id)

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
        # S18 п.1: per-query min-max ACT-R — топ-эпизод получает ceil, худший 1.0.
        acts = [actr_activation(now, episode.created_at, 1) for episode in episodes]
        mult = _minmax_actr(acts)
        return [
            {
                "id": -episode.episode_id - _ID_OFFSET_EPISODIC,
                "title": f"Episode {episode.episode_id}",
                "content": episode.summary,
                "score": episode.emotional_weight * weight * mult[i],
                "source": "episodic",
                "created_at": episode.created_at,
            }
            for i, episode in enumerate(episodes)
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

        # S18 п.1: per-query min-max ACT-R — топ-факт получает ceil (1.3),
        # худший нейтральный floor (1.0) относительно этой выдачи.
        acts = [actr_activation(now, f.get("updated_at", now), freq.get(int(f.get("entry_id", 0)), 0)) for f in facts]
        actr_mult = _minmax_actr(acts)

        return [
            {
                "id": hash(f["key"]) % 10000000,
                "title": f["key"],
                "content": f["value"],
                "score": f["importance"] * weight * _kind_weight(f.get("memory_kind")) * actr_mult[i],
                "source": "core",
                "entry_id": f.get("entry_id"),
            }
            for i, f in enumerate(facts)
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

    async def _from_entities(self, query: str, user_id: str, limit: int, strategy: str, weight: float) -> list[dict[str, Any]]:
        """Entity-expanded source (S10 6th): synonym canon-classes → epi_nodes LIKE.

        Query tokens that belong to a synonym class (rag/synonyms) expand to ALL
        class members; nodes whose content contains ANY member are candidates
        (deterministic, LLM-free). Fixed score sits below typical FTS hits — the
        source ADDS candidates to the fusion, it never replaces direct hits.
        """
        if not self.cm:
            return []
        from rag.synonyms import load_synonyms

        syn = load_synonyms()
        members: set[str] = set()
        for tok in _TOKEN_RE.findall(query.lower()):
            if len(tok) < 4 or (tok not in syn and not any(tok in vs for vs in syn.values())):
                continue
            # класс токена — тот же набор, из которого canonical_form берёт минимум
            members |= {tok, *syn.get(tok, []), *(k for k, vs in syn.items() if tok in vs)}
        if not members:
            return []

        from shared.constants import DB_NAME

        conn = await self.cm.get(DB_NAME)
        likes = " OR ".join("content LIKE ?" for _ in members)
        cur = await conn.execute(
            f"SELECT node_id, content, node_type, confidence FROM epi_nodes WHERE user_id=? AND ({likes}) LIMIT ?",
            (user_id, *(f"%{m}%" for m in sorted(members)), limit * 2),
        )
        return [
            {
                "id": -r["node_id"] - _ID_OFFSET_GRAPH,
                "title": f"Graph Node {r['node_id']} ({r['node_type']})",
                "content": r["content"],
                "score": _ENTITY_SCORE * weight,
                "source": "entities",
            }
            for r in await cur.fetchall()
        ]

    async def _expand_graph(self, results: list[dict[str, Any]], user_id: str) -> list[dict[str, Any]]:
        """GraphRAG stage (B1.6): append 1-hop epi_edges neighbors of graph hits.

        Neighbors enter the rerank with a damped score (0.5 * edge weight *
        confidence) under source="graph_expand". Primary results pass through
        untouched; no-op without a cm or without graph hits.
        """
        if not self.cm:
            return results
        node_ids = [
            -r["id"] - _ID_OFFSET_GRAPH
            for r in results
            if r.get("source") == "graph" and isinstance(r.get("id"), int) and r["id"] <= -_ID_OFFSET_GRAPH
        ]
        if not node_ids:
            return results

        from shared.constants import DB_NAME

        conn = await self.cm.get(DB_NAME)
        ph = ",".join("?" * len(node_ids))
        cur = await conn.execute(
            f"""SELECT n.node_id, n.content, n.node_type, n.confidence, e.weight
                FROM epi_edges e
                JOIN epi_nodes n
                  ON (n.node_id = e.target_id AND e.source_id IN ({ph}))
                  OR (n.node_id = e.source_id AND e.target_id IN ({ph}))
                WHERE n.user_id=?""",
            (*node_ids, *node_ids, user_id),
        )
        existing = {r.get("id") for r in results}
        for row in await cur.fetchall():
            rid = -int(row["node_id"]) - _ID_OFFSET_GRAPH
            if rid in existing:
                continue
            existing.add(rid)
            results.append(
                {
                    "id": rid,
                    "title": f"Graph Node {row['node_id']} ({row['node_type']})",
                    "content": row["content"],
                    "score": 0.5 * float(row["weight"]) * float(row["confidence"]),
                    "source": "graph_expand",
                }
            )
        return results
