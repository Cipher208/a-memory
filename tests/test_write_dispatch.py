import pytest

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch

CREATE = (
    "CREATE TABLE IF NOT EXISTS core_memory ("
    "entry_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    'user_id TEXT, "key" TEXT, value TEXT, importance REAL,'
    "memory_kind TEXT, expires_at REAL, created_at REAL, updated_at REAL)"
)


@pytest.fixture
async def cm(tmp_path):
    m = AsyncConnectionManager(base_dir=str(tmp_path))
    conn = await m.get(DB_NAME)
    await conn.execute(CREATE)
    await conn.commit()
    yield m
    await m.close_all()


@pytest.mark.asyncio
async def test_write_returns_rowcount(cm):
    d = WriteDispatch(cm)
    n = await d.write(
        DB_NAME,
        'INSERT INTO core_memory (user_id, "key", value, importance, memory_kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        ("u", "k1", "v1", 0.5, "fact", 1000.0, 1000.0),
    )
    assert n == 1
    rows = await d.query(DB_NAME, 'SELECT value FROM core_memory WHERE "key" = ?', ("k1",))
    assert len(rows) == 1
    assert rows[0]["value"] == "v1"


@pytest.mark.asyncio
async def test_write_many_is_atomic(cm):
    d = WriteDispatch(cm)
    with pytest.raises(Exception):
        await d.write_many(
            DB_NAME,
            [
                ('INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("u", "a", "x")),
                ("INSERT INTO core_memory (entry_id, user_id) VALUES (?, ?)", (1, "bad")),  # duplicate PK -> fail
            ],
        )
    rows = await d.query(DB_NAME, 'SELECT "key" FROM core_memory WHERE "key" = ?', ("a",))
    assert rows == [], "write_many must roll back the whole batch on failure"


@pytest.mark.asyncio
async def test_write_after_foreign_commit_succeeds(cm):
    # Regression: a retained read cursor on the shared conn + foreign commit must NOT
    # make a subsequent write on the SAME dispatch fail with BUSY_SNAPSHOT (517).
    d = WriteDispatch(cm)
    for key, imp in (("p1", 0.5), ("p2", 0.4)):
        await d.write(
            DB_NAME,
            'INSERT INTO core_memory (user_id, "key", value, importance, memory_kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            ("u", key, key, imp, "fact", 1000.0, 1000.0),
        )
    # Retain a raw read cursor on the shared conn to simulate the leak.
    shared = await cm.get(DB_NAME)
    cur = await shared.execute("SELECT entry_id, importance FROM core_memory")
    await cur.fetchone()
    # Foreign commit on a separate manager.
    foreign = AsyncConnectionManager(base_dir=str(cm.base_dir))
    try:
        fconn = await foreign.get(DB_NAME)
        await fconn.execute('UPDATE core_memory SET importance = 0.1 WHERE "key" = ?', ("p1",))
        await fconn.commit()
    finally:
        await foreign.close_all()
    # Dispatch write must still succeed (its own conn's snapshot is fresh).
    n = await d.write(DB_NAME, 'UPDATE core_memory SET importance = 0.9 WHERE "key" = ?', ("p2",))
    assert n == 1


@pytest.mark.asyncio
async def test_insert_returns_lastrowid(cm):
    d = WriteDispatch(cm)
    first = await d.insert(
        DB_NAME,
        'INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)',
        ("u", "a", "1"),
    )
    second = await d.insert(
        DB_NAME,
        'INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)',
        ("u", "b", "2"),
    )
    assert first == 1
    assert second == 2, "insert must report the id the database assigned"


@pytest.mark.asyncio
async def test_atomic_read_then_write_commits(cm):
    d = WriteDispatch(cm)
    await d.write(DB_NAME, 'INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("u", "head", "v"))

    async def chain(conn):
        row = await (await conn.execute("SELECT MAX(entry_id) FROM core_memory")).fetchone()
        parent = int(row[0])
        cur = await conn.execute('INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("u", "child", str(parent)))
        return (parent, cur.lastrowid)

    parent, child = await d.atomic(DB_NAME, chain)
    assert parent == 1
    assert child == 2
    rows = await d.query(DB_NAME, 'SELECT value FROM core_memory WHERE "key" = ?', ("child",))
    assert rows[0]["value"] == "1"


@pytest.mark.asyncio
async def test_atomic_read_then_write_rolls_back(cm):
    d = WriteDispatch(cm)

    async def fail_after_write(conn):
        await conn.execute('INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("u", "ghost", "x"))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await d.atomic(DB_NAME, fail_after_write)
    rows = await d.query(DB_NAME, 'SELECT "key" FROM core_memory WHERE "key" = ?', ("ghost",))
    assert rows == [], "atomic must roll back everything written before the failure"


@pytest.mark.asyncio
async def test_get_write_dispatch_is_shared_per_manager(cm):
    from shared.write_dispatch import get_write_dispatch

    assert get_write_dispatch(cm) is get_write_dispatch(cm)

    other = AsyncConnectionManager(base_dir=str(cm.base_dir))
    try:
        assert get_write_dispatch(cm) is not get_write_dispatch(other)
    finally:
        await other.close_all()


@pytest.mark.asyncio
async def test_atomic_is_reentrant_for_the_same_task(cm):
    """A domain writer called from inside an atomic body must join that transaction.

    Without re-entrancy the inner call waits on the lock the outer frame holds,
    which is a permanent hang rather than an error.
    """
    d = WriteDispatch(cm)

    async def _outer(conn):
        await conn.execute('INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("u", "outer", "1"))
        # Nested write through the public API: same connection, no extra commit.
        await d.write(DB_NAME, 'INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("u", "inner", "2"))

        async def _inner(conn2):
            assert conn2 is conn, "nested atomic must reuse the outer connection"
            await conn2.execute('INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("u", "deep", "3"))
            return 3

        return await d.atomic(DB_NAME, _inner)

    assert await d.atomic(DB_NAME, _outer) == 3
    rows = await d.query(DB_NAME, 'SELECT "key" FROM core_memory ORDER BY entry_id')
    assert [r["key"] for r in rows] == ["outer", "inner", "deep"]


@pytest.mark.asyncio
async def test_atomic_rollback_covers_nested_writes(cm):
    """A failure outside a nested call still discards everything it wrote."""
    d = WriteDispatch(cm)

    async def _outer(conn):
        await d.write(DB_NAME, 'INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("u", "kept", "1"))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await d.atomic(DB_NAME, _outer)
    rows = await d.query(DB_NAME, 'SELECT "key" FROM core_memory')
    assert rows == [], "the whole outer transaction must roll back"
