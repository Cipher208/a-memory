"""Queue hygiene: decided-proposal tombstone + consolidation pre-gate + WAL size cap.

Bug class (2026-09-12/13): session_close re-proposed the same five rejected
keys (#80-84 after #75-79) because propose() deduped only against PENDING
rows; and the autohooks consolidation cycle proposed items whose importance
could never pass the apply gate (mimocode #85/#86 with imp 0.5 at gate 0.7),
so approving them was a guaranteed no-op that still consumed operator time.
Plus: a leaked read snapshot starved WAL auto-checkpoint to 3.9 GB; an
explicit journal_size_limit caps the damage SQLite can do under any future
checkpoint starvation.
"""

from __future__ import annotations

import json
import sqlite3
import time
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, kind TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default', layer TEXT NOT NULL DEFAULT 'user',
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            proposed_at REAL NOT NULL, expires_at REAL NOT NULL,
            decided_at REAL, decided_by TEXT, result_ref TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            action TEXT NOT NULL, layer TEXT, target_id TEXT, details TEXT, timestamp REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return fresh_dir


def _mark_decided(ensure_schema: Path, pid: int, status: str, decided_at: float) -> None:
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute(
        "UPDATE mutation_proposals SET status=?, decided_at=?, decided_by='tool' WHERE id=?",
        (status, decided_at, pid),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_propose_tombstone_rejected_key_absorbed(ensure_schema: Path) -> None:
    """A fresh propose with a key rejected yesterday must NOT create a new row."""
    pid = await staging.propose("session_close", "core_write", "u1", "user", {"key": "same", "value": "v", "importance": 0.9})
    _mark_decided(ensure_schema, pid, "rejected", time.time())

    again = await staging.propose("session_close", "core_write", "u1", "user", {"key": "same", "value": "v2", "importance": 0.9})

    assert again == pid  # absorbed: caller learns the decided id
    conn = sqlite3.connect(ensure_schema / "memory.db")
    n = conn.execute("SELECT COUNT(*) FROM mutation_proposals WHERE id=?", (pid,)).fetchone()[0]
    assert n == 1  # no zombie row, no re-opened pending
    conn.close()


@pytest.mark.asyncio
async def test_propose_tombstone_expired_key_absorbed(ensure_schema: Path) -> None:
    """Same for expired decisions — expiry is a decision too."""
    pid = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "echo", "value": "v", "importance": 0.9})
    _mark_decided(ensure_schema, pid, "expired", time.time())

    again = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "echo", "value": "v", "importance": 0.9})

    assert again == pid


@pytest.mark.asyncio
async def test_propose_tombstone_expires_after_window(ensure_schema: Path) -> None:
    """An old decision must not silence the same key forever (window = expire_days)."""
    pid = await staging.propose("session_close", "core_write", "u1", "user", {"key": "old", "value": "v", "importance": 0.9})
    stale = time.time() - (staging._expire_days() + 1) * 86400
    _mark_decided(ensure_schema, pid, "rejected", stale)

    again = await staging.propose("session_close", "core_write", "u1", "user", {"key": "old", "value": "v", "importance": 0.9})

    assert again != pid
    assert again > 0


@pytest.mark.asyncio
async def test_propose_tombstone_scoped_by_kind_and_source(ensure_schema: Path) -> None:
    """Tombstones must not leak across kinds/sources — different action, different identity."""
    pid = await staging.propose("session_close", "core_write", "u1", "user", {"key": "k", "value": "v", "importance": 0.9})
    _mark_decided(ensure_schema, pid, "rejected", time.time())

    other = await staging.propose("session_close", "wiki_write", "u1", "user", {"title": "k", "content": "c"})
    assert other != pid and other > 0


@pytest.mark.asyncio
async def test_stage_consolidation_skips_subgate_items(ensure_schema: Path) -> None:
    """No proposal at all when every item would be skipped by the apply gate."""
    from lifecycle.consolidation import stage_consolidation

    items = [{"id": 1, "content": "status report: готов, закрыто", "importance": 0.5, "memory_kind": "fact"}]
    pid = await stage_consolidation("u1", "user", items, min_importance=0.7)

    assert pid is None
    conn = sqlite3.connect(ensure_schema / "memory.db")
    assert conn.execute("SELECT COUNT(*) FROM mutation_proposals").fetchone()[0] == 0
    conn.close()


@pytest.mark.asyncio
async def test_stage_consolidation_proposes_only_promotable(ensure_schema: Path) -> None:
    """Mixed batch: the proposal payload must carry only the items that can pass."""
    from lifecycle.consolidation import stage_consolidation

    items = [
        {"id": 1, "content": "хвостовая строка лога", "importance": 0.4, "memory_kind": "fact"},
        {"id": 2, "content": "решение: канон ключей через _canonical_key", "importance": 0.8, "memory_kind": "fact"},
    ]
    pid = await stage_consolidation("u1", "user", items, min_importance=0.7)

    assert pid is not None
    conn = sqlite3.connect(ensure_schema / "memory.db")
    payload = json.loads(conn.execute("SELECT payload FROM mutation_proposals WHERE id=?", (pid,)).fetchone()[0])
    conn.close()
    assert [i["id"] for i in payload["items"]] == [2]


@pytest.mark.asyncio
async def test_wal_journal_size_limit_applied(ensure_schema: Path) -> None:
    """Every managed connection must cap WAL growth (defense against checkpoint starvation)."""
    conn = await connection_manager.get("sized.db")
    (value,) = await (await conn.execute("PRAGMA journal_size_limit")).fetchone()
    assert value == 67108864  # 64 MiB
