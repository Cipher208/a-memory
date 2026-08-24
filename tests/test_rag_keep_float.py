"""rag.storage.keep_float_blobs flag: float blobs stored only when enabled."""

import pytest

from rag.ingestor import RAGIngestor
from rag.schema import init_rag_db
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def cm(tmp_path):
    manager = AsyncConnectionManager(base_dir=tmp_path)
    await init_rag_db(manager, fts_available=False)
    return manager


def _set_flag(monkeypatch, value: bool) -> None:
    from config import config

    # patch the SINGLETON instance — _load() set an instance-level _data
    monkeypatch.setattr(config, "_data", {"rag": {"storage": {"keep_float_blobs": value}}}, raising=False)


@pytest.mark.asyncio
async def test_float_blobs_kept_when_enabled(cm, monkeypatch):
    _set_flag(monkeypatch, True)
    ing = RAGIngestor(cm=cm, layer="user", binary_dim=384)
    await ing.ingest(title="t1", content="hello world " * 30, user_id="u")

    conn = await cm.get("memory.db")
    cur = await conn.execute(
        """SELECT c.bin_embedding IS NOT NULL AS has_bin, c.float_embedding IS NOT NULL AS has_float
           FROM rag_chunks c JOIN rag_pages p ON p.id = c.page_id WHERE p.user_id='u'"""
    )
    rows = await cur.fetchall()
    assert rows and all(r[0] for r in rows), "bin embeddings always stored"
    assert all(r[1] for r in rows), "float blobs present when flag on"


@pytest.mark.asyncio
async def test_float_blobs_dropped_when_disabled(cm, monkeypatch):
    _set_flag(monkeypatch, False)
    ing = RAGIngestor(cm=cm, layer="user", binary_dim=384)
    await ing.ingest(title="t2", content="another doc " * 30, user_id="v")

    conn = await cm.get("memory.db")
    cur = await conn.execute("""SELECT c.float_embedding FROM rag_chunks c JOIN rag_pages p ON p.id = c.page_id WHERE p.user_id='v'""")
    rows = await cur.fetchall()
    assert rows and all(r[0] is None for r in rows), "no float blobs when flag off"
