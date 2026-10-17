from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shared.connection import AsyncConnectionManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence


class WriteDispatch:
    """Sole owner of a dedicated SQLite connector and its transactions.

    A cursor never escapes a method body: every query opens, reads, and closes
    its cursor inside an ``async with``. No read transaction is held between
    calls, so a snapshot cannot go stale relative to a foreign commit and the
    BUSY_SNAPSHOT (517) failure class is impossible at this API.

    The connector is private to this dispatch, built from the supplied
    manager's base directory. A cursor a caller retains on the shared manager's
    connection keeps its snapshot on that other connection, so it can never
    stale the one used here.
    """

    def __init__(self, connection_manager: AsyncConnectionManager) -> None:
        self._cm = AsyncConnectionManager(base_dir=str(connection_manager.base_dir))

    async def query(self, db: str, sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]:
        conn: Any = await self._cm.get(db)
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def select_one(self, db: str, sql: str, params: Sequence[Any] = ()) -> Mapping[str, Any] | None:
        conn: Any = await self._cm.get(db)
        async with conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def scalar(self, db: str, sql: str, params: Sequence[Any] = ()) -> Any:
        conn: Any = await self._cm.get(db)
        async with conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return row[0]

    async def write(self, db: str, sql: str, params: Sequence[Any] = ()) -> int:
        conn: Any = await self._cm.get(db)
        async with conn.execute(sql, params) as cur:
            rowcount = cur.rowcount
        await conn.commit()
        return int(rowcount or 0)

    async def write_many(self, db: str, ops: Sequence[tuple[str, Sequence[Any]]]) -> None:
        """Execute all ops in one transaction; roll back on any failure."""
        conn: Any = await self._cm.get(db)
        async with conn.cursor() as cur:
            try:
                for sql, params in ops:
                    await cur.execute(sql, params)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def fetch_many(
        self,
        db: str,
        sql: str,
        params: Sequence[Any] = (),
        chunk: int = 1000,
    ) -> AsyncIterator[list[Mapping[str, Any]]]:
        conn: Any = await self._cm.get(db)
        async with conn.execute(sql, params) as cur:
            while True:
                batch = await cur.fetchmany(chunk)
                if not batch:
                    break
                yield [dict(r) for r in batch]

    async def close(self) -> None:
        await self._cm.close_all()
