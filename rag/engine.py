"""RAG Engine Facade — Unified semantic and keyword search.

Orchestrates ingestion, binarization, and multi-strategy retrieval
across all memory layers with high-performance async I/O.
"""

from __future__ import annotations

"""
RAG Engine — Unified facade for Ingestor and Searcher.
Maintains backward compatibility for legacy consumers.
"""

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import get_write_dispatch
from rag.schema import init_rag_db
from rag.ingestor import RAGIngestor
from rag.searcher import RAGSearcher, StrategyT
from shared.importance import ImportanceScorer

if TYPE_CHECKING:
    from pathlib import Path
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class RAGEngine:
    def __init__(
        self,
        cm: AsyncConnectionManager | None = None,
        layer: str = "user",
        binary_dim: int | None = None,
        binary_threshold_mode: str | None = None,
        binary_thresholds_path: str | None = None,
        thresholds: Any = None,
        search_strategy: StrategyT = "fts",
    ) -> None:
        self._cm = cm or connection_manager
        self.layer = layer
        # yaml `binary:` section is the source of truth; args override
        from config import config as _cfg

        self.binary_dim = binary_dim if binary_dim is not None else int(_cfg.get("binary", "dim", default=384))
        self.binary_threshold_mode = binary_threshold_mode or str(_cfg.get("binary", "mode", default="naive"))
        self.binary_thresholds_path = binary_thresholds_path or _cfg.get("binary", "thresholds_path")
        self.thresholds = thresholds
        self.search_strategy: StrategyT = search_strategy

        # Load thresholds if needed
        self._thresholds_cache = self._load_thresholds()

        # Initialize Scorer
        self.scorer = self._init_scorer()

        # Initialize Ingestor and Searcher
        self.ingestor = RAGIngestor(
            cm=self._cm, layer=self.layer, binary_dim=self.binary_dim, thresholds_cache=self._thresholds_cache or self.thresholds
        )
        self.searcher = RAGSearcher(cm=self._cm, layer=self.layer, scorer=self.scorer, binary_dim=self.binary_dim)

    def _binary_for(self, emb: Sequence[float]) -> bytes:
        """Binarize using current thresholds. Mostly for internal use and tests."""
        from rag.quantize import embed_to_binary, binary_from_threshold_array

        thr = self._thresholds_cache or self.thresholds
        if thr is not None:
            return binary_from_threshold_array(emb, thr)
        return embed_to_binary(emb, threshold=0.0, dim=self.binary_dim)

    def _load_thresholds(self) -> Any | None:
        if self.binary_threshold_mode != "supervised_path" or not self.binary_thresholds_path:
            return None
        try:
            import numpy as np

            return np.load(self.binary_thresholds_path)
        except (FileNotFoundError, Exception) as e:
            logger.warning(f"[rag] Failed to load thresholds from {self.binary_thresholds_path}: {e}")
            return None

    def _init_scorer(self) -> ImportanceScorer | None:
        try:
            return ImportanceScorer()
        except Exception as e:
            logger.debug(f"[rag] ImportanceScorer not available: {e}")
            return None

    async def init_db(self) -> None:
        """Delegate to rag.schema.init_rag_db."""
        fts_available = await self.searcher._check_fts()
        await init_rag_db(self._cm, fts_available)

    async def ingest_file(self, filepath: Path, user_id: str = "default", wiki_type: str | None = None) -> str:
        """Delegate to ingestor."""
        content = await asyncio.to_thread(filepath.read_text, encoding="utf-8")
        page_id = await self.ingestor.ingest(title=filepath.stem, content=content, user_id=user_id, wiki_type=wiki_type, path=str(filepath))
        if page_id is None:
            return f"[SKIP] {filepath.name} (already ingested or empty)"
        return f"[OK] {filepath.name}"

    async def ingest_text(
        self,
        title: str,
        text: str,
        user_id: str = "default",
        wiki_type: str | None = None,
        path: str = "",
        relation_to: int | None = None,
        relation_type: str = "elaborates",
    ) -> int:
        """Delegate to ingestor."""
        page_id = await self.ingestor.ingest(title=title, content=text, user_id=user_id, wiki_type=wiki_type, path=path)

        if page_id and relation_to is not None:
            await self.add_relation(page_id, relation_to, relation_type)

        return page_id or 0

    async def search(self, query: str, user_id: str = "default", strategy: StrategyT | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Delegate to searcher.search().

        Converts SearchResult models back to dict for backward compatibility.
        """
        results = await self.searcher.search(query=query, user_id=user_id, strategy=strategy or self.search_strategy, limit=limit)

        # Compatibility layer: convert models to dicts
        return [
            {
                "id": r.page_id,
                "title": r.title,
                "content": r.content,
                "score": r.score,
                "source": r.metadata.get("source", ""),
                "wiki_type": r.metadata.get("wiki_type"),
            }
            for r in results
        ]

    async def get_relations(self, page_id: int, depth: int = 1) -> list[dict[str, Any]]:
        dispatch = get_write_dispatch(self._cm)
        sql = """
        WITH RECURSIVE graph AS (
            SELECT r.source_id, r.target_id, r.relation_type, r.weight, 1 as d
            FROM rag_relations r WHERE r.source_id = ?
            UNION ALL
            SELECT r.source_id, r.target_id, r.relation_type, r.weight, g.d + 1
            FROM rag_relations r JOIN graph g ON r.source_id = g.target_id WHERE g.d < ?
        )
        SELECT wp.id, wp.title, g.relation_type, g.weight
        FROM graph g JOIN rag_pages wp ON g.target_id = wp.id
        """
        rows = await dispatch.query(DB_NAME, sql, (page_id, depth))
        return [{"id": r["id"], "title": r["title"], "relation": r["relation_type"], "weight": r["weight"]} for r in rows]

    async def add_relation(self, source_id: int, target_id: int, relation_type: str = "elaborates", weight: float = 0.8) -> None:
        await get_write_dispatch(self._cm).write(
            DB_NAME,
            "INSERT OR REPLACE INTO rag_relations (source_id, target_id, relation_type, weight) VALUES (?, ?, ?, ?)",
            (source_id, target_id, relation_type, weight),
        )

    async def count_pages(self, user_id: str | None = None) -> int:
        dispatch = get_write_dispatch(self._cm)
        if user_id:
            total = await dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM rag_pages WHERE user_id=?", (user_id,))
        else:
            total = await dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM rag_pages")
        return int(total or 0)

    async def count_chunks(self) -> int:
        total = await get_write_dispatch(self._cm).scalar(DB_NAME, "SELECT COUNT(*) FROM rag_chunks")
        return int(total or 0)
