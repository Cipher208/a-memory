"""project(audit) — dream-based gap analysis over real stores."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.connection import AsyncConnectionManager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    manager = AsyncConnectionManager(base_dir=tmp_path)
    await MigrationManager(cm=manager).migrate()
    return manager


def _make_app(cm, tmp_path):
    from core import MemoryManager as MM
    from graph.epistemic import EpistemicGraph
    from wiki import WikiManager

    class App:
        pass

    app = App()
    app.mm = MM(cm=cm)
    app.cache = None
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wa"), cm=cm)
    app.user_graph = EpistemicGraph(layer="user", cm=cm)
    app.user_multi = MagicMock()

    async def _search(query, user_id="default", limit=5, **kw):
        # "architecture"-flavored query hits, others don't — deterministic
        if "architecture" in query:
            return [{"title": "arch doc", "content": "system design", "source": "wiki"}]
        return []

    app.user_multi.search = AsyncMock(side_effect=_search)
    app.importance = MagicMock()
    return app


@pytest.mark.asyncio
async def test_audit_reports_dimensions_and_store(tmp_path):
    cm = AsyncConnectionManager(base_dir=tmp_path / "db")
    (tmp_path / "db").mkdir(exist_ok=True)
    await MigrationManager(cm=cm).migrate()

    from core import MemoryManager as MM
    from features.rate_limiting import RateLimiter
    from graph.epistemic import EpistemicGraph
    from wiki import WikiManager

    class App:
        pass

    app = App()
    app.mm = MM(cm=cm)
    app.cache = None
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wa"), cm=cm)
    app.user_graph = EpistemicGraph(layer="user", cm=cm)
    app.agent_graph = EpistemicGraph(layer="agent", cm=cm)
    app.user_multi = MagicMock()

    async def _search(query, user_id="default", limit=5, **kw):
        if "architecture" in query:
            return [{"title": "arch doc", "content": "x", "source": "wiki"}]
        return []

    app.user_multi.search = AsyncMock(side_effect=_search)
    app.rate_limiter = RateLimiter()

    from config import config as _cfg

    data = {**getattr(_cfg, "_data", {}), "wiki": {"user": {"project_spec": True}}}
    from unittest.mock import patch as _mockpatch

    _p = _mockpatch.dict(_cfg._data, data)
    _p.start()

    import wiki.shared as _wshared

    def _fake_load_config():
        return {"wiki": {"user": {"project_spec": True}}}

    _wp = _mockpatch.object(_wshared, "load_config", _fake_load_config)
    _wp.start()
    app.user_hooks = MagicMock()

    ctx = MagicMock()
    ctx.request_context.lifespan_context = app

    from mcp_server.tools.primitives import project

    res = await project(action="init", name="apollo", details="lunar tracker", user_id="ua", ctx=ctx)
    assert res["status"] == "ok"

    res = await project(
        action="decision",
        name="apollo",
        decision="sqlite over postgres",
        details="single-node deployment",
        outcome="accepted",
        user_id="ua",
        ctx=ctx,
    )
    assert res["status"] == "decided"

    res = await project(action="audit", name="apollo", user_id="ua", ctx=ctx)
    assert res["status"] == "audit"
    report = res["audit_report"]
    # architecture dimension found its hit; security/testing missing
    assert "Architecture/Design: 1 related entries" in report
    assert "Security/Hardening documentation is missing" in report
    # store completeness: identity + summary present, decision counted
    assert "Project summary is present." in report
    assert "Decisions recorded: 1" in report
    assert res["decisions"][0]["decision"] == "sqlite over postgres"
