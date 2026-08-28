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