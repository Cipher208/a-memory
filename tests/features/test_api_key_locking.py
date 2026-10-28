"""F95 — a key mutation must apply to the current on-disk state.

Every test drives the public API of separate instances sharing one keys file;
none of them feeds a prepared snapshot into a helper.
"""

from __future__ import annotations

import threading

import pytest

from features.auth.api_key import APIKeyAuth
from features.auth.models import APIKeyModel
from features.auth.store import EncryptedStore


@pytest.fixture
def keys_file(tmp_path):
    return tmp_path / "keys.enc"


def test_revoke_survives_create_key_in_other_instance(keys_file):
    """A create_key holding a pre-revoke snapshot must not resurrect the key."""
    auth = APIKeyAuth(keys_file)
    key = auth.create_key(user_id="user_1", label="revoked")

    stale = APIKeyAuth(keys_file)
    assert auth.revoke(key) is True
    stale.create_key(user_id="user_2", label="other")

    fresh = APIKeyAuth(keys_file)
    assert fresh.verify(key) is None
    assert stale.verify(key) is None


def test_stale_instance_verify_does_not_resurrect_revoked_key(keys_file):
    """verify() writes last_used; that write must not restore an enabled key."""
    auth = APIKeyAuth(keys_file)
    key = auth.create_key(user_id="user_1", label="revoked")

    stale = APIKeyAuth(keys_file)
    assert auth.revoke(key) is True

    assert stale.verify(key) is None

    fresh = APIKeyAuth(keys_file)
    assert fresh.verify(key) is None


def test_key_created_by_one_instance_is_visible_to_another(keys_file):
    """An instance whose cache predates the key must still verify it."""
    creator = APIKeyAuth(keys_file)
    other = APIKeyAuth(keys_file)

    key = creator.create_key(user_id="user_1", label="visible")

    assert other.verify(key) is not None


def test_concurrent_create_key_does_not_lose_keys(keys_file):
    """Concurrent read-modify-write cycles must not drop each other's keys."""
    threads_count = 4
    keys_per_thread = 5
    instances = [APIKeyAuth(keys_file) for _ in range(threads_count)]
    barrier = threading.Barrier(threads_count)
    created: list[str] = []

    def worker(auth: APIKeyAuth) -> None:
        barrier.wait()
        for index in range(keys_per_thread):
            created.append(auth.create_key(user_id="user", label=f"k{index}"))

    threads = [
        threading.Thread(target=worker, args=(instance,))
        for instance in instances
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    fresh = APIKeyAuth(keys_file)
    assert len(created) == threads_count * keys_per_thread
    assert len(fresh.list_keys()) == threads_count * keys_per_thread
    for key in created:
        assert fresh.verify(key) is not None


def test_update_does_not_save_after_failed_load(keys_file):
    """An unreadable store must be left untouched, never overwritten with {}."""
    store = EncryptedStore(keys_file, APIKeyModel)
    keys_file.write_bytes(b"not encrypted, not json")
    before = keys_file.read_bytes()

    result = store.update(lambda data: data.__setitem__("ak_x", {}))

    assert result is None
    assert keys_file.read_bytes() == before
