"""D1.13 Memory Scopes — per-user isolation via API-key binding."""

from types import SimpleNamespace

import pytest


def _ctx_with_request(auth_header: str | None) -> object:
    """Fake mcp Context whose request carries the given Authorization header."""
    headers = {"Authorization": auth_header} if auth_header else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers)))


@pytest.mark.asyncio
async def test_resolve_no_ctx_passthrough():
    from mcp_server.tools.base import _resolve_user_id

    assert _resolve_user_id(None, "alice") == "alice"


@pytest.mark.asyncio
async def test_resolve_no_header_passthrough():
    from mcp_server.tools.base import _resolve_user_id

    ctx = _ctx_with_request(None)
    assert _resolve_user_id(ctx, "alice") == "alice"


@pytest.mark.asyncio
async def test_resolve_valid_key_binds_user(monkeypatch):
    from mcp_server.tools.base import _resolve_user_id

    class _FakeAuth:
        def verify(self, key: str) -> dict | None:
            return {"user_id": "bound-user", "label": "test"} if key == "ak_1234" else None

    monkeypatch.setattr("features.auth.api_key_auth", _FakeAuth())
    ctx = _ctx_with_request("Bearer ak_1234")
    assert _resolve_user_id(ctx, "attacker") == "bound-user"


@pytest.mark.asyncio
async def test_resolve_invalid_key_passthrough(monkeypatch):
    from mcp_server.tools.base import _resolve_user_id

    class _FakeAuth:
        def verify(self, key: str) -> dict | None:
            return None

    monkeypatch.setattr("features.auth.api_key_auth", _FakeAuth())
    ctx = _ctx_with_request("Bearer ak_bad")
    assert _resolve_user_id(ctx, "alice") == "alice"


@pytest.mark.asyncio
async def test_resolve_bearer_token_passthrough(monkeypatch):
    from mcp_server.tools.base import _resolve_user_id

    class _FakeAuth:
        def verify(self, key: str) -> dict | None:
            raise AssertionError("must not verify non-ak tokens")

    monkeypatch.setattr("features.auth.api_key_auth", _FakeAuth())
    ctx = _ctx_with_request("Bearer mt_globaltoken")
    assert _resolve_user_id(ctx, "alice") == "alice"


# ═══════════════════════════════════════════════════════════════
# AuthMiddleware — bearer OR API key
# ═══════════════════════════════════════════════════════════════

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_server.middlewares import add_middlewares


def _build_app() -> Starlette:
    async def _ping(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/ping", _ping)])
    add_middlewares(app)
    return app


def test_middleware_accepts_bearer(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setattr("mcp_server.middlewares.bearer_auth", SimpleNamespace(verify=lambda h: h == "Bearer mt_ok"))
    with TestClient(_build_app()) as client:
        resp = client.get("/api/ping", headers={"Authorization": "Bearer mt_ok"})
        assert resp.status_code == 200


def test_middleware_accepts_api_key(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setattr("mcp_server.middlewares.bearer_auth", SimpleNamespace(verify=lambda h: False))
    monkeypatch.setattr(
        "features.auth.api_key_auth",
        SimpleNamespace(verify=lambda k: {"user_id": "u1"} if k == "ak_ok" else None),
    )
    with TestClient(_build_app()) as client:
        resp = client.get("/api/ping", headers={"Authorization": "Bearer ak_ok"})
        assert resp.status_code == 200


def test_middleware_rejects_invalid(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setattr("mcp_server.middlewares.bearer_auth", SimpleNamespace(verify=lambda h: False))
    monkeypatch.setattr("features.auth.api_key_auth", SimpleNamespace(verify=lambda k: None))
    with TestClient(_build_app()) as client:
        resp = client.get("/api/ping", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401


def test_middleware_allows_no_header(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    with TestClient(_build_app()) as client:
        resp = client.get("/api/ping")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════
# Registration wrapper — binds user_id on every tool
# ═══════════════════════════════════════════════════════════════

import inspect

from mcp_server.server import _scope_tool


async def _sample_tool(user_id: str = "default", key: str = "", ctx=None):
    """Stand-in for a registered tool; records the user_id it actually sees."""
    return {"user_id": user_id, "key": key}


def test_scope_tool_no_user_id_param_unchanged():
    async def _no_uid(x: int = 1, ctx=None):
        return x

    assert _scope_tool(_no_uid) is _no_uid


@pytest.mark.asyncio
async def test_scope_tool_rewrites_user_id(monkeypatch):
    monkeypatch.setattr(
        "features.auth.api_key_auth",
        SimpleNamespace(verify=lambda k: {"user_id": "bound-user"} if k == "ak_1234" else None),
    )
    wrapped = _scope_tool(_sample_tool)
    # signature preserved for tools/list schema generation
    assert "user_id" in inspect.signature(wrapped).parameters
    ctx = _ctx_with_request("Bearer ak_1234")
    result = await wrapped(user_id="attacker", key="k", ctx=ctx)
    assert result["user_id"] == "bound-user"


@pytest.mark.asyncio
async def test_scope_tool_fallback_without_key(monkeypatch):
    wrapped = _scope_tool(_sample_tool)
    result = await wrapped(user_id="alice", key="k", ctx=None)
    assert result["user_id"] == "alice"


@pytest.mark.asyncio
async def test_scope_tool_binds_on_real_remember(monkeypatch, tmp_path):
    """Integration: a real tool call lands under the key-bound user_id."""
    from unittest.mock import MagicMock

    from mcp_server.tools.memory import memory_remember
    from shared.connection import AsyncConnectionManager
    from shared.migrations import MigrationManager

    monkeypatch.setattr(
        "features.auth.api_key_auth",
        SimpleNamespace(verify=lambda k: {"user_id": "bound-user"} if k == "ak_1234" else None),
    )
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    mm = MigrationManager(cm=cm)
    await mm.migrate()

    from core import MemoryManager as MM
    from features.rate_limiting import RateLimiter
    from graph.epistemic import EpistemicGraph
    from hooks.agent_hooks import AgentHooks
    from hooks.user_hooks import UserHooks
    from lifecycle.emotion import EmotionTrigger, EmotionEngine, load_emotion_config
    from shared.cache import MemoryCache
    from wiki import WikiManager

    class App:
        pass

    app = App()
    app.mm = MM(cm=cm)
    app.cache = MemoryCache()
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"), cm=cm)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wiki_a"), cm=cm)
    app.user_graph = EpistemicGraph(layer="user", cm=cm)
    app.agent_graph = EpistemicGraph(layer="agent", cm=cm)
    emo_cfg = load_emotion_config()
    app.emotion_engine = EmotionEngine(config=emo_cfg)
    app.emotion_trigger = EmotionTrigger(app.emotion_engine)
    app.rate_limiter = RateLimiter()
    app.user_hooks = UserHooks()
    app.agent_hooks = AgentHooks()

    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app
    ctx.request_context.request = SimpleNamespace(headers={"Authorization": "Bearer ak_1234"})

    wrapped = _scope_tool(memory_remember)
    result = await wrapped(layer="user", user_id="attacker", key="k", value="v", importance=0.5, ctx=ctx)
    assert result["status"] == "ok"
    entry = await app.mm.user_memory("bound-user").l4.get("bound-user", "k")
    assert entry is not None and entry.value == "v"
    # the attacker's namespace is untouched
    assert await app.mm.user_memory("attacker").l4.get("attacker", "k") is None
    await cm.close_all()
