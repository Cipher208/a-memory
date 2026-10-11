"""The rate limiter commits a connection it does not own.

`AsyncConnectionManager.get()` hands out **one connection per database**
(`shared/connection.py:213-234`), and `RateLimiter.check()` runs at the top of every tool call
(`mcp_server/tools/base.py:149`). Whatever it commits, it commits on behalf of every component
that has a write in flight at that moment — so a limiter that only needs its own counter to
survive can flush somebody else's half-finished transaction to disk.

Every test here observes from a **second** connection. That is the only observer that can tell
a pending write from a durable one: read back on the shared handle, the assertion passes
whether or not the commit happened, and measures nothing at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite

from features.rate_limiting import RateLimiter
from shared.connection import AsyncConnectionManager

if TYPE_CHECKING:
    from pathlib import Path

DB = "memory.db"
USER = "u1"


async def _limiter(tmp_path: Path) -> tuple[AsyncConnectionManager, RateLimiter]:
    """A limiter over its own database, table ready."""
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    rl = RateLimiter(cm=cm)
    await rl._init_db()
    return cm, rl


async def _somebody_elses_work_in_flight(cm: AsyncConnectionManager) -> None:
    """Leave an uncommitted write on the shared handle, the state a component is in mid-write.

    `execute_script` runs as a script, which commits, so the scratch table is really on disk
    and the INSERT below is the only thing in flight.
    """
    await cm.execute_script(DB, "CREATE TABLE IF NOT EXISTS foreign_work (v TEXT);")
    conn = await cm.get(DB)
    await conn.execute("INSERT INTO foreign_work VALUES ('in flight, not committed')")
    assert conn.in_transaction, "the setup must leave a real pending transaction"


async def _on_disk(tmp_path: Path, sql: str) -> int:
    """What a second connection sees — the only view that separates pending from durable."""
    async with aiosqlite.connect(str(tmp_path / DB)) as other:
        row = await (await other.execute(sql)).fetchone()
    return int(row[0]) if row else 0


async def test_a_rate_check_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """Counting one request must not put another component's unfinished work on disk.

    This is the defect: `check()` ended in an unconditional `commit()` on a handle it shares.
    It runs before every tool's own logic, so it was the widest-reaching instance of the shape
    — a method committing a connection it did not open.
    """
    cm, rl = await _limiter(tmp_path)
    await _somebody_elses_work_in_flight(cm)

    before = await _on_disk(tmp_path, "SELECT COUNT(*) FROM foreign_work")
    assert before == 0, "nothing should be durable before the rate check"

    result = await rl.check(USER)

    after = await _on_disk(tmp_path, "SELECT COUNT(*) FROM foreign_work")

    assert result["allowed"] is True, "the check itself must still answer"
    assert after == 0, f"the rate check committed another component's pending write: {after} row(s) durable"


async def test_a_rate_check_still_commits_its_own_window(tmp_path: Path) -> None:
    """When nothing else is in flight, the window must survive — the fix is not a deletion.

    A limiter whose own counter never reaches disk is a different bug wearing the fix's clothes:
    `remaining` would be right in-process and wrong after a restart. This test was green before
    the fix and is here so it cannot be made green by simply removing the commit.
    """
    _cm, rl = await _limiter(tmp_path)

    result = await rl.check(USER)

    assert result["allowed"] is True
    assert await _on_disk(tmp_path, "SELECT COUNT(*) FROM rate_limits") == 1, "the window did not reach the disk"


async def test_cleanup_old_does_not_commit_a_borrowed_transaction(tmp_path: Path) -> None:
    """The same shape in the same class: `cleanup_old` commits a handle it does not own either."""
    cm, rl = await _limiter(tmp_path)
    await _somebody_elses_work_in_flight(cm)

    await rl.cleanup_old()

    assert await _on_disk(tmp_path, "SELECT COUNT(*) FROM foreign_work") == 0, "cleanup_old committed another component's pending write"
