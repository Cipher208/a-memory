"""The forgetting ritual must not write the base while someone else is writing it.

Why this test exists.

`forgetting_ritual` runs decay and compression. It reaches the base from the
autohooks daemon (via the `forgetting_ritual` hook, `hooks/agent_hooks.py:170` and
`hooks/user_hooks.py:139`, layer `both`) — and the same two operations also run
from the MCP server's 15-minute `_periodic_tasks`. Only the second of those takes
the per-base maintenance lock; the ritual took none at all.

Measured on the live hermes base, night of 14-15.10.2026: **106 `database is
locked` tracebacks**, one every fifteen minutes, all from
`lifecycle/forgetting.py:183` (`decay_importance` -> `executemany`). Three writers —
the server's periodic task, the daemon's ritual, the importance sweep — with the
lock held by two of them. The two misses were not an occasional race: every ritual
attempt failed, interval after interval, because another writer was always inside.

Same defect shape as the two-locks bug fixed for the nightly pass, one level
further in: not two locks, but one piece of work reachable from two processes where
only one of them locks.

`decay_importance` and `compress_duplicates` are idempotent — a skipped round is
caught by the next one — so losing the race must skip, not wait and not raise.
"""

from __future__ import annotations

import contextlib
import fcntl
from typing import TYPE_CHECKING

import pytest

from hooks.shared import forgetting_ritual
from shared.maintenance_lock import LOCK_NAME

if TYPE_CHECKING:
    import io


def _hold(base) -> io.TextIOWrapper:
    fh = open(base / LOCK_NAME, "w")  # noqa: SIM115 — closing releases the flock
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def _free(base) -> bool:
    from shared import maintenance_lock

    fd = maintenance_lock.acquire(base)
    if fd is None:
        return False
    maintenance_lock.release(fd)
    return True


class _CountingForgetting:
    """Stands in for ForgettingSystem: the test is about who may write, not what.

    `forgetting_ritual` asks it for a connection and for the archivable entries
    before it touches decay or compression, so both are stubbed: the assertion is
    about whether the two WRITES happen, and a stub that cannot answer those two
    calls would fail the test for the wrong reason.
    """

    def __init__(self) -> None:
        self.decays = 0
        self.compresses = 0

    async def _conn(self) -> object:
        return object()

    @property
    def _cm(self):
        class _CM:
            async def get(self, _name):
                return object()

        return _CM()

    async def _find_archivable_entries(self, _now, _conn=None):
        return []

    async def decay_importance(self) -> int:
        self.decays += 1
        return 1

    async def compress_duplicates(self) -> int:
        self.compresses += 1
        return 1

    async def cleanup(self) -> dict[str, int]:
        self.decays += 1
        self.compresses += 1
        return {"decayed": 1, "compressed": 1}


@pytest.fixture
def _staging_enabled(monkeypatch):
    """Force the staged path so the assertion is about decay/compress directly.

    `hooks.shared.forgetting_ritual` consults `config.get("staging", "enabled")` and
    takes a different route when it is off. Both routes run the same two operations
    on the base, so both are covered here; this fixture pins the one the live
    daemon uses, and `test_ritual_does_not_write_on_the_plain_cleanup_path` covers
    the other.
    """
    monkeypatch.setattr("config.config.get", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _lock_points_at_tmp(tmp_path, monkeypatch):
    """Point the lock at tmp_path instead of the live base directory."""
    monkeypatch.setattr("shared.maintenance_lock._lock_dir", lambda base=None: tmp_path)
    return tmp_path


async def test_ritual_does_not_write_while_another_holder_is_inside(tmp_path, monkeypatch, _staging_enabled) -> None:
    """The measured defect: the ritual ran with no lock at all, every 15 minutes."""
    fake = _CountingForgetting()
    monkeypatch.setattr("hooks.shared.ForgettingSystem", lambda: fake)

    held = _hold(tmp_path)
    try:
        result = await forgetting_ritual({})
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()

    assert fake.decays == 0, "the ritual ran decay while another writer held the base"
    assert result.get("skipped") == "maintenance_lock", f"the skip must be reported, not silent: {result!r}"
    # And the failed claim must not have left a lock behind: the skip path returns
    # BEFORE the try/finally, so nothing releases on its way out.
    assert _free(tmp_path), "the skip path leaked the maintenance lock"


async def test_ritual_skips_rather_than_waiting(tmp_path, monkeypatch, _staging_enabled) -> None:
    """Losing the race must not become a wait.

    This is what separates the shipped behaviour from `acquire(timeout=...)`: a
    holder that lets go a quarter of a second later. Without a timeout the ritual
    has already declined and moved on; with one it would have stayed and run the
    work. Both operations are idempotent, so the next round is the cheaper answer —
    and waiting would hold a connection open while another process is writing.
    """
    import threading
    import time as _time

    from hooks.shared import forgetting_ritual as ritual

    fake = _CountingForgetting()
    monkeypatch.setattr("hooks.shared.ForgettingSystem", lambda: fake)

    held = _hold(tmp_path)

    def _release_soon() -> None:
        _time.sleep(0.25)
        with contextlib.suppress(OSError):
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()

    thread = threading.Thread(target=_release_soon)
    thread.start()
    try:
        started = _time.monotonic()
        result = await ritual({})
        elapsed = _time.monotonic() - started
    finally:
        thread.join(timeout=5)

    assert fake.decays == 0, "the ritual waited for the lock instead of skipping"
    assert result.get("skipped") == "maintenance_lock"
    assert elapsed < 0.2, f"the ritual waited {elapsed:.2f}s — it is supposed to decline at once"


async def test_ritual_does_not_write_on_the_plain_cleanup_path(tmp_path, monkeypatch) -> None:
    """Staging off takes `fs.cleanup()`, which writes the same base — same guard."""
    fake = _CountingForgetting()
    monkeypatch.setattr("hooks.shared.ForgettingSystem", lambda: fake)
    monkeypatch.setattr("config.config.get", lambda *a, **k: False)

    held = _hold(tmp_path)
    try:
        result = await forgetting_ritual({})
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()

    assert fake.decays == 0, "the plain cleanup ran while another writer held the base"
    assert result.get("skipped") == "maintenance_lock"


async def test_ritual_runs_when_it_is_the_leader(tmp_path, monkeypatch, _staging_enabled) -> None:
    """The other half: the guard must not disable the ritual outright."""
    fake = _CountingForgetting()
    monkeypatch.setattr("hooks.shared.ForgettingSystem", lambda: fake)

    result = await forgetting_ritual({})

    assert fake.decays == 1, "the leader still has to do the work"
    assert fake.compresses == 1
    assert result.get("skipped") is None
    assert _free(tmp_path), "the lock must be handed back after the ritual"


async def test_ritual_releases_the_lock_when_the_work_raises(tmp_path, monkeypatch, _staging_enabled) -> None:
    """A crash inside the work must not strand the base."""

    class _Exploding(_CountingForgetting):
        async def decay_importance(self) -> int:
            raise RuntimeError("boom")

    monkeypatch.setattr("hooks.shared.ForgettingSystem", lambda: _Exploding())

    with pytest.raises(RuntimeError):
        await forgetting_ritual({})

    assert _free(tmp_path), "the lock was not released when the work raised"
