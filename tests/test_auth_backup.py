"""
Tests for auth — unique tests only.

Each test names its own store path. Constructed bare, `APIKeyAuth()` and
`BearerAuth()` fall back to the CWD-relative `data/auth/*.enc`, so these tests
used to append to `<repo>/data/auth/keys.enc` on every run — 704 KB of
accumulated `alice` keys by 2026-10-07, and growing by ~860 B per run. It is
gitignored (`*.enc`), so nothing was ever committed, but `test_api_key_list`
asserting `len(keys) >= 2` was satisfied by that history rather than by the
test. The suite-wide fix for the modules that resolve their directory at import
time is in `tests/conftest.py`; this file is fixed here because the fallback it
was hitting is relative, and no environment variable can redirect that.
"""

import pytest


@pytest.mark.asyncio
async def test_api_key_create(tmp_path):
    from features.auth import APIKeyAuth

    auth = APIKeyAuth(keys_file=tmp_path / "keys.enc")
    key = auth.create_key("alice", "test key")
    assert key.startswith("ak_")
    assert len(key) > 20


@pytest.mark.asyncio
async def test_api_key_verify(tmp_path):
    from features.auth import APIKeyAuth

    auth = APIKeyAuth(keys_file=tmp_path / "keys.enc")
    key = auth.create_key("alice", "test key")
    info = auth.verify(key)
    assert info is not None
    assert info["user_id"] == "alice"
    assert info["label"] == "test key"


@pytest.mark.asyncio
async def test_api_key_revoke(tmp_path):
    from features.auth import APIKeyAuth

    auth = APIKeyAuth(keys_file=tmp_path / "keys.enc")
    key = auth.create_key("alice", "test key")
    assert auth.verify(key) is not None
    revoked = auth.revoke(key)
    assert revoked is True
    assert auth.verify(key) is None


@pytest.mark.asyncio
async def test_api_key_list(tmp_path):
    from features.auth import APIKeyAuth

    auth = APIKeyAuth(keys_file=tmp_path / "keys.enc")
    auth.create_key("alice", "key1")
    auth.create_key("alice", "key2")
    keys = auth.list_keys()
    assert len(keys) == 2


@pytest.mark.asyncio
async def test_bearer_auth(tmp_path):
    from features.auth import BearerAuth

    ba = BearerAuth(token_file=tmp_path / "token.enc")
    token = ba.get_token()
    assert token.startswith("mt_")
    assert ba.verify("Bearer " + token) is True
    assert ba.verify("Bearer invalid") is False
    assert ba.verify("") is False


@pytest.mark.asyncio
async def test_bearer_rotate(tmp_path):
    from features.auth import BearerAuth

    ba = BearerAuth(token_file=tmp_path / "token.enc")
    old_token = ba.get_token()
    new_token = ba.rotate()
    assert old_token != new_token
    assert ba.verify("Bearer " + new_token) is True


@pytest.mark.asyncio
async def test_mcp_lifespan():
    from mcp_server.server import lifespan, mcp

    async with lifespan(mcp) as ctx:
        assert ctx is not None
        assert hasattr(ctx, "mm")
        assert hasattr(ctx, "user_wiki")
        assert hasattr(ctx, "agent_wiki")
