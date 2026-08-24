"""ProjectMemory store + project primitive: decisions, recall, artifacts."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.projects import ProjectMemory
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def cm(tmp_path):
    manager = AsyncConnectionManager(base_dir=tmp_path)
    store = ProjectMemory(cm=manager)
    await store._init_db()
    return manager


@pytest.mark.asyncio
async def test_project_upsert_and_get(cm):
    pm = ProjectMemory(cm=cm)
    await pm.upsert_project("alpha", summary="first", path="/tmp/alpha")
    proj = await pm.get_project("alpha")
    assert proj["summary"] == "first"
    # empty fields must NOT overwrite existing values
    await pm.upsert_project("alpha", status="done")
    proj = await pm.get_project("alpha")
    assert proj["status"] == "done"
    assert proj["summary"] == "first"
    assert proj["path"] == "/tmp/alpha"


@pytest.mark.asyncio
async def test_decisions_ordered(cm):
    pm = ProjectMemory(cm=cm)
    await pm.add_decision("p", decision="use sqlite", rationale="simple", outcome="shipped")
    await pm.add_decision("p", decision="drop cache", outcome="reverted")
    ds = await pm.list_decisions("p")
    assert [d["decision"] for d in ds] == ["drop cache", "use sqlite"]  # newest first


@pytest.mark.asyncio
async def test_artifacts_upsert_unique(cm):
    pm = ProjectMemory(cm=cm)
    await pm.upsert_artifact("p", path="src/a.py", role="core")
    await pm.upsert_artifact("p", path="src/a.py", role="updated-role", wiki_ref="w/1")
    arts = await pm.list_artifacts("p")
    assert len(arts) == 1
    assert arts[0]["role"] == "updated-role"
    assert arts[0]["wiki_ref"] == "w/1"


@pytest.mark.asyncio
async def test_symbols_replace(cm):
    pm = ProjectMemory(cm=cm)
    n = await pm.replace_symbols("p", [{"label": "foo", "file_type": "code", "source_file": "a.py", "source_location": "L12"}])
    assert n == 1
    assert await pm.count_symbols("p") == 1
    await pm.replace_symbols("p", [])
    assert await pm.count_symbols("p") == 0


# ── primitive actions ──


@pytest.mark.asyncio
async def test_primitive_init_decision_recall(tmp_path):
    from mcp_server.tools import primitives as prim
    from mcp_server.tools.primitives import project

    cm = AsyncConnectionManager(base_dir=tmp_path)
    pm = ProjectMemory(cm=cm)
    await pm._init_db()

    ctx = MagicMock()
    ctx.request_context.lifespan_context.mm._cm = cm

    wiki = MagicMock()
    wiki.add = AsyncMock(return_value="project_spec/alpha.md")

    def fake_get_wiki(_app, _layer):
        return wiki

    def fake_get_memory(_app, _layer, _uid):
        m = MagicMock()
        m.l4.search = AsyncMock(return_value=[])
        return m

    orig_wiki, orig_mem = prim._get_wiki, prim._get_memory
    prim._get_wiki = fake_get_wiki
    prim._get_memory = fake_get_memory
    try:
        res = await project(action="init", name="alpha", details="tracker for x", user_id="u", ctx=ctx)
        assert res["status"] == "ok"

        res = await project(
            action="decision",
            name="alpha",
            decision="keep it simple",
            details="YAGNI",
            outcome="accepted",
            user_id="u",
            ctx=ctx,
        )
        assert res["status"] == "decided"

        wiki.add = AsyncMock(return_value="project_spec/Map_src-core.md")
        res = await project(action="mapping", name="alpha", details="src/core.py", role="core module", status="stable", user_id="u", ctx=ctx)
        assert res["status"] == "mapped"

        # decision without text is an explicit error
        res = await project(action="decision", name="alpha", user_id="u", ctx=ctx)
        assert res["status"] == "error"

        res = await project(action="recall", name="alpha", user_id="u", ctx=ctx)
        assert res["status"] == "recalled"
        assert any(d["decision"] == "keep it simple" for d in res["decisions"])
        assert "Project: alpha" in res["audit_report"]
        assert len(res["artifacts"]) == 1

        # persisted through the same cm the primitive resolved
        assert (await pm.list_decisions("alpha"))[0]["rationale"] == "YAGNI"
    finally:
        prim._get_wiki = orig_wiki
        prim._get_memory = orig_mem


@pytest.mark.asyncio
async def test_update_without_graphify_skips_code_map(tmp_path):
    """graphify absent -> update still succeeds, code_map reports skipped."""
    from mcp_server.tools import primitives as prim
    from mcp_server.tools.primitives import project

    cm = AsyncConnectionManager(base_dir=tmp_path)

    ctx = MagicMock()
    ctx.request_context.lifespan_context.mm._cm = cm

    wiki = MagicMock()
    wiki.index.search = AsyncMock(return_value=[])
    wiki.add = AsyncMock(return_value="project_spec/beta.md")

    def fake_get_wiki(_app, _layer):
        return wiki

    def fake_get_memory(_app, _layer, _uid):
        return MagicMock()

    orig_wiki, orig_mem = prim._get_wiki, prim._get_memory
    prim._get_wiki = fake_get_wiki
    prim._get_memory = fake_get_memory
    try:
        monkey_path = tmp_path / "proj"  # does NOT exist -> skip before touching graphify
        res = await project(
            action="update",
            name="beta",
            details="refreshed summary",
            path=str(monkey_path),
            user_id="u",
            ctx=ctx,
        )
        assert res["status"] in ("ok", "updated")
        assert "skipped" in res["code_map"]
    finally:
        prim._get_wiki = orig_wiki
        prim._get_memory = orig_mem
