"""
RAG Engine — FTS5 + binary embeddings hybrid search.
All DB operations via AsyncConnectionManager (aiosqlite).
"""

import contextlib
import hashlib
import logging
from pathlib import Path
from typing import Any, Literal, cast

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

try:
    from rag.quantize import embed_to_binary

    _HAS_BINARY = True
except ImportError:
    _HAS_BINARY = False

StrategyT = Literal["fts", "mib", "hybrid", "auto"]


class RAGEngine:
    def __init__(
        self,
        cm: AsyncConnectionManager | None = None,
        layer: str = "user",
        binary_dim: int = 384,
        binary_threshold_mode: str = "naive",
        binary_thresholds_path: str | None = None,
        thresholds=None,
        search_strategy: StrategyT = "fts",
    ):
        self._cm = cm or connection_manager
        self.layer = layer
        self._fts_available = False
        self.binary_dim = binary_dim
        self.binary_threshold_mode = binary_threshold_mode
        self.binary_thresholds_path = binary_thresholds_path
        self._thresholds_cache = None
        self.thresholds = thresholds
        self.search_strategy: StrategyT = search_strategy
        self.scorer = None

    def _rrf_k(self) -> int:
        try:
            from config import config

            return int(config.get("rag", "rrf_k", default=60))
        except Exception:
            return 60

    def _load_thresholds(self):
        if self.binary_threshold_mode != "supervised_path":
            return None
        if self._thresholds_cache is not None:
            return self._thresholds_cache
        if not self.binary_thresholds_path:
            return None
        try:
            import numpy as np

            self._thresholds_cache = np.load(self.binary_thresholds_path)
        except (FileNotFoundError, Exception):
            return None
        return self._thresholds_cache

    def _binary_for(self, emb: list[float]) -> bytes | None:
        if not _HAS_BINARY:
            return None
        thr = self.thresholds if self.thresholds is not None else self._load_thresholds()
        if thr is not None:
            from rag.quantize import binary_from_threshold_array

            return binary_from_threshold_array(emb, thr)
        return embed_to_binary(emb, threshold=0.0, dim=len(emb))

    async def init_db(self):
        conn = await self._cm.get(DB_NAME)
        try:
            compile_options = [r[0] for r in await (await conn.execute("PRAGMA compile_options")).fetchall()]
            self._fts_available = "ENABLE_FTS5" in compile_options
        except Exception:
            self._fts_available = False

        if not self._fts_available:
            from shared.metrics import metrics

            metrics.inc("rag_fts5_unavailable_total")
            metrics.gauge("rag_fts5_enabled", 0)
            logger.warning("[rag] SQLite build lacks FTS5; lexical search will use LIKE fallback.")

        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS rag_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL DEFAULT 'user',
                user_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL, path TEXT, content TEXT NOT NULL,
                sha256_hash TEXT, wiki_type TEXT,
                created_at REAL DEFAULT (strftime('%s','now')),
                updated_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL, bin_embedding BLOB
            );
            CREATE TABLE IF NOT EXISTS rag_relations (
                source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'elaborates',
                weight REAL DEFAULT 0.8,
                PRIMARY KEY (source_id, target_id, relation_type)
            );
            CREATE INDEX IF NOT EXISTS idx_rag_user ON rag_pages(user_id);
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_bin ON rag_chunks(page_id, id) WHERE bin_embedding IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_page_idx ON rag_chunks(page_id, chunk_index);
        """,
        )
        if self._fts_available:
            with contextlib.suppress(Exception):
                await self._cm.execute_script(
                    DB_NAME,
                    "CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(title, content, wiki_type, content=rag_pages, content_rowid=id)",
                )

    async def _ingest_single_file(self, conn, page_id: int, content: str) -> int:
        from rag.chunking import chunk_text
        from shared.embeddings import embed_texts

        chunks = chunk_text(content)
        embeddings = await embed_texts(chunks)
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings, strict=False)):
            bin_blob = self._binary_for(emb) if emb and len(emb) > 0 and _HAS_BINARY else None
            await conn.execute(
                "INSERT INTO rag_chunks (page_id, chunk_index, content, bin_embedding) VALUES (?, ?, ?, ?)",
                (page_id, i, chunk, bin_blob),
            )
        return len(chunks)

    async def _insert_page(
        self, conn, title: str, content: str, user_id: str, page_hash: str, wiki_type: str | None = None, path: str = ""
    ) -> int | None:
        cur = await conn.execute("SELECT id FROM rag_pages WHERE sha256_hash = ? AND user_id = ?", (page_hash, user_id))
        existing = await cur.fetchone()
        if existing:
            return None

        cursor = await conn.execute(
            "INSERT INTO rag_pages (layer, user_id, title, path, content, sha256_hash, wiki_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.layer, user_id, title, path, content, page_hash, wiki_type),
        )
        page_id = cursor.lastrowid

        if self._fts_available:
            with contextlib.suppress(Exception):
                await conn.execute(
                    "INSERT INTO rag_fts(rowid, title, content, wiki_type) VALUES (?, ?, ?, ?)",
                    (page_id, title, content, wiki_type or ""),
                )

        await self._ingest_single_file(conn, page_id, content)
        return page_id

    async def ingest_file(self, filepath: Path, user_id: str = "default", wiki_type: str | None = None) -> str:
        content = filepath.read_text(encoding="utf-8")
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        conn = await self._cm.get(DB_NAME)

        page_id = await self._insert_page(conn, filepath.stem, content, user_id, file_hash, wiki_type, str(filepath))
        if page_id is None:
            return f"[SKIP] {filepath.name} (already ingested)"

        await conn.commit()
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
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        conn = await self._cm.get(DB_NAME)

        page_id = await self._insert_page(conn, title, text, user_id, text_hash, wiki_type, path)
        if page_id is None:
            cur = await conn.execute("SELECT id FROM rag_pages WHERE sha256_hash = ? AND user_id = ?", (text_hash, user_id))
            existing = await cur.fetchone()
            return existing[0] if existing else 0

        if relation_to is not None:
            await conn.execute(
                "INSERT OR IGNORE INTO rag_relations (source_id, target_id, relation_type) VALUES (?, ?, ?)",
                (page_id, relation_to, relation_type),
            )

        await conn.commit()
        return page_id

    async def search(self, query: str, user_id: str = "default", strategy: StrategyT | None = None, limit: int = 10) -> list[dict[str, Any]]:
        from rag.search import apply_type_boost, auto_strategy, format_result, materialize_candidates, search_binary, search_fts5, search_rrf

        strategy = strategy or self.search_strategy
        if strategy == "auto":
            strategy = cast("StrategyT", auto_strategy(query))

        if strategy == "fts":
            results = await search_fts5(self._cm, query, user_id, limit, self._fts_available)
        elif strategy == "mib":
            results = await search_binary(self._cm, query, user_id, limit, self._binary_for, self.binary_dim)
        elif strategy == "hybrid":
            fts = await search_fts5(self._cm, query, user_id, limit * 3, self._fts_available)
            mib = await search_binary(self._cm, query, user_id, limit * 3, self._binary_for, self.binary_dim)
            candidates = materialize_candidates(fts + mib)
            if self.scorer is not None:
                ranked = await self.scorer.rank(query, candidates, user_id)
                results = [format_result(c) for c in ranked][:limit]
            else:
                results = await search_rrf(self._cm, query, user_id, limit, self._rrf_k(), self._binary_for, self.binary_dim, self._fts_available)
        else:
            raise ValueError(f"unknown strategy: {strategy!r}")

        return apply_type_boost(query, results)

    async def get_relations(self, page_id: int, depth: int = 1) -> list[dict[str, Any]]:
        conn = await self._cm.get(DB_NAME)
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
        cur = await conn.execute(sql, (page_id, depth))
        rows = await cur.fetchall()
        return [{"id": r[0], "title": r[1], "relation": r[2], "weight": r[3]} for r in rows]

    async def add_relation(self, source_id: int, target_id: int, relation_type: str = "elaborates", weight: float = 0.8):
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT OR REPLACE INTO rag_relations (source_id, target_id, relation_type, weight) VALUES (?, ?, ?, ?)",
            (source_id, target_id, relation_type, weight),
        )
        await conn.commit()

    async def count_pages(self, user_id: str | None = None) -> int:
        conn = await self._cm.get(DB_NAME)
        if user_id:
            row = await (await conn.execute("SELECT COUNT(*) FROM rag_pages WHERE user_id=?", (user_id,))).fetchone()
        else:
            row = await (await conn.execute("SELECT COUNT(*) FROM rag_pages")).fetchone()
        return row[0] if row else 0

    async def count_chunks(self) -> int:
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM rag_chunks")).fetchone()
        return row[0] if row else 0
