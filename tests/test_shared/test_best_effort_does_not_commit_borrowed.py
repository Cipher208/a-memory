"""A best-effort writer must not commit a transaction it did not open.

The shared handle (`AsyncConnectionManager.get()`) is handed to every component in the process, so
`await conn.commit()` from any of them ends whatever transaction another component has open. This
file pins the *best-effort* half of that contract: a writer whose row is allowed to be lost — a log,
a telemetry record, a miner's edge, a pruner's delete — must leave a foreign transaction alone. If
the transaction belongs to someone else, riding along with it is correct and flushing it is not.

The durable half is the opposite problem and is not asserted here: a writer that must survive a
stranger's `rollback()` cannot be fixed by skipping its own commit, because the row then dies with
the stranger's transaction. See the module docstring of `tests/test_shared/test_connection.py` for
the ownership contract itself.

Each test drives one real production function against a foreign transaction that is open on the
same handle, then asks a *second* connection what became durable. The second connection is the
point: reading through the shared handle would show the uncommitted row either way and the test
would pass whatever the code did.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from core.projects import PROJECTS_DB, ProjectMemory
from lifecycle.transitions import record_transition
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


async def _leave_foreign_work_in_flight(cm: AsyncConnectionManager, db_name: str = DB_NAME) -> None:
    """Open a transaction on the shared handle and write one row into it, uncommitted.

    The scratch table is created and committed first — a script commits, so that step must not be
    inside the window being measured.
    """
    conn = await cm.get(db_name)
    await conn.execute("CREATE TABLE IF NOT EXISTS foreign_probe (v TEXT)")
    await conn.commit()
    await conn.execute("INSERT INTO foreign_probe VALUES ('somebody else, still in flight')")


def _durable_rows(base_dir: str, db_name: str = DB_NAME) -> int:
    """How many `foreign_probe` rows a *separate* connection can see. Committed, or nothing."""
    path = Path(base_dir) / db_name
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as other:
        try:
            return int(other.execute("SELECT COUNT(*) FROM foreign_probe").fetchone()[0])
        except sqlite3.OperationalError:  # the table was itself rolled away
            return 0


@pytest.mark.asyncio
async def test_execute_script_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """The schema chokepoint: `execute_script` is called by every `ensure()`/`_init_db()`.

    `sqlite3`'s `executescript()` commits implicitly, so this method flushes a stranger's open
    transaction even without its own explicit `commit()`. Its scripts are all
    `CREATE TABLE IF NOT EXISTS`/guarded `ALTER`, i.e. idempotent — a schema lost with the stranger's
    rollback is recreated by the next call, which is exactly why riding along is acceptable here.
    """
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await _leave_foreign_work_in_flight(cm)

    await cm.execute_script(DB_NAME, "CREATE TABLE IF NOT EXISTS schema_probe (x)")

    assert _durable_rows(str(tmp_path)) == 0, "execute_script committed a transaction it does not own"
    await (await cm.get(DB_NAME)).rollback()


@pytest.mark.asyncio
async def test_record_transition_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """`record_transition` is a best-effort audit line; it may not flush a stranger's work."""
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await _leave_foreign_work_in_flight(cm)

    await record_transition(cm, "u1", "episode", "episode:7", "l4", "core:42", "consolidation")

    assert _durable_rows(str(tmp_path)) == 0, "record_transition committed a transaction it does not own"
    await (await cm.get(DB_NAME)).rollback()


@pytest.mark.asyncio
async def test_project_touch_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """`touch` only moves an `updated_at` timestamp; a stranger's transaction is not its to end."""
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    store = ProjectMemory(cm)
    await store._init_db()
    # `touch` writes `projects.db`, so the borrowed transaction must be opened there too.
    await _leave_foreign_work_in_flight(cm, PROJECTS_DB)

    await store.touch("proj")

    assert _durable_rows(str(tmp_path), PROJECTS_DB) == 0, "touch committed a transaction it does not own"
    await (await cm.get(PROJECTS_DB)).rollback()


async def _exercise(call: Callable[[], Awaitable[object]], base_dir: str) -> int:
    """Run `call` against a borrowed transaction and answer what became durable."""
    cm = AsyncConnectionManager(base_dir=base_dir)
    await _leave_foreign_work_in_flight(cm)
    await call()
    rows = _durable_rows(base_dir)
    await (await cm.get(DB_NAME)).rollback()
    return rows


@pytest.mark.asyncio
async def test_the_observer_sees_a_foreign_row_once_it_is_committed(tmp_path: Path) -> None:
    """The instrument itself: without this, a `== 0` assertion above proves nothing.

    A test that asserts "nothing was flushed" passes trivially if the observer can never see a
    flushed row. This one commits the foreign row deliberately and requires the observer to report
    it.
    """
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await _leave_foreign_work_in_flight(cm)
    await (await cm.get(DB_NAME)).commit()

    assert _durable_rows(str(tmp_path)) == 1, "the observer cannot see a committed row, so it proves nothing"


def test_projects_db_is_a_separate_file_from_memory_db() -> None:
    """The shared handle is per database, which bounds the blast radius of a bad commit."""
    assert PROJECTS_DB != DB_NAME


# ---------------------------------------------------------------------------
# The remaining best-effort sites. Each is one call site of the same shape:
# take the shared handle, do one DML statement, commit unconditionally.
# ---------------------------------------------------------------------------


async def _counterpart(
    base_dir: str,
    *,
    prepare: Callable[[AsyncConnectionManager], Awaitable[object]],
    act: Callable[[AsyncConnectionManager], Awaitable[object]],
    db_name: str = DB_NAME,
) -> int:
    """Prepare schema, open a stranger's transaction, run `act`, answer what became durable.

    Preparation is a separate step on purpose: it commits its own work, so whatever the borrowed
    window contains afterwards is only what `act` did.
    """
    cm = AsyncConnectionManager(base_dir=base_dir)
    await prepare(cm)
    await _leave_foreign_work_in_flight(cm, db_name)
    await act(cm)
    rows = _durable_rows(base_dir, db_name)
    await (await cm.get(db_name)).rollback()
    return rows


@pytest.mark.asyncio
async def test_episodic_delete_older_than_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """A layer purge of recent episodes is housekeeping, not a transaction owner."""
    from core.episodic import EpisodicMemory

    async def prepare(cm: AsyncConnectionManager) -> None:
        await EpisodicMemory(cm=cm, layer="user")._init_db()

    async def act(cm: AsyncConnectionManager) -> None:
        await EpisodicMemory(cm=cm, layer="user").delete_older_than("u1", time.time())

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0


@pytest.mark.asyncio
async def test_core_memory_delete_older_than_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """Same purge shape one layer up, on `core_memory`."""
    from core.memory import CoreMemory

    async def prepare(cm: AsyncConnectionManager) -> None:
        await CoreMemory(cm=cm, layer="user")._init_db()

    async def act(cm: AsyncConnectionManager) -> None:
        await CoreMemory(cm=cm, layer="user").delete_older_than("u1", time.time())

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0


@pytest.mark.asyncio
async def test_audit_cleanup_old_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """Retention pruning of the audit log."""
    from features.audit_trail import AuditTrail

    async def prepare(cm: AsyncConnectionManager) -> None:
        await AuditTrail(cm)._init_db()

    async def act(cm: AsyncConnectionManager) -> None:
        await AuditTrail(cm).cleanup_old(30)

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0


@pytest.mark.asyncio
async def test_record_recall_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """Telemetry for a recall: `ensure()` runs first, so this also covers the schema chokepoint."""
    from features.recall_telemetry import record_recall

    async def prepare(cm: AsyncConnectionManager) -> None:
        return None

    async def act(cm: AsyncConnectionManager) -> None:
        await record_recall(cm, "default", "u1", "a query", "balanced", 3)

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0


@pytest.mark.asyncio
async def test_record_recall_useful_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """One `recall_useful` audit row per entry; a best-effort counter, not a durable write."""
    from features.audit_trail import AuditTrail
    from features.replay import record_recall_useful

    async def prepare(cm: AsyncConnectionManager) -> None:
        await AuditTrail(cm)._init_db()

    async def act(cm: AsyncConnectionManager) -> None:
        await record_recall_useful(cm, "default", "u1", [(1, "k")])

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0


@pytest.mark.asyncio
async def test_prune_dangling_edges_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """The central liveness sweep: dust, not debt — it may ride along, not flush."""
    from lifecycle.graph_sanitation import prune_dangling_edges

    async def prepare(cm: AsyncConnectionManager) -> None:
        await cm.execute_script(
            DB_NAME,
            """CREATE TABLE IF NOT EXISTS epi_nodes (node_id TEXT PRIMARY KEY);
               CREATE TABLE IF NOT EXISTS epi_edges (source_id TEXT, target_id TEXT);""",
        )

    async def act(cm: AsyncConnectionManager) -> None:
        await prune_dangling_edges(cm)

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0


@pytest.mark.asyncio
async def test_dream_buffer_cleanup_old_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """Staging-buffer retention: a bounded side buffer beside the pipeline."""
    from shared.dream_buffer import DreamBuffer

    async def prepare(cm: AsyncConnectionManager) -> None:
        await DreamBuffer(cm=cm, layer="user").ensure()

    async def act(cm: AsyncConnectionManager) -> None:
        await DreamBuffer(cm=cm, layer="user").cleanup_old()

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0


@pytest.mark.asyncio
async def test_staging_purge_decided_past_window_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """`purge_decided_past_window` takes no manager, so the module-global one is pointed at tmp_path."""
    from features.staging import purge_decided_past_window
    from shared.connection import connection_manager

    original = connection_manager.base_dir
    await connection_manager.close_all()
    connection_manager.base_dir = tmp_path
    try:
        await connection_manager.execute_script(
            DB_NAME,
            """CREATE TABLE IF NOT EXISTS mutation_proposals (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   status TEXT, decided_at REAL, expires_at REAL, proposed_at REAL)""",
        )
        await _leave_foreign_work_in_flight(connection_manager)
        await purge_decided_past_window()
        assert _durable_rows(str(tmp_path)) == 0, "purge_decided_past_window flushed a stranger's work"
        await (await connection_manager.get(DB_NAME)).rollback()
    finally:
        await connection_manager.close_all()
        connection_manager.base_dir = original


# ---------------------------------------------------------------------------
# `lifecycle/graph_miners.py`: the miners and loggers. Fifteen sites of the same
# shape in one file; three are driven here (each ensures its own schema), and the
# remaining twelve share the identical one-line change.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_miner_tags_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """A miner's edges are recomputed from the graph, so losing them is not a data loss."""
    from lifecycle.graph_miners import miner_tags

    async def prepare(cm: AsyncConnectionManager) -> None:
        # `miner_tags` joins epi_tags against itself and against epi_nodes. The tables may be
        # empty: the function reaches its commit whether or not it found any shared tag, which is
        # why this test proves the guard rather than the mining.
        await cm.execute_script(
            DB_NAME,
            """CREATE TABLE IF NOT EXISTS epi_nodes (
                   node_id INTEGER PRIMARY KEY, layer TEXT, content TEXT, tags TEXT DEFAULT '[]');
               CREATE TABLE IF NOT EXISTS epi_tags (node_id INTEGER, tag TEXT);
               CREATE TABLE IF NOT EXISTS epi_edges (
                   source_id INTEGER, target_id INTEGER, kind TEXT, weight REAL DEFAULT 0.0,
                   UNIQUE(source_id, target_id, kind));""",
        )

    async def act(cm: AsyncConnectionManager) -> None:
        await miner_tags(cm, "user")

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0


@pytest.mark.asyncio
async def test_log_co_pairs_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """Co-retrieval pairs are observability for ranking, not memory."""
    from lifecycle.graph_miners import log_co_pairs

    async def prepare(cm: AsyncConnectionManager) -> None:
        return None

    async def act(cm: AsyncConnectionManager) -> None:
        # `_hit_ref` reads `hit["id"]`; two nonzero ids are needed to reach the insert loop.
        await log_co_pairs(cm, "a query", [{"id": 1}, {"id": 2}])

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0


@pytest.mark.asyncio
async def test_log_zero_result_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """A zero-result log line: pure best-effort observation."""
    from lifecycle.graph_miners import log_zero_result

    async def prepare(cm: AsyncConnectionManager) -> None:
        return None

    async def act(cm: AsyncConnectionManager) -> None:
        await log_zero_result(cm, "user", "u1", "a query that found nothing")

    assert await _counterpart(str(tmp_path), prepare=prepare, act=act) == 0
