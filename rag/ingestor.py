import asyncio
import hashlib
from typing import Any, Optional

from rag.chunking import chunk_text
from rag.models import RAGChunk, RAGPage
from rag.quantize import binary_batch
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME
from shared.embeddings import embed_texts


class RAGIngestor:
    def __init__(self, cm: AsyncConnectionManager, layer: str = "user", binary_dim: int = 384, thresholds_cache: Any = None):
        self.cm = cm
        self.layer = layer
        self.binary_dim = binary_dim
        self.thresholds_cache = thresholds_cache

    async def ingest(self, title: str, content: str, user_id: str, wiki_type: Optional[str] = None, path: str = "") -> Optional[int]:
        # Calculate content hash
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        conn = await self.cm.get(DB_NAME)

        # Check if page exists
        cursor = await conn.execute("SELECT id FROM rag_pages WHERE sha256_hash = ? AND user_id = ?", (content_hash, user_id))
        row = await cursor.fetchone()
        if row:
            return row[0]

        # Split text into chunks
        chunks_text = chunk_text(content)
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
            page_id = row[0]

            # Batch Insertion
            chunk_data = []
            for i, (text, bin_emb) in enumerate(zip(chunks_text, bin_embeddings, strict=False)):
                chunk_data.append((page_id, i, text, bin_emb))

            await conn.executemany(
                "INSERT INTO rag_chunks (page_id, chunk_index, content, bin_embedding) VALUES (?, ?, ?, ?)",
                chunk_data,
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

        return page_id
