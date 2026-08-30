"""D2.1: wiki_read tool + skill convention (progressive disclosure read leg)."""

import pytest

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
