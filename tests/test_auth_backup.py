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


# --- unreadable store: an unreadable file must not look like an absent key ---
#
# `path.write_bytes(b"not a store")` makes `EncryptedStore._load()` return None:
# the bytes are not a valid Fernet blob (decrypt_json raises), and they are not
# UTF-8 JSON either (json.loads raises), so both branches fall through to None.
# Before the fix `load()` collapsed that None into `{}` and every caller read the
# lie as truth.


def _break_store(path):
    path.write_bytes(b"not a store")
    return path.read_bytes()


@pytest.mark.asyncio
async def test_revoke_raises_when_the_store_is_unreadable(tmp_path):
    """Kills the mutation that turns an unreadable store into `return False`.

    A False here tells the operator "not revoked" while the key may still be
    live on disk. The bytes must also be untouched: revoke must not fall back
    to overwriting the store with `{}`.
    """
    from features.auth import APIKeyAuth
    from features.auth.store import StoreUnreadable

    path = tmp_path / "keys.enc"
    auth = APIKeyAuth(keys_file=path)
    key = auth.create_key("alice", "test key")
    broken = _break_store(path)

    with pytest.raises(StoreUnreadable):
        auth.revoke(key)

    assert path.read_bytes() == broken


@pytest.mark.asyncio
async def test_delete_key_raises_when_the_store_is_unreadable(tmp_path):
    """Kills the mutation that turns an unreadable store into `return False`."""
    from features.auth import APIKeyAuth
    from features.auth.store import StoreUnreadable

    path = tmp_path / "keys.enc"
    auth = APIKeyAuth(keys_file=path)
    key = auth.create_key("alice", "test key")
    broken = _break_store(path)

    with pytest.raises(StoreUnreadable):
        auth.delete_key(key)

    assert path.read_bytes() == broken


@pytest.mark.asyncio
async def test_verify_raises_when_the_store_is_unreadable(tmp_path):
    """Kills the mutation that turns an unreadable store into `return None`.

    `None` means "invalid key"; an unreadable store means "cannot check". The
    caller must be able to tell those apart.
    """
    from features.auth import APIKeyAuth
    from features.auth.store import StoreUnreadable

    path = tmp_path / "keys.enc"
    auth = APIKeyAuth(keys_file=path)
    key = auth.create_key("alice", "test key")
    _break_store(path)

    with pytest.raises(StoreUnreadable):
        auth.verify(key)


@pytest.mark.asyncio
async def test_list_keys_reads_the_current_state_not_the_cache(tmp_path):
    """Kills the mutation that serves `self._keys` (the __init__ snapshot).

    Instance B is constructed before A creates the key; B must still see it.
    """
    from features.auth import APIKeyAuth

    path = tmp_path / "keys.enc"
    a = APIKeyAuth(keys_file=path)
    b = APIKeyAuth(keys_file=path)
    assert b.list_keys() == []

    a.create_key("alice", "created after B was built")

    keys = b.list_keys()
    assert len(keys) == 1
    assert keys[0]["user_id"] == "alice"


@pytest.mark.asyncio
async def test_list_keys_raises_when_the_store_is_unreadable(tmp_path):
    """Kills the mutation that reports an empty key list for an unreadable store."""
    from features.auth import APIKeyAuth
    from features.auth.store import StoreUnreadable

    path = tmp_path / "keys.enc"
    auth = APIKeyAuth(keys_file=path)
    auth.create_key("alice", "test key")
    _break_store(path)

    with pytest.raises(StoreUnreadable):
        auth.list_keys()


@pytest.mark.asyncio
async def test_verify_still_denies_an_unknown_key_on_a_readable_store(tmp_path):
    """Guard against over-firing: an unknown key is still a plain None."""
    from features.auth import APIKeyAuth

    auth = APIKeyAuth(keys_file=tmp_path / "keys.enc")
    auth.create_key("alice", "test key")

    assert auth.verify("ak_" + "0" * 48) is None


@pytest.mark.asyncio
async def test_auth_middleware_denies_instead_of_crashing_when_the_store_is_unreadable(tmp_path, monkeypatch):
    """The middleware must fail closed: deny, never let the raise escape.

    Before the fix `verify()` returned None, so this was green for the wrong
    reason. After `verify()` raises, the middleware's catch is what keeps the
    raise from becoming a 500 — this test guards that catch.
    """
    import features.auth
    from features.auth import APIKeyAuth
    from mcp_server.middlewares import _verify_api_key

    path = tmp_path / "keys.enc"
    auth = APIKeyAuth(keys_file=path)
    key = auth.create_key("alice", "test key")
    _break_store(path)
    monkeypatch.setattr(features.auth, "api_key_auth", auth)

    assert _verify_api_key("Bearer " + key) is False
