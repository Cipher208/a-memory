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
            layer TEXT DEFAULT 'user', user_id TEXT, "key" TEXT, value TEXT,
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


async def _insert(cm, user_id, key, value, imp, kind, days_ago, expires_in=None, layer="user"):
    conn = await cm.get("memory.db")
    now = time.time()
    await conn.execute(
        """INSERT INTO core_memory
           (layer, user_id, "key", value, importance, memory_kind,
            expires_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (layer, user_id, key, value, imp, kind, (now + expires_in * 86400) if expires_in else None, now - days_ago * 86400, now - days_ago * 86400),
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


@pytest.mark.asyncio
async def test_compress_and_archive_survive_stale_read_snapshot_on_shared_conn(cm):
    """The other two maintenance writers must also stay off the shared connection.

    Same production shape as the decay test: a retained read cursor holds a
    snapshot on the shared handle, a foreign writer commits, and the snapshot goes
    stale. `compress_duplicates` and `archive_old_entries` both still read and wrote
    on that handle, so they failed the same instant BUSY_SNAPSHOT way.
    """
    # Duplicate key -> compress has something to remove; two rows so the read
    # snapshot spans more than one page (see the decay test for why that matters).
    await _insert(cm, "u", "dup", "первый", 0.5, "fact", days_ago=120)
    await _insert(cm, "u", "dup", "второй", 0.5, "fact", days_ago=10)
    # Old + unimportant -> archive has something to take (default 90d / 0.3).
    await _insert(cm, "u", "stale", "забытое", 0.1, "fact", days_ago=400)
    fs = ForgettingSystem(cm=cm)

    shared = await cm.get("memory.db")
    cur = await shared.execute("SELECT entry_id, importance FROM core_memory")
    await cur.fetchone()

    foreign = AsyncConnectionManager(base_dir=str(cm.base_dir))
    try:
        fconn = await foreign.get("memory.db")
        await fconn.execute("UPDATE core_memory SET importance=0.2")
        await fconn.commit()
    finally:
        await foreign.close_all()

    try:
        removed = await fs.compress_duplicates()
        assert removed >= 1, "compress_duplicates must not be wedged by a stale shared snapshot"
        archived = await fs.archive_old_entries()
        assert archived >= 1, "archive_old_entries must not be wedged by a stale shared snapshot"
    finally:
        await cur.close()


@pytest.mark.asyncio
async def test_archive_expired_commitment_is_never_archived(cm):
    """Kills: the `expired` branch selecting `commitment` despite never_archive.

    The expired branch hardcoded `memory_kind IN ('goal','todo','commitment')`
    while the policy says commitment.never_archive is True, so an expired
    commitment was archived — 13 such rows sat in the mimocode bin with reason
    `expired`. The protected list must come from the policy, not a second list.
    """
    await _insert(cm, "u", "c", "обещал", 0.5, "commitment", days_ago=1, expires_in=-1)
    await _insert(cm, "u", "g", "цель", 0.5, "goal", days_ago=1, expires_in=-1)
    await _insert(cm, "u", "t", "дело", 0.5, "todo", days_ago=1, expires_in=-1)
    fs = ForgettingSystem(cm=cm)
    n = await fs.archive_old_entries()
    assert n == 2, "goal and todo expire into the archive, commitment does not"
    conn = await cm.get("memory.db")
    left = {r["key"] for r in await (await conn.execute('SELECT "key" FROM core_memory')).fetchall()}
    assert left == {"c"}, f"commitment must survive the archive, left={left}"
    kinds = {r["memory_type"] for r in await (await conn.execute("SELECT memory_type FROM archived_memories")).fetchall()}
    assert "commitment" not in kinds


@pytest.mark.asyncio
async def test_archive_old_entries_scopes_to_owner(cm):
    """Kills: dropping the `AND user_id=?` predicate from either select."""
    await _insert(cm, "u1", "mine", "v", 0.5, "goal", days_ago=1, expires_in=-1)
    await _insert(cm, "u2", "theirs", "v", 0.5, "goal", days_ago=1, expires_in=-1)
    fs = ForgettingSystem(cm=cm)
    n = await fs.archive_old_entries(user_id="u1")
    assert n == 1, "only u1's expired row may be archived"
    conn = await cm.get("memory.db")
    left = {r["key"] for r in await (await conn.execute('SELECT "key" FROM core_memory')).fetchall()}
    assert left == {"theirs"}, f"another owner's row was taken, left={left}"


@pytest.mark.asyncio
async def test_archive_old_entries_scopes_to_layer(cm):
    """Kills: dropping the `AND layer=?` predicate from either select."""
    await _insert(cm, "u1", "user_row", "v", 0.5, "goal", days_ago=1, expires_in=-1, layer="user")
    await _insert(cm, "u1", "agent_row", "v", 0.5, "goal", days_ago=1, expires_in=-1, layer="agent")
    fs = ForgettingSystem(cm=cm)
    n = await fs.archive_old_entries(user_id="u1", layer="user")
    assert n == 1, "a layer-scoped archive must take only that layer"
    conn = await cm.get("memory.db")
    left = {r["key"] for r in await (await conn.execute('SELECT "key" FROM core_memory')).fetchall()}
    assert left == {"agent_row"}, f"the agent layer was raided by a user-layer archive, left={left}"


@pytest.mark.asyncio
async def test_archivable_entries_preview_is_read_only(cm):
    """Kills: a preview that writes, or that ignores the owner scope."""
    await _insert(cm, "u1", "mine", "v", 0.5, "goal", days_ago=1, expires_in=-1)
    await _insert(cm, "u2", "theirs1", "v", 0.5, "goal", days_ago=1, expires_in=-1)
    await _insert(cm, "u2", "theirs2", "v", 0.1, "fact", days_ago=400)
    fs = ForgettingSystem(cm=cm)
    rows = await fs.archivable_entries(user_id="u2")
    assert {r["key"] for r in rows} == {"theirs1", "theirs2"}, f"preview scoped wrong: {[r['key'] for r in rows]}"
    conn = await cm.get("memory.db")
    total = await (await conn.execute("SELECT COUNT(*) AS n FROM core_memory")).fetchone()
    assert total["n"] == 3, "a preview must not delete anything"


@pytest.mark.asyncio
async def test_archive_max_rows_takes_expired_first(cm):
    """Kills: no LIMIT (all four go), and a cap that keeps old rows over expired."""
    await _insert(cm, "u", "old1", "v", 0.05, "fact", days_ago=400)
    await _insert(cm, "u", "old2", "v", 0.05, "fact", days_ago=400)
    await _insert(cm, "u", "exp1", "v", 0.5, "goal", days_ago=1, expires_in=-1)
    await _insert(cm, "u", "exp2", "v", 0.5, "todo", days_ago=1, expires_in=-1)
    fs = ForgettingSystem(cm=cm)
    n = await fs.archive_old_entries(user_id="u", max_rows=2)
    assert n == 2, "max_rows must bound the batch"
    conn = await cm.get("memory.db")
    left = {r["key"] for r in await (await conn.execute('SELECT "key" FROM core_memory')).fetchall()}
    assert left == {"old1", "old2"}, f"expired rows must be taken first, left={left}"


@pytest.mark.asyncio
async def test_archive_excludes_every_never_archive_kind(cm):
    """Kills: a hardcoded protected list instead of the policy's.

    `procedural` is `never_archive` but was archivable before this package: the
    `old` branch hardcoded ('instruction','rule','commitment'). The list now comes
    from `get_policy`, so a kind the audit never named is protected too.
    """
    await _insert(cm, "u", "proc", "как деплоить", 0.05, "procedural", days_ago=400)
    await _insert(cm, "u", "f", "старый факт", 0.05, "fact", days_ago=400)
    fs = ForgettingSystem(cm=cm)
    n = await fs.archive_old_entries()
    assert n == 1, "only the fact is archivable"
    conn = await cm.get("memory.db")
    left = {r["key"] for r in await (await conn.execute('SELECT "key" FROM core_memory')).fetchall()}
    assert left == {"proc"}, f"a never_archive kind was archived, left={left}"


@pytest.mark.asyncio
async def test_run_cleanup_forwards_owner_and_layer(cm):
    """Kills: run_cleanup dropping user_id, or ignoring the instance's layer."""
    await _insert(cm, "u1", "mine", "v", 0.5, "goal", days_ago=1, expires_in=-1, layer="user")
    await _insert(cm, "u1", "mine_agent", "v", 0.5, "goal", days_ago=1, expires_in=-1, layer="agent")
    await _insert(cm, "u2", "theirs", "v", 0.5, "goal", days_ago=1, expires_in=-1, layer="user")
    fs = ForgettingSystem(cm=cm, layer="user")
    res = await fs.run_cleanup("u1")
    assert res == {"archived": 1}, f"run_cleanup must scope to the owner and the layer, got {res}"
    conn = await cm.get("memory.db")
    left = {r["key"] for r in await (await conn.execute('SELECT "key" FROM core_memory')).fetchall()}
    assert left == {"mine_agent", "theirs"}, f"run_cleanup raided another owner or layer, left={left}"
