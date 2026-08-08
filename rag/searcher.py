from __future__ import annotations
import logging
from typing import Literal, cast, TYPE_CHECKING

from shared.constants import DB_NAME
from rag.models import SearchResult
from rag.search import search_fts5, search_binary, search_rrf, auto_strategy

if TYPE_CHECKING:
    from shared.connection import AsyncConnectionManager
    from shared.importance import ImportanceScorer

logger = logging.getLogger(__name__)

StrategyT = Literal["fts", "mib", "hybrid", "auto"]


class RAGSearcher:
    def __init__(
        self,
        cm: AsyncConnectionManager,
        layer: str = "user",
        scorer: ImportanceScorer | None = None,
        binary_dim: int = 384,
    ):
        self._cm = cm
        self.layer = layer
        self.scorer = scorer
        self.binary_dim = binary_dim
        self._fts_available: bool | None = None

    async def _check_fts(self) -> bool:
        if self._fts_available is not None:
            return self._fts_available

        conn = await self._cm.get(DB_NAME)
        try:
            cur = await conn.execute("PRAGMA compile_options")
            options = [r[0] for r in await cur.fetchall()]
            self._fts_available = "ENABLE_FTS5" in options
        except Exception:
            self._fts_available = False
        return self._fts_available or False

    async def _search_fts5(self, query: str, user_id: str, limit: int) -> list[SearchResult]:
        fts_ready = await self._check_fts()
        raw_results = await search_fts5(self._cm, query, user_id, limit, fts_ready)
        return [
            SearchResult(
                page_id=r["id"],
                title=r["title"],
                content=r["content"],
                score=float(r["score"] or 0.0),
                metadata={"source": r["source"], "wiki_type": r.get("wiki_type")},
            )
            for r in raw_results
        ]

    async def _search_mib(self, query: str, user_id: str, limit: int) -> list[SearchResult]:
        from rag.quantize import embed_to_binary

        def default_bin_for(emb):
            return embed_to_binary(emb, threshold=0.0, dim=len(emb))

        raw_results = await search_binary(self._cm, query, user_id, limit, default_bin_for, self.binary_dim)
        return [
            SearchResult(
                page_id=r["page_id"],
                title=r["title"],
                content=r["content"],
                score=float(r["score"] or 0.0),
                metadata={"source": r["source"], "wiki_type": r.get("wiki_type")},
            )
            for r in raw_results
        ]

    async def _search_hybrid(self, query: str, user_id: str, limit: int) -> list[SearchResult]:
        if self.scorer is None:
            fts_ready = await self._check_fts()
            raw_results = await search_rrf(self._cm, query, user_id, limit, fts_available=fts_ready, binary_dim=self.binary_dim)
            return [
                SearchResult(
                    page_id=r["id"],
                    title=r["title"],
                    content=r["content"],
                    score=float(r["score"] or 0.0),
                    metadata={"source": r["source"], "wiki_type": r.get("wiki_type")},
                )
                for r in raw_results
            ]

        fts_results = await self._search_fts5(query, user_id, limit * 3)
        mib_results = await self._search_mib(query, user_id, limit * 3)

        seen = {}
        for r in fts_results + mib_results:
            pid = r.page_id
            if pid not in seen:
                seen[pid] = r
            else:
                seen[pid].score = max(seen[pid].score, r.score)

        candidates = list(seen.values())
        for c in candidates:
            res = self.scorer.score(c.content, context={"query": query})
            c.score = 0.7 * c.score + 0.3 * res.score
            c.metadata["importance_score"] = res.score

        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:limit]

    async def search(self, query: str, user_id: str = "default", strategy: StrategyT = "auto", limit: int = 10) -> list[SearchResult]:
        if strategy == "auto":
            strategy = cast("StrategyT", auto_strategy(query))

        if strategy == "fts":
            return await self._search_fts5(query, user_id, limit)
        if strategy == "mib":
            return await self._search_mib(query, user_id, limit)
        if strategy == "hybrid":
            return await self._search_hybrid(query, user_id, limit)
        raise ValueError(f"Unknown strategy: {strategy}")
