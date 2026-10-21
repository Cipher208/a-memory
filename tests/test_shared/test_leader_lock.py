"""Leader lock: one member runs the background set, everyone else waits."""

from __future__ import annotations

from shared.leader_lock import LOCK_NAME, claim, release


def test_second_claim_gets_nothing(tmp_path) -> None:
    first = claim(tmp_path)
    assert first is not None
    # flock treats two fds as independent even inside one process, which is
    # exactly how a second process behaves (spec S2: no double leader).
    assert claim(tmp_path) is None, "a second claimer must not become leader"
    release(first)
    last = claim(tmp_path)
    assert last is not None, "leadership must be reclaimable after release"
    release(last)


def test_lock_file_has_the_contract_name(tmp_path) -> None:
    # The name is part of the contract: a lock at a surprise path would let two
    # processes protect different things and both believe they are the leader.
    fd = claim(tmp_path)
    try:
        assert (tmp_path / LOCK_NAME).exists()
    finally:
        release(fd)


def test_release_with_none_is_safe(tmp_path) -> None:
    release(None)
    fd = claim(tmp_path)
    assert fd is not None
    release(fd)
