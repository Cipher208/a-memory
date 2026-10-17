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

    commits: list[int] = []
    real_commit = conn.commit

    async def counting_commit():
        commits.append(1)
        return await real_commit()

    conn.commit = counting_commit

    fs = ForgettingSystem(cm=cm)
    n = await fs.decay_importance()
    assert n == N, f"every row should decay, got {n}"
    assert len(commits) > 1, (
        "decay must commit in chunks so the SQLite write lock is released "
        "between batches — one long transaction is what made tool writes fail "
        "with 'database is locked'"
    )
