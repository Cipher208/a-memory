"""archive_old must archive into the base its rows were read from.

Ground: `EpisodicMemory` is built on a caller-supplied manager, but
`archive_old` constructed `ArchivedMemories()` with no `cm`, so the archive
row landed in whatever base the global `connection_manager` pointed at while
the SELECT and the DELETE ran against the caller's base. The episode moved to
a database nobody asked for. The archive and the delete were also two separate
writes, so a failure between them left the episode in the bin *and* still live.
"""

import sqlite3
import time

import pytest

from core.episodic import EpisodicMemory
from shared.archived_memories import ArchivedMemories
from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME

OLD = time.time() - 200 * 86400


def _cm(base):
    return AsyncConnectionManager(base_dir=str(base))


def _rows(base, sql, params=()):
    conn = sqlite3.connect(base / DB_NAME)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture()
def global_base(tmp_path):
    """Point the global manager at a second base, and put it back afterwards."""
    base_b = tmp_path / "b"
    base_b.mkdir()
    original = connection_manager.base_dir
    connection_manager._conns.clear()
    connection_manager.base_dir = base_b
    try:
        yield base_b
    finally:
        connection_manager.base_dir = original
        connection_manager._conns.clear()


async def test_archive_old_writes_to_the_callers_base(tmp_path, global_base):
    """Kills the mutation: `ArchivedMemories()` bound to the global manager.

    The global base is deliberately left without the table: a global-bound
    instance would create `archived_memories` there in `ensure()`, and the
    insert would then ride the caller's open transaction through the
    dispatcher's `_active` ContextVar — so a row-level assertion alone stays
    green with the fix reverted. Absence of the table is what proves the bin
    was never bound to the global base at all.
    """
    base_a = tmp_path / "a"
    base_a.mkdir()
    cm_a = _cm(base_a)
    epi = EpisodicMemory(cm=cm_a, layer="user")
    await epi._init_db()
    await ArchivedMemories(cm=cm_a).ensure()
    await epi.save("u1", "old low-weight memory", emotional_weight=0.1, created_at=OLD)

    assert await epi.archive_old("u1", days=90) == 1

    assert _rows(base_a, "SELECT episode_id FROM episodes") == []
    assert _rows(base_a, "SELECT memory_type FROM archived_memories") == [("episode",)]
    assert _rows(global_base, "SELECT name FROM sqlite_master WHERE type='table' AND name='archived_memories'") == []


async def test_archive_old_leaves_other_owners_and_layers(tmp_path):
    """Kills the mutation: a delete that is not scoped to this owner and layer."""
    base_a = tmp_path / "a"
    base_a.mkdir()
    cm_a = _cm(base_a)
    user = EpisodicMemory(cm=cm_a, layer="user")
    agent = EpisodicMemory(cm=cm_a, layer="agent")
    await user._init_db()
    await agent._init_db()
    await user.save("u1", "u1 old", emotional_weight=0.1, created_at=OLD)
    await user.save("u2", "u2 old", emotional_weight=0.1, created_at=OLD)
    await agent.save("u1", "u1 agent old", emotional_weight=0.1, created_at=OLD)

    assert await user.archive_old("u1", days=90) == 1

    remaining = _rows(base_a, "SELECT layer, user_id FROM episodes ORDER BY layer, user_id")
    assert remaining == [("agent", "u1"), ("user", "u2")]


async def test_archive_old_is_atomic(tmp_path, global_base, monkeypatch):
    """Kills the mutation: archiving and deleting as two separate writes."""
    base_a = tmp_path / "a"
    base_a.mkdir()
    cm_a = _cm(base_a)
    epi = EpisodicMemory(cm=cm_a, layer="user")
    await epi._init_db()
    await ArchivedMemories(cm=cm_a).ensure()
    await ArchivedMemories(cm=_cm(global_base)).ensure()
    await epi.save("u1", "first old", emotional_weight=0.1, created_at=OLD)
    await epi.save("u1", "second old", emotional_weight=0.1, created_at=OLD + 1)

    calls = {"n": 0}
    real_archive = ArchivedMemories.archive

    async def flaky(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("archive failed mid-way")
        return await real_archive(self, **kwargs)

    monkeypatch.setattr(ArchivedMemories, "archive", flaky)

    with pytest.raises(RuntimeError, match="archive failed mid-way"):
        await epi.archive_old("u1", days=90)

    assert len(_rows(base_a, "SELECT episode_id FROM episodes")) == 2
    assert _rows(base_a, "SELECT id FROM archived_memories") == []
    assert _rows(global_base, "SELECT id FROM archived_memories") == []
