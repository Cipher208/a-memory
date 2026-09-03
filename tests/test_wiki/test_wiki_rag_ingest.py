"""A2.3: wiki pages chunk into rag_chunks on write (doc → searchable sections)."""

import asyncio
import sqlite3

import pytest

from shared.connection import connection_manager


@pytest.fixture()
def hermetic_wiki(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager
    from wiki.manager import WikiManager

    asyncio.run(migration_manager.migrate())
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"))
    yield wm, tmp_path
    connection_manager._conns.clear()


def _rag_chunks(db_path):
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0]
    pages = conn.execute("SELECT COUNT(*) FROM rag_pages").fetchone()[0]
    conn.close()
    return n, pages


async def test_add_ingests_into_rag(hermetic_wiki):
    from rag.engine import RAGEngine

    wm, tmp = hermetic_wiki
    wm.rag = RAGEngine(layer="user", cm=connection_manager)
    await wm.rag.init_db()

    await wm.add(wiki_type="work_notes", title="chunky page", content="Long content about postgres tuning " * 30)

    n_chunks, n_pages = _rag_chunks(tmp / "memory.db")
    assert n_chunks > 0 and n_pages >= 1


async def test_add_without_rag_unchanged(hermetic_wiki):
    """rag=None (CLI/default) — no rag rows, no crash (back-compat)."""
    wm, tmp = hermetic_wiki
    await wm.add(wiki_type="work_notes", title="plain page", content="body")
    n_chunks, _ = _rag_chunks(tmp / "memory.db")
    assert n_chunks == 0


async def test_rag_failure_is_soft(hermetic_wiki):
    """A broken engine never breaks the wiki write."""
    wm, _tmp = hermetic_wiki

    class _Broken:
        async def ingest_text(self, *a, **k):
            raise RuntimeError("rag down")

    wm.rag = _Broken()
    path = await wm.add(wiki_type="work_notes", title="resilient page", content="body")
    assert path  # page still written
    entry = await wm.get(path)
    assert entry is not None
