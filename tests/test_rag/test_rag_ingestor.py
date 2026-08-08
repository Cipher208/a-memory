import pytest
from unittest.mock import AsyncMock, patch
from rag.ingestor import RAGIngestor
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME
from rag.schema import init_rag_db
import tempfile
import os
import shutil

@pytest.fixture
async def cm():
    temp_dir = tempfile.mkdtemp()
    os.environ["MCP_MEMORY_DATA_DIR"] = temp_dir
    cm = AsyncConnectionManager(base_dir=temp_dir)
    await init_rag_db(cm, fts_available=True)
    yield cm
    await cm.close_all()
    shutil.rmtree(temp_dir)

@pytest.mark.asyncio
async def test_ingest_basic(cm):
    ingestor = RAGIngestor(cm)
    title = "Test Page"
    content = "This is a test content that should be chunked properly. " * 20 # Long enough for chunks
    user_id = "user1"

    page_id = await ingestor.ingest(title, content, user_id)
    assert page_id is not None

    conn = await cm.get(DB_NAME)
    # Verify page
    cursor = await conn.execute("SELECT * FROM rag_pages WHERE id = ?", (page_id,))
    page_row = await cursor.fetchone()
    assert page_row["title"] == title
    assert page_row["user_id"] == user_id

    # Verify chunks
    cursor = await conn.execute("SELECT COUNT(*) FROM rag_chunks WHERE page_id = ?", (page_id,))
    count_row = await cursor.fetchone()
    assert count_row[0] > 0

    # Verify unique hash skipping
    page_id_2 = await ingestor.ingest(title, content, user_id)
    assert page_id == page_id_2

    cursor = await conn.execute("SELECT COUNT(*) FROM rag_pages")
    count_row = await cursor.fetchone()
    assert count_row[0] == 1

@pytest.mark.asyncio
async def test_ingest_parallel_embeddings(cm):
    with patch("rag.ingestor.embed_texts", new_callable=AsyncMock) as mock_embed:
        # Mock embeddings to return some dummy vectors
        mock_embed.side_effect = lambda texts: [[0.1] * 384 for _ in texts]

        ingestor = RAGIngestor(cm)
        content = "Chunk 1\n\nChunk 2\n\nChunk 3" # 3 paragraphs -> 3 chunks likely
        await ingestor.ingest("Parallel Test", content, "user1")

        # Verify it was called once for all chunks
        mock_embed.assert_called_once()
        args, _ = mock_embed.call_args
        # Depending on chunker, might be 1 or more chunks.
        # But should be called with a list.
        assert isinstance(args[0], list)
        assert len(args[0]) > 0

@pytest.mark.asyncio
async def test_ingest_binarization(cm):
    ingestor = RAGIngestor(cm, binary_dim=384)
    content = "Some content for binarization test."
    page_id = await ingestor.ingest("Bin Test", content, "user1")

    conn = await cm.get(DB_NAME)
    cursor = await conn.execute("SELECT bin_embedding FROM rag_chunks WHERE page_id = ?", (page_id,))
    row = await cursor.fetchone()
    assert row is not None
    assert isinstance(row["bin_embedding"], bytes)
    assert len(row["bin_embedding"]) == 48 # 384 / 8
