"""Revert of applied proposals (C1.13 S4)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from features import staging
from shared.connection import connection_manager

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Path:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()
    connection_manager.base_dir = original


@pytest.fixture()
def ensure_schema(fresh_dir: Path) -> Path:
    conn = sqlite3.connect(fresh_dir / "memory.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mutation_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, kind TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default', layer TEXT NOT NULL DEFAULT 'user',
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            proposed_at REAL NOT NULL, expires_at REAL NOT NULL,
            decided_at REAL, decided_by TEXT, result_ref TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            action TEXT NOT NULL, layer TEXT, target_id TEXT, details TEXT, timestamp REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS core_memory (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            importance REAL DEFAULT 0.5, is_conflict INTEGER DEFAULT 0,
            conflict_group_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            memory_kind TEXT, expires_at REAL, source TEXT DEFAULT 'manual', metadata TEXT,
            -- Added 2026-10-11, one day late. This fixture hand-writes a *partial* schema
            -- instead of running the migrations, and it did not carry `layer`. So every test
            -- here that restored an L4 row ran against a table with no such column — which is
            -- exactly the column `_restore_core` was silently leaving to its default. A restore
            -- that filed a row into the wrong layer could not fail here, because here there was
            -- no layer to file it into. Keep this block in step with the migrated schema, or
            -- the same blind spot comes back.
            layer TEXT NOT NULL DEFAULT 'user',
            visibility TEXT NOT NULL DEFAULT 'visible'
        );
        CREATE TABLE IF NOT EXISTS archived_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL DEFAULT 'default',
            original_id INTEGER, content TEXT NOT NULL, memory_type TEXT, importance REAL,
            archive_reason TEXT NOT NULL, archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memory_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, kind TEXT NOT NULL,
            from_ref TEXT NOT NULL, to_ref TEXT NOT NULL, reason TEXT, ts REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return fresh_dir


class _FakeUserMem:
    def __init__(self, store: dict) -> None:
        self._store = store

    async def remember(self, key: str, value: str, importance: float, **kw: object) -> int:
        self._store[key] = value
        return 900

    async def forget(self, key: str) -> bool:
        return self._store.pop(key, None) is not None


class _FakeMM:
    def __init__(self, store: dict) -> None:
        self._store = store

    def user_memory(self, user_id: str) -> _FakeUserMem:
        return _FakeUserMem(self._store)


class _FakeApp:
    def __init__(self) -> None:
        self.mm = _FakeMM({"dream_memory_0": "важное решение"})


async def test_revert_core_write_deletes_entry(ensure_schema: Path) -> None:
    pid = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "dream_memory_0", "value": "важное решение", "importance": 0.9})
    applied = await staging.decide(pid, True, mem=_FakeApp())
    assert applied["status"] == "applied"
    out = await staging.revert(pid, mem=_FakeApp())
    assert out["status"] == "reverted"
    conn = sqlite3.connect(ensure_schema / "memory.db")
    status = conn.execute("SELECT status FROM mutation_proposals WHERE id = ?", (pid,)).fetchone()[0]
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log").fetchall()]
    conn.close()
    assert status == "reverted"
    assert "proposal_reverted" in actions


async def test_revert_archive_restores_rows(ensure_schema: Path) -> None:
    conn = sqlite3.connect(ensure_schema / "memory.db")
    now = 1000.0
    conn.execute(
        "INSERT INTO core_memory (user_id, key, value, importance, created_at, updated_at, memory_kind)"
        " VALUES ('u1', 'old_fact', 'старый факт', 0.3, ?, ?, 'fact')",
        (now, now),
    )
    entry_id = conn.execute("SELECT entry_id FROM core_memory WHERE key='old_fact'").fetchone()[0]
    conn.execute(
        "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason)"
        " VALUES ('u1', ?, 'old_fact=старый факт', 'fact', 0.3, 'inactive_90d')",
        (entry_id,),
    )
    conn.execute("DELETE FROM core_memory WHERE entry_id = ?", (entry_id,))
    conn.commit()
    conn.close()

    pid = await staging.propose(
        "forgetting", "archive", "u1", "user", {"ids": [entry_id], "owners": {str(entry_id): "u1"}, "kinds": {str(entry_id): "fact"}}
    )
    await staging.decide(pid, True, mem=_FakeApp())
    out = await staging.revert(pid, mem=_FakeApp())
    assert out["status"] == "reverted"
    conn = sqlite3.connect(ensure_schema / "memory.db")
    restored = conn.execute("SELECT count(*) FROM core_memory WHERE key='old_fact'").fetchone()[0]
    archived_left = conn.execute("SELECT count(*) FROM archived_memories WHERE original_id=?", (entry_id,)).fetchone()[0]
    conn.close()
    assert restored == 1 and archived_left == 0
    assert out["restored"] == 1
    assert out["refused"] == []
    assert out["not_found"] == []


async def test_revert_archive_uses_the_owner_and_kind_staged_with_it(ensure_schema: Path) -> None:
    """The proposal now carries what it staged, and the revert uses it.

    Two things are proved at once, and neither is visible from the count alone:
    the recorded **owner** wins over the proposal's own `user_id` (which the
    producer writes as a constant, so it is not the row's owner), and the recorded
    **kind** resolves an id that would otherwise be ambiguous.
    """
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute(
        "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason)"
        " VALUES ('u_owner', 5, 'chosen=wrong kind', 'fact', 0.5, 'inactive_90d')"
    )
    conn.execute(
        "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason)"
        " VALUES ('u_owner', 5, 'chosen=right kind', 'question', 0.5, 'inactive_90d')"
    )
    conn.commit()
    conn.close()

    pid = await staging.propose(
        "forgetting",
        "archive",
        "default",  # deliberately not the owner of the rows
        "user",
        {"ids": [5], "owners": {"5": "u_owner"}, "kinds": {"5": "question"}},
    )
    await staging.decide(pid, True, mem=_FakeApp())
    out = await staging.revert(pid, mem=_FakeApp())

    assert out["status"] == "reverted"
    assert out["restored"] == 1
    assert out["refused"] == [], "the staged kind did not reach the restore — it was ambiguous again"
    conn = sqlite3.connect(ensure_schema / "memory.db")
    values = conn.execute("SELECT value FROM core_memory WHERE user_id='u_owner'").fetchall()
    left = conn.execute("SELECT count(*) FROM archived_memories WHERE original_id=5").fetchone()[0]
    conn.close()
    assert values == [("right kind",)]
    assert left == 1, "the other kind's row was consumed as well"


async def test_revert_archive_reports_what_it_could_not_restore(ensure_schema: Path) -> None:
    """A purged row is refused, and `reverted` says so instead of returning a bare 0
    that reads like an id which merely names nothing."""
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute(
        "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason)"
        " VALUES ('u1', 6, 'removed on purpose', 'episode', 0.5, 'foreign_agent_contamination')"
    )
    conn.commit()
    conn.close()

    pid = await staging.propose("forgetting", "archive", "u1", "user", {"ids": [6], "owners": {"6": "u1"}, "kinds": {"6": "episode"}})
    await staging.decide(pid, True, mem=_FakeApp())
    out = await staging.revert(pid, mem=_FakeApp())

    assert out["restored"] == 0
    assert [r["reason"] for r in out["refused"]] == ["purged"]
    assert out["not_found"] == []


async def test_revert_refuses_an_id_with_no_recorded_owner(ensure_schema: Path) -> None:
    """A payload staged before owners were recorded names only ids, and the
    proposal's own `user_id` is **not** the rows' owner — the producer writes a
    constant there while the ids come from a query with no owner filter. Falling
    back to it would write one owner's row into another's, which is the very
    mistake that made `restore_entries` wrong in the first place."""
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute(
        "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason)"
        " VALUES ('u_real', 5, 'k=v', 'fact', 0.5, 'inactive_90d')"
    )
    conn.commit()
    conn.close()

    pid = await staging.propose("forgetting", "archive", "default", "user", {"ids": [5]})
    await staging.decide(pid, True, mem=_FakeApp())
    out = await staging.revert(pid, mem=_FakeApp())

    assert out["restored"] == 0, "an id with no recorded owner was guessed into another owner's namespace"
    assert [r["reason"] for r in out["refused"]] == ["no_owner"]
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute("SELECT 1")
    left = conn.execute("SELECT count(*) FROM archived_memories WHERE original_id=5").fetchone()[0]
    written = conn.execute("SELECT count(*) FROM core_memory").fetchone()[0]
    conn.close()
    assert left == 1 and written == 0, "the row was moved or copied on a guess"


async def test_revert_rejects_non_applied(ensure_schema: Path) -> None:
    pid = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "k", "value": "v", "importance": 0.9})
    with pytest.raises(ValueError, match="not applied"):
        await staging.revert(pid, mem=_FakeApp())


async def test_revert_rejects_consolidate_kind(ensure_schema: Path) -> None:
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute(
        "INSERT INTO mutation_proposals (source, kind, user_id, layer, payload, status, proposed_at, expires_at, decided_at, decided_by, result_ref)"
        " VALUES ('consolidation', 'consolidate_staging', 'u1', 'user', '{}', 'applied', 1, 2, 3, 'tool', 'promoted=4')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="revert not supported"):
        await staging.revert(1, mem=_FakeApp())


async def test_revert_puts_the_row_back_in_the_layer_the_proposal_staged(ensure_schema: Path) -> None:
    """The layer of the revert is the proposal's, and the row must land in it.

    This is the case the suite could not see until 2026-10-11. `_restore_core` wrote a
    `core_memory` row without naming the `layer` column, so it took the column default
    `'user'` no matter which layer the caller worked in — and this fixture hand-wrote a
    `core_memory` with **no `layer` column at all**, so nothing here could have noticed. A
    revert staged in `agent` restored the row into `user`, and every assertion in this file
    still passed.

    Measured, not assumed: with the column named, the row lands in `agent`; without it, in
    `user`. The rest of the suite would go on passing either way, which is why the
    assertion is here rather than with the others.
    """
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute(
        "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason)"
        " VALUES ('u1', 7, 'agent_key=agent_value', 'fact', 0.5, 'inactive_90d')"
    )
    conn.commit()
    conn.close()

    pid = await staging.propose("forgetting", "archive", "u1", "agent", {"ids": [7], "owners": {"7": "u1"}, "kinds": {"7": "fact"}})
    await staging.decide(pid, True, mem=_FakeApp())
    out = await staging.revert(pid, mem=_FakeApp())

    assert out["status"] == "reverted"
    assert out["restored"] == 1, out
    conn = sqlite3.connect(ensure_schema / "memory.db")
    rows = conn.execute("SELECT layer, key, value FROM core_memory WHERE user_id='u1'").fetchall()
    conn.close()
    assert rows == [("agent", "agent_key", "agent_value")], f"the layer of the revert, not the column default: {rows}"
