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
