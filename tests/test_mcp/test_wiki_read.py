"""D2.1: wiki_read tool + skill convention (progressive disclosure read leg)."""

import time

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager
from wiki.manager import WikiManager


def _ctx_for(wiki: WikiManager):
    from types import SimpleNamespace

    app = SimpleNamespace(user_wiki=wiki, agent_wiki=wiki)
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


@pytest.fixture
async def wiki(tmp_path):
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki"))
    await wm.init_db()
    return wm


@pytest.fixture
async def migrated_wiki(tmp_path, monkeypatch):
    """Герметичная глобальная БД + WikiManager поверх неё (graph-минер + core_memory)."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki"))
    await wm.init_db()
    yield wm
    connection_manager._conns.clear()


@pytest.mark.asyncio
async def test_wiki_read_round_trip(wiki):
    from mcp_server.tools.wiki import wiki_add, wiki_read

    ctx = _ctx_for(wiki)
    await wiki_add(layer="user", title="Deploy Flow", content="ssh then uv sync", wiki_type="skill", ctx=ctx)
    listed = await wiki.list_by_type("skill", 10)
    assert listed, "skill page should be listed under type=skill"
    path = listed[0].file_path
    out = await wiki_read(layer="user", path=path, ctx=ctx)
    assert out["status"] == "ok"
    assert out["content"] == "ssh then uv sync"
    assert out["wiki_type"] == "skill"


@pytest.mark.asyncio
async def test_wiki_read_not_found(wiki):
    from mcp_server.tools.wiki import wiki_read

    out = await wiki_read(layer="user", path="skill/missing.md", ctx=_ctx_for(wiki))
    assert out == {"status": "not_found", "path": "skill/missing.md"}


@pytest.mark.asyncio
async def test_wiki_list_returns_path(wiki):
    from mcp_server.tools.wiki import wiki_add, wiki_list

    ctx = _ctx_for(wiki)
    await wiki_add(layer="user", title="Deploy Flow", content="x", wiki_type="skill", ctx=ctx)
    out = await wiki_list(layer="user", wiki_type="skill", ctx=ctx)
    assert out["count"] == 1
    assert out["pages"][0]["path"], "list must expose file_path for the read leg"


@pytest.mark.asyncio
async def test_lint_flags_oversized_skill(tmp_path):
    from wiki.lint import lint_entry
    from wiki.models import WikiEntry
    import time

    entry = WikiEntry(
        wiki_type="skill",
        title="Big",
        content="x" * 5000,
        file_path="skill/Big.md",
        created_at=time.time(),
        updated_at=time.time(),
    )
    codes = [f.code for f in lint_entry(entry)]
    assert "skill_too_large" in codes
    small = entry.model_copy(update={"content": "x" * 100})
    assert "skill_too_large" not in [f.code for f in lint_entry(small)]


@pytest.mark.asyncio
async def test_wiki_read_returns_related_facts(migrated_wiki) -> None:
    """S19: страница с [[fact:key]]-линком → wiki_read содержит related_facts."""
    from mcp_server.tools.wiki import wiki_add, wiki_read

    wiki = migrated_wiki
    ctx = _ctx_for(wiki)
    await wiki_add(layer="user", title="Ops Notes", content="см. [[fact:backup_enc]]", wiki_type="diary", ctx=ctx)
    listed = await wiki.list_by_type("diary", 10)
    page_path = listed[0].file_path

    from core.memory import CoreMemory

    conn = await connection_manager.get("memory.db")
    cmem = CoreMemory(cm=connection_manager, layer="user")
    await cmem.save("ru1", "fact:backup_enc", "бэкапы шифруются ключом x", importance=0.8, memory_kind="fact")
    await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','ru1','бэкапы шифруются ключом x','fact','[]',0.5,?)",
        (time.time(),),
    )
    await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','ru1',?,'wiki_page','[]',0.5,?)",
        (page_path, time.time()),
    )
    await conn.commit()

    from lifecycle.graph_miners import miner_wiki_fact_links

    res = await miner_wiki_fact_links(connection_manager, "user")
    assert res["edges"] >= 1, "setup: ребро построено"

    out = await wiki_read(layer="user", path=page_path, ctx=ctx)
    assert out["status"] == "ok"
    assert out["related_count"] >= 1, f"related_facts в ответе: {out}"
    assert any(f["value"] == "бэкапы шифруются ключом x" for f in out["related_facts"]), f"значение факта: {out['related_facts']}"
    assert any(f["key"] == "fact:backup_enc" for f in out["related_facts"])


@pytest.mark.asyncio
async def test_wiki_read_related_facts_empty_default(wiki) -> None:
    """Без graph-связок — related_facts пуст (常态, не ошибка)."""
    from mcp_server.tools.wiki import wiki_add, wiki_read

    ctx = _ctx_for(wiki)
    await wiki_add(layer="user", title="Lonely", content="no links here", wiki_type="diary", ctx=ctx)
    listed = await wiki.list_by_type("diary", 10)
    out = await wiki_read(layer="user", path=listed[0].file_path, ctx=ctx)
    assert out["status"] == "ok"
    assert out["related_facts"] == [] and out["related_count"] == 0
