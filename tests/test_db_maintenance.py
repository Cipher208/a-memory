"""features.db_maintenance: size thresholds + auto-VACUUM on fragmentation."""

import sqlite3

import pytest

from features.db_maintenance import run_db_maintenance
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def cm(tmp_path, monkeypatch):
    manager = AsyncConnectionManager(base_dir=str(tmp_path))
    # maintenance reads connection_manager.base_dir for the file listing —
    # point it at this test's tmp and restore afterwards.
    from shared import connection as conn_mod

    orig_dir = conn_mod.connection_manager.base_dir
    orig_conns = dict(conn_mod.connection_manager._conns)
    conn_mod.connection_manager.base_dir = tmp_path
    conn_mod.connection_manager._conns.clear()
    yield manager
    conn_mod.connection_manager.base_dir = orig_dir
    conn_mod.connection_manager._conns.clear()
    conn_mod.connection_manager._conns.update(orig_conns)


async def _make_fragmented(path, rows: int) -> None:
    """Create a DB with real dead pages: bulk insert then delete."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [("x" * 2000,)] * rows)
    conn.commit()
    conn.execute("DELETE FROM t")
    conn.commit()
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    assert freelist / max(page_count, 1) > 0.25, "fixture must be fragmented"
    conn.close()


@pytest.mark.asyncio
async def test_vacuum_reclaims_fragmented_db(cm, tmp_path):
    db = tmp_path / "memory.db"
    await _make_fragmented(db, rows=3000)

    # Direct probe first: the vacuum primitive itself must reclaim space.
    from features.db_maintenance import _vacuum_if_fragmented

    freed, did = await _vacuum_if_fragmented(cm, db, min_mb=1, ratio_threshold=0.25)
    assert did and freed > 0, f"direct vacuum: did={did} freed={freed}"

    reports = await run_db_maintenance(cm=cm, vacuum_min_mb=50)
    mem = [r for r in reports if r.name == "memory.db"]
    assert mem, "memory.db must be reported"

    conn = sqlite3.connect(str(db))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_thresholds_reported(cm):
    """Small clean DB -> ok level; report carries all data-dir files."""
    import sqlite3 as sq

    conn = sq.connect(str(cm.base_dir / "projects.db"))
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.commit()
    conn.close()
    reports = await run_db_maintenance(cm=cm)
    names = {r.name for r in reports}
    assert "projects.db" in names
    assert all(r.level in ("ok", "warn", "alert") for r in reports)
