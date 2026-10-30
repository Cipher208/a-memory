"""StoreUnreadable must map to a proper error body on the two reporting surfaces.

`features/auth` raises `StoreUnreadable` when the key store exists but cannot be
decoded (package #169). These tests guard the mapping in the HTTP endpoints and
the `memory_api_key` tool — and that the raw message (which carries an absolute
filesystem path) never reaches the caller.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_server.constants import ERROR_KEY_STORE_UNREADABLE
from mcp_server.endpoints.auth import AuthEndpoints
from mcp_server.tools.ops import memory_api_key


class _FakeLimiter:
    async def check(self, user_id: str) -> dict[str, Any]:
        return {"allowed": True}


class _Req:
    headers: dict[str, str] = {}
    client = None

    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self._body = body or {}

    async def json(self) -> dict[str, Any]:
        return self._body


def _unreadable_store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    """Build a valid store, then overwrite its bytes with neither Fernet nor JSON."""
    import features.auth
    from features.auth import APIKeyAuth

    path = tmp_path / "keys.enc"
    auth = APIKeyAuth(keys_file=path)
    auth.create_key("alice", "test key")
    path.write_bytes(b"not a store")
    monkeypatch.setattr(features.auth, "api_key_auth", auth)
    return auth


def _healthy_store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    import features.auth
    from features.auth import APIKeyAuth

    auth = APIKeyAuth(keys_file=tmp_path / "keys.enc")
    auth.create_key("alice", "test key")
    monkeypatch.setattr(features.auth, "api_key_auth", auth)
    return auth


@pytest.mark.asyncio
async def test_auth_keys_returns_503_when_the_store_is_unreadable(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills the mutation that lets `StoreUnreadable` escape `auth_keys` as a 500."""
    monkeypatch.setenv("MCP_AUTH_DISABLED", "1")
    _unreadable_store(tmp_path, monkeypatch)
    ep = AuthEndpoints(_FakeLimiter())  # type: ignore[arg-type]

    resp = await ep.auth_keys(_Req())  # type: ignore[arg-type]

    assert resp.status_code == 503
    assert json.loads(resp.body) == {"error": ERROR_KEY_STORE_UNREADABLE}


@pytest.mark.asyncio
async def test_auth_keys_does_not_leak_the_store_path(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the new mapping: the raw message carries an absolute path, never echo it."""
    monkeypatch.setenv("MCP_AUTH_DISABLED", "1")
    _unreadable_store(tmp_path, monkeypatch)
    ep = AuthEndpoints(_FakeLimiter())  # type: ignore[arg-type]

    resp = await ep.auth_keys(_Req())  # type: ignore[arg-type]
    body = resp.body.decode()

    assert "/" not in body
    assert str(tmp_path) not in body


@pytest.mark.asyncio
async def test_auth_create_returns_503_when_the_store_is_unreadable(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills the mutation that lets `StoreUnreadable` escape `auth_create` as a 500."""
    monkeypatch.setenv("MCP_AUTH_DISABLED", "1")
    _unreadable_store(tmp_path, monkeypatch)
    ep = AuthEndpoints(_FakeLimiter())  # type: ignore[arg-type]

    resp = await ep.auth_create(_Req({"user_id": "bob"}))  # type: ignore[arg-type]

    assert resp.status_code == 503
    assert json.loads(resp.body) == {"error": ERROR_KEY_STORE_UNREADABLE}


@pytest.mark.asyncio
async def test_memory_api_key_returns_an_error_result_when_the_store_is_unreadable(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills the mutation that lets `StoreUnreadable` escape the tool as a transport error.

    All three branches are exercised: an unmapped `create` would leave a tool
    error on the one action that writes, and a loop over `list`/`revoke` alone
    would not notice.
    """
    _unreadable_store(tmp_path, monkeypatch)

    for action in ("create", "list", "revoke"):
        result = await memory_api_key(action=action, api_key="ak_deadbeef")
        assert result["error"] == ERROR_KEY_STORE_UNREADABLE
        assert result["keys"] is None
        assert result["revoked"] is None
        assert result["api_key"] is None
        assert str(tmp_path) not in json.dumps(result), "the tool result leaked the store path"


@pytest.mark.asyncio
async def test_memory_api_key_still_lists_on_a_healthy_store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard-does-not-over-fire: a healthy store still lists and reports no error."""
    _healthy_store(tmp_path, monkeypatch)

    result = await memory_api_key(action="list")

    assert result["error"] is None
    assert len(result["keys"]) == 1
