"""Tests for typed forgetting — type-aware decay and archive."""

import time

import pytest

from lifecycle.forgetting import ForgettingSystem
from shared.archived_memories import ArchivedMemories
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def cm(tmp_path):
    m = AsyncConnectionManager(base_dir=str(tmp_path))
    await m.execute_script(
        "memory.db",
        """
        CREATE TABLE core_memory (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, "key" TEXT, value TEXT,
            importance REAL, memory_kind TEXT,
            expires_at REAL, source TEXT DEFAULT 'manual', metadata TEXT,
            created_at REAL, updated_at REAL
        );
    """,
    )
    # Initialize archived_memories table via ArchivedMemories
    am = ArchivedMemories(cm=m)
    await am._init_db()
    return m


async def _insert(cm, user_id, key, value, imp, kind, days_ago, expires_in=None):
    conn = await cm.get("memory.db")
    now = time.time()
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, memory_kind,
            expires_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, key, value, imp, kind, (now + expires_in * 86400) if expires_in else None, now - days_ago * 86400, now - days_ago * 86400),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_decay_never_touches_instruction(cm):
    await _insert(cm, "u", "rule1", "никогда не удаляй бэкапы", 0.95, "instruction", days_ago=1000)
    fs = ForgettingSystem(cm=cm)
    n = await fs.decay_importance()
    assert n == 0


@pytest.mark.asyncio
async def test_decay_touches_fact(cm):
    await _insert(cm, "u", "f1", "любит синий", 0.5, "fact", days_ago=120)
    fs = ForgettingSystem(cm=cm)
    n = await fs.decay_importance()
    assert n >= 1


@pytest.mark.asyncio
async def test_archive_never_touches_rule_or_instruction(cm):
    await _insert(cm, "u", "rule", "запрещено", 0.05, "rule", days_ago=10000)
    await _insert(cm, "u", "instr", "запомни", 0.05, "instruction", days_ago=10000)
    fs = ForgettingSystem(cm=cm)
    n = await fs.archive_old_entries()
    assert n == 0


@pytest.mark.asyncio
async def test_archive_handles_expired_goal(cm):
    await _insert(cm, "u", "g", "выучить Rust", 0.8, "goal", days_ago=10, expires_in=-1)
    fs = ForgettingSystem(cm=cm)
    n = await fs.archive_old_entries()
    assert n == 1
    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT 1 FROM archived_memories WHERE memory_type='goal'")).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_archive_old_low_importance_fact(cm):
    await _insert(cm, "u", "f", "любит xyz", 0.1, "fact", days_ago=200)
    fs = ForgettingSystem(cm=cm)
    n = await fs.archive_old_entries()
    assert n == 1


@pytest.mark.asyncio
async def test_decay_commits_in_chunks(cm):
    """decay must release the write lock between batches, not hold it for all.

    It used to `executemany` every row in one transaction and commit once, so the
    SQLite write lock was held for the whole batch. A tool write arriving
    mid-batch waited out `busy_timeout` (250 ms) and failed with
    `database is locked` — measured on the hermes base, 106 tracebacks in one
    night, one every fifteen minutes. Chunked commits release the write lock
    between batches, so a tool write can interleave instead of failing.
    """
    N = 2500
    now = time.time()
    conn = await cm.get("memory.db")
    await conn.executemany(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, memory_kind,
            expires_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, NULL, ?, ?)""",
        [("u", f"k{i}", f"v{i}", 0.5, "fact", now - 120 * 86400, now - 120 * 86400) for i in range(N)],
    )
    await conn.commit()

    fs = ForgettingSystem(cm=cm)

    # Decay writes through the dispatch's private connection, so count the
    # dispatch's write_many calls: it commits once per call, and more than one
    # call means the write lock is released between batches.
    batches: list[int] = []
    real_write_many = fs._dispatch.write_many

    async def counting_write_many(db: str, ops: object) -> None:
        batches.append(1)  # type: ignore[arg-type]
        await real_write_many(db, ops)  # type: ignore[arg-type]

    fs._dispatch.write_many = counting_write_many  # type: ignore[method-assign]

    n = await fs.decay_importance()
    assert n == N, f"every row should decay, got {n}"
    assert len(batches) > 1, (
        "decay must commit in chunks so the SQLite write lock is released "
        "between batches — one long transaction is what made tool writes fail "
        "with 'database is locked'"
    )


@pytest.mark.asyncio
async def test_decay_survives_stale_read_snapshot_on_shared_conn(cm):
    """decay must not fail `database is locked` when the shared conn holds a stale read txn.

    A read cursor retained on the shared connection keeps a read transaction (and
    with it a snapshot) open on the handle every component borrows. Once a foreign
    writer — here another manager on the same file — commits, that snapshot is
    stale, and the next write on the *same* connection fails instantly with
    `SQLITE_BUSY_SNAPSHOT` (517); SQLite does not consult the busy handler for it.
    Measured on the hermes base 17.10: decay/compress/prune all failed for hours
    with the shm read-lock byte held by the MCP process while the autohooks daemon
    kept committing. Maintenance writes must therefore not ride the shared
    connection: they get their own, fresh one, whose snapshot cannot be stale.
    """
    # Two rows, not one. With a single row the table fits in one b-tree page, and
    # SQLite can keep the write off the page the retained read snapshot touched, so
    # the BUSY_SNAPSHOT never fires and the test would pass even on the buggy code.
    # Two rows spread across pages, and the foreign write then touches a page the
    # stale snapshot already read — the exact production shape. Verified: one row
    # returns n>=1 (green, useless); two rows return 0 with `database is locked`.
    await _insert(cm, "u", "f1", "любит синий", 0.5, "fact", days_ago=120)
    await _insert(cm, "u", "f2", "любит зелёный", 0.4, "fact", days_ago=120)
    fs = ForgettingSystem(cm=cm)

    # Retain a read cursor on the SHARED conn: fetch one row, exhaust neither the
    # cursor nor the statement. While it lives, the read txn and its snapshot are open.
    shared = await cm.get("memory.db")
    cur = await shared.execute("SELECT entry_id, importance FROM core_memory")
    await cur.fetchone()

    # Foreign writer commits on a different connection -> the shared snapshot is stale.
    # It must UPDATE the same column decay writes (`importance`) of a row the retained
    # cursor already read: SQLITE_BUSY_SNAPSHOT (517) fires when the stale connection
    # writes a page whose content it read under the stale snapshot — an INSERT
    # appending to a fresh page does not touch what was read.
    foreign = AsyncConnectionManager(base_dir=str(cm.base_dir))
    try:
        fconn = await foreign.get("memory.db")
        await fconn.execute("UPDATE core_memory SET importance=0.1 WHERE \"key\"='f1'")
        await fconn.commit()
    finally:
        await foreign.close_all()

    try:
        n = await fs.decay_importance()
        assert n >= 1, (
            "decay must not be wedged by a stale read snapshot on the shared connection — BUSY_SNAPSHOT is instant and unhandled by busy_timeout"
        )
    finally:
        await cur.close()
