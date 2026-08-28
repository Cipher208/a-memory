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