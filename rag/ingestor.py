from __future__ import annotations
import hashlib
from typing import Any, TYPE_CHECKING

from rag.chunking import chunk_text
from rag.models import RAGPage
from rag.quantize import binary_batch
from shared.constants import DB_NAME
from shared.embeddings import embed_texts

if TYPE_CHECKING:
    from shared.connection import AsyncConnectionManager


class RAGIngestor:
    def __init__(self, cm: AsyncConnectionManager, layer: str = "user", binary_dim: int = 384, thresholds_cache: Any = None):
        self.cm = cm
        self.layer = layer
        self.binary_dim = binary_dim
        self.thresholds_cache = thresholds_cache

    async def ingest(self, title: str, content: str, user_id: str, wiki_type: str | None = None, path: str = "") -> int | None:
        # Calculate content hash
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        conn = await self.cm.get(DB_NAME)

        # Check if page exists — scoped to this layer so the same content
        # can legitimately exist in user and agent layers
        cursor = await conn.execute(
            "SELECT id FROM rag_pages WHERE sha256_hash = ? AND user_id = ? AND layer = ?",
            (content_hash, user_id, self.layer),
        )
        row = await cursor.fetchone()
        if row:
            return int(row[0]) if row[0] is not None else None

        # Split text into chunks
        from config import config

        chunks_text = chunk_text(
            content,
            max_size=int(config.get("rag", "chunk_size", default=500)),
            overlap=int(config.get("rag", "chunk_overlap", default=100)),
        )
        if not chunks_text:
            return None

        # Create RAGPage model
        page = RAGPage(
            layer=self.layer,
            user_id=user_id,
            title=title,
            path=path,
            content=content,
            sha256_hash=content_hash,
            wiki_type=wiki_type,
        )

        # Insert page and chunks in a transaction manually if needed,
        # but aiosqlite connection itself isn't a context manager for transactions.
        # It's a context manager for the connection itself.

        # Parallelism: Embed all chunks at once
        embeddings = await embed_texts(chunks_text)

        # Binarize embeddings
        bin_embeddings = binary_batch(embeddings, thresholds=self.thresholds_cache, dim=self.binary_dim)

        # Start transaction
        await conn.execute("BEGIN TRANSACTION")
        try:
            # Insert page
            await conn.execute(
                """
                INSERT INTO rag_pages (layer, user_id, title, path, content, sha256_hash, wiki_type, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    page.layer,
                    page.user_id,
                    page.title,
                    page.path,
                    page.content,
                    page.sha256_hash,
                    page.wiki_type,
                    page.created_at,
                    page.updated_at,
                ),
            )

            cursor = await conn.execute("SELECT last_insert_rowid()")
            row = await cursor.fetchone()
            page_id = int(row[0]) if row and row[0] is not None else 0

            # Batch Insertion. Float blobs (dense embeddings) are kept only when
            # rag.storage.keep_float_blobs is on — they exist for supervised
            # threshold training; search runs purely on binary embeddings.
            from config import config

            keep_floats = bool(config.get("rag", "storage", "keep_float_blobs", default=True))
            import numpy as np

            def _row(i: int, text: str, bin_emb: bytes) -> tuple[int, int, str, bytes, bytes | None]:
                float_blob = np.asarray(embeddings[i], dtype=np.float32).tobytes() if keep_floats else None
                return (page_id, i, text, bin_emb, float_blob)

            chunk_data = [_row(i, t, b) for i, (t, b) in enumerate(zip(chunks_text, bin_embeddings, strict=False))]

            await conn.executemany(
                "INSERT INTO rag_chunks (page_id, chunk_index, content, bin_embedding, float_embedding) VALUES (?, ?, ?, ?, ?)",
                chunk_data,
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

        return page_id
