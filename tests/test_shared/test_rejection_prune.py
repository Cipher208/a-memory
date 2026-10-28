"""F107 — the rejection log must not grow forever.

`memory_dispatch_rejections` recorded every refusal and had no prune path, so
one base carried 28 425 rows (27 048 of them the forensic tail of the F96
duplicate bug). The bound has two passes: retention for age, a hard cap for
size. These tests drive the real function against real SQLite.
"""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from shared.connection import connection_manager
from shared.door_log import prune_rejections

if TYPE_CHECKING:
    from pathlib import Path

_DDL = """
    CREATE TABLE IF NOT EXISTS memory_dispatch_rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event TEXT NOT NULL DEFAULT '',
        source_msg_id INTEGER,
        layer TEXT NOT NULL DEFAULT 'user',
        user_id TEXT NOT NULL DEFAULT 'default',
        reason TEXT NOT NULL,
        text_preview TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS l0_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_hash TEXT
    );
"""

_DAY = 86400.0


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Path:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()
    connection_manager.base_dir = original


def _db(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path / "memory.db")


def _seed(path: Path, created_ats: list[float]) -> list[int]:
    conn = _db(path)
    conn.executescript(_DDL)
    ids: list[int] = []
    for created_at in created_ats:
        cur = conn.execute(
            "INSERT INTO memory_dispatch_rejections (reason, created_at) VALUES (?, ?)",
            ("rate_limit", created_at),
        )
        ids.append(int(cur.lastrowid or 0))
    conn.commit()
    conn.close()
    return ids


def _surviving_ids(path: Path) -> list[int]:
    conn = _db(path)
    ids = [int(r[0]) for r in conn.execute("SELECT id FROM memory_dispatch_rejections ORDER BY id")]
    conn.close()
    return ids


async def test_old_rows_are_deleted_and_fresh_rows_kept(fresh_dir: Path) -> None:
    now = time.time()
    _seed(fresh_dir, [now - 40 * _DAY] * 3 + [now, now])

    result = await prune_rejections(retention_days=30, max_rows=5000)

    assert result["deleted_old"] == 3
    assert result["remaining"] == 2
    assert _surviving_ids(fresh_dir) == [4, 5]


async def test_the_cap_deletes_the_oldest_rows_first(fresh_dir: Path) -> None:
    now = time.time()
    ids = _seed(fresh_dir, [now - i for i in range(8)])

    result = await prune_rejections(retention_days=100_000, max_rows=3)

    assert result["deleted_over_cap"] == 5
    assert result["remaining"] == 3
    assert _surviving_ids(fresh_dir) == ids[5:]


async def test_nothing_happens_when_under_both_limits(fresh_dir: Path) -> None:
    now = time.time()
    _seed(fresh_dir, [now, now])

    result = await prune_rejections(retention_days=30, max_rows=10)

    assert result == {"deleted_old": 0, "deleted_over_cap": 0, "remaining": 2}


async def test_batches_loop_until_done(fresh_dir: Path) -> None:
    old = time.time() - 40 * _DAY
    _seed(fresh_dir, [old] * 5)

    result = await prune_rejections(retention_days=30, max_rows=5000, batch=2)

    assert result["deleted_old"] == 5
    assert result["remaining"] == 0


async def test_a_missing_table_is_not_an_error(fresh_dir: Path) -> None:
    result = await prune_rejections(retention_days=30, max_rows=5000)

    assert result == {"deleted_old": 0, "deleted_over_cap": 0, "remaining": 0}


async def test_only_the_rejections_table_is_touched(fresh_dir: Path) -> None:
    now = time.time()
    _seed(fresh_dir, [now - 40 * _DAY] * 3)
    conn = _db(fresh_dir)
    conn.execute("INSERT INTO l0_journal (content_hash) VALUES ('keep-me')")
    conn.commit()
    conn.close()

    await prune_rejections(retention_days=30, max_rows=5000)

    conn = _db(fresh_dir)
    rows = conn.execute("SELECT content_hash FROM l0_journal").fetchall()
    conn.close()
    assert rows == [("keep-me",)]
