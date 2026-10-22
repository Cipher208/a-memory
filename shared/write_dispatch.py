from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TypeVar
from weakref import WeakKeyDictionary

from shared.connection import AsyncConnectionManager, connection_manager
from shared.lock_owners import describe_lock_holders

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
    from types import FrameType

_T = TypeVar("_T")

#: The (database, connection) a task already owns inside ``atomic``. Nesting is
#: per task, so a body that calls back into the dispatcher joins the transaction
#: it is already in instead of deadlocking on the lock it already holds.
_active: ContextVar[tuple[str, Any] | None] = ContextVar("write_dispatch_active", default=None)

logger = logging.getLogger(__name__)

#: A write that takes longer than this is reported together with the phase of
#: its CALLER. The long holder of a write lock is a code path in some process,
#: and that phase is the only thing that names it -- the dispatcher's own
#: frames name nothing. Tunable: the tests lower it.
_SLOW_WRITE_WARN_S = 2.0

#: SQLite's "another writer holds the lock" codes: BUSY for a write lock,
#: BUSY_SNAPSHOT for a stale read snapshot that cannot be upgraded.
_SQLITE_BUSY = 5
_SQLITE_BUSY_SNAPSHOT = 517


def _caller_frame() -> FrameType | None:
    """Return the first stack frame outside this module, if there is one.

    The frame is captured while the caller's stack is still live. The phase is
    only read when a write turns out to be slow, and by then the frames of the
    resumed coroutine describe the event loop, not the caller.
    """
    frame = inspect.currentframe()
    try:
        while frame is not None:
            if frame.f_globals.get("__name__") != __name__:
                return frame
            frame = frame.f_back
    finally:
        del frame
    return None


def _phase_of(frame: FrameType | None) -> str:
    """Render a frame as ``module:function:line`` for the log."""
    if frame is None:
        return "unknown:unknown:0"
    module = frame.f_globals.get("__name__", "unknown")
    return f"{module}:{frame.f_code.co_name}:{frame.f_lineno}"


def _is_busy(exc: sqlite3.Error) -> bool:
    """Report whether the error means "another writer holds the lock"."""
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code in (_SQLITE_BUSY, _SQLITE_BUSY_SNAPSHOT)
    text = str(exc).lower()
    return "locked" in text or "busy" in text


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

    Every entry point takes the same per-loop lock, so a body running inside
    :meth:`atomic` owns the connection end to end and no other coroutine can
    interleave a statement into its transaction. That is what makes a single
    connection safe for both ordinary writes and read-then-write critical
    sections. A call made by the same task from inside an ``atomic`` callback
    joins that transaction rather than waiting for itself.
    """

    def __init__(self, connection_manager: AsyncConnectionManager) -> None:
        self._cm = AsyncConnectionManager(base_dir=str(connection_manager.base_dir))
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_lock(self) -> asyncio.Lock:
        """Return a lock bound to the running loop.

        A dispatch can outlive the loop it was first used from (tests drive
        several loops through one manager); rebinding on a loop change keeps
        ``asyncio.Lock`` from raising "attached to a different loop" while
        preserving serialization inside any single loop.
        """
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    @asynccontextmanager
    async def _borrow(self, db: str) -> AsyncIterator[tuple[Any, bool]]:
        """Yield ``(conn, nested)``; ``nested`` means the caller already owns the txn."""
        active = _active.get()
        if active is not None and active[0] == db:
            yield active[1], True
            return
        async with self._get_lock():
            conn: Any = await self._cm.get(db)
            token = _active.set((db, conn))
            try:
                yield conn, False
            finally:
                _active.reset(token)

    async def query(self, db: str, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async with self._borrow(db) as (conn, _nested):
            cur = await conn.execute(sql, params)
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        return [dict(r) for r in rows]

    async def select_one(self, db: str, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        async with self._borrow(db) as (conn, _nested):
            cur = await conn.execute(sql, params)
            try:
                row = await cur.fetchone()
            finally:
                await cur.close()
        return dict(row) if row is not None else None

    async def scalar(self, db: str, sql: str, params: Sequence[Any] = ()) -> Any:
        async with self._borrow(db) as (conn, _nested):
            cur = await conn.execute(sql, params)
            try:
                row = await cur.fetchone()
            finally:
                await cur.close()
        if row is None:
            return None
        return row[0]

    @asynccontextmanager
    async def _write_scope(self, kind: str, db: str, caller: FrameType | None) -> AsyncIterator[None]:
        """Time one write transaction and attribute a busy failure to its holder.

        A write that takes longer than ``_SLOW_WRITE_WARN_S`` is reported with
        the phase of its caller, and a write that fails with SQLITE_BUSY is
        reported with the OS-level holders of the base's locks. Both are
        diagnostics: the transaction boundaries are exactly what they were, and
        the only cost on the happy path is one clock read.
        """
        started = time.monotonic()
        try:
            yield
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                # A diagnostic must never turn a recoverable failure into a
                # crash, so everything it touches is contained here.
                with suppress(Exception):
                    self._report_busy(kind, db, exc, caller)
            raise
        else:
            elapsed = time.monotonic() - started
            if elapsed > _SLOW_WRITE_WARN_S:
                logger.warning(
                    "slow write: kind=%s duration=%.3fs phase=%s db=%s",
                    kind,
                    elapsed,
                    _phase_of(caller),
                    db,
                )

    def _report_busy(self, kind: str, db: str, exc: sqlite3.OperationalError, caller: FrameType | None) -> None:
        """Log who holds the lock that just blocked this write.

        The SQLite code says that somebody holds it; the holder list says who.
        Only the base directory, the code and the phase are logged -- no
        values, no keys, nothing that was being written.
        """
        code = getattr(exc, "sqlite_errorcode", None)
        name = getattr(exc, "sqlite_errorname", None) or "unknown"
        base = str(self._cm.base_dir)
        holders = describe_lock_holders(base)
        logger.error(
            "sqlite busy on write: kind=%s code=%s name=%s base=%s db=%s phase=%s\nlock holders:\n%s",
            kind,
            code,
            name,
            base,
            db,
            _phase_of(caller),
            "\n".join(holders),
        )

    async def write(self, db: str, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._write_scope("write", db, _caller_frame()), self._borrow(db) as (conn, nested):
            cur = await conn.execute(sql, params)
            try:
                rowcount = cur.rowcount
            finally:
                await cur.close()
            if not nested:
                await conn.commit()
        return int(rowcount or 0)

    async def insert(self, db: str, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute one INSERT and return its ``lastrowid``.

        ``write`` reports the rowcount; an INSERT caller usually needs the id
        instead, and reading it outside the cursor is impossible at this API.
        """
        async with self._write_scope("insert", db, _caller_frame()), self._borrow(db) as (conn, nested):
            cur = await conn.execute(sql, params)
            try:
                lastrowid = cur.lastrowid
            finally:
                await cur.close()
            if not nested:
                await conn.commit()
        return int(lastrowid or 0)

    async def execute_script(self, db: str, script: str) -> None:
        """Run a multi-statement DDL script (schema creation) and commit."""
        async with self._write_scope("execute_script", db, _caller_frame()), self._borrow(db) as (conn, nested):
            await conn.executescript(script)
            if not nested:
                await conn.commit()

    async def write_many(self, db: str, ops: Sequence[tuple[str, Sequence[Any]]]) -> None:
        """Execute all ops in one transaction; roll back on any failure."""
        async with (
            self._write_scope("write_many", db, _caller_frame()),
            self._borrow(db) as (conn, nested),
            conn.cursor() as cur,
        ):
            try:
                for sql, params in ops:
                    await cur.execute(sql, params)
                if not nested:
                    await conn.commit()
            except Exception:
                if not nested:
                    await conn.rollback()
                raise

    async def atomic(self, db: str, fn: Callable[[Any], Awaitable[_T]]) -> _T:
        """Run ``fn(conn)`` inside one transaction on the dispatch's connection.

        The only supported way to do read-then-write atomically (the L0 hash
        chain reads the previous digest, then inserts and updates it). Commit
        on success, rollback on any exception.

        A call made from inside another ``atomic`` body joins that outer
        transaction: the statements become one unit and only the outermost
        frame commits. This keeps a domain writer callable from a maintenance
        body without a self-deadlock.
        """
        async with self._write_scope("atomic", db, _caller_frame()), self._borrow(db) as (conn, nested):
            if nested:
                return await fn(conn)
            try:
                result = await fn(conn)
                await conn.commit()
                return result
            except BaseException:
                await conn.rollback()
                raise

    async def fetch_many(
        self,
        db: str,
        sql: str,
        params: Sequence[Any] = (),
        chunk: int = 1000,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        async with self._borrow(db) as (conn, _nested):
            cur = await conn.execute(sql, params)
            try:
                while True:
                    batch = await cur.fetchmany(chunk)
                    if not batch:
                        break
                    yield [dict(r) for r in batch]
            finally:
                await cur.close()

    async def close(self) -> None:
        await self._cm.close_all()


#: One dispatcher per (manager, data directory), so the whole process writes
#: through a single connection. Keyed weakly: a manager that goes away takes its
#: dispatcher with it instead of pinning a dead connection for the life of the
#: process. The directory is part of the key because a manager can be repointed
#: (the test harness does exactly that) and a dispatcher on the old directory
#: would silently write to the wrong database.
_dispatchers: WeakKeyDictionary[AsyncConnectionManager, tuple[str, WriteDispatch]] = WeakKeyDictionary()


def get_write_dispatch(cm: AsyncConnectionManager | None = None) -> WriteDispatch:
    """Return the process-wide dispatcher for ``cm`` (the global manager by default).

    Every subsystem passes its own manager, so a test that injects a temporary
    manager gets its own dispatcher while production code — which everywhere
    defaults to the global ``connection_manager`` — shares exactly one.
    """
    manager = cm or connection_manager
    base = str(manager.base_dir)
    entry = _dispatchers.get(manager)
    if entry is not None and entry[0] == base:
        return entry[1]
    dispatch = WriteDispatch(manager)
    _dispatchers[manager] = (base, dispatch)
    return dispatch


async def close_dispatchers_for(manager: AsyncConnectionManager) -> None:
    """Close the dispatcher built on ``manager``, if one exists.

    Called from ``AsyncConnectionManager.close_all``: the dispatcher's connector is a
    separate set of handles, and their non-daemon worker threads keep a finished CLI
    alive until they are closed.
    """
    entry = _dispatchers.pop(manager, None)
    if entry is not None:
        await entry[1].close()


async def reset_write_dispatchers() -> None:
    """Close and forget every dispatcher (test teardown only)."""
    for _base, dispatch in list(_dispatchers.values()):
        await dispatch.close()
    _dispatchers.clear()
