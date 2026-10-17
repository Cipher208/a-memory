"""Closing helpers for the synchronous SQLite connections in ``features/`` and ``shared/``.

``with sqlite3.connect(...)`` is a *transaction* context manager, not a resource one:
``Connection.__exit__`` commits or rolls back and leaves the connection **open**. In a
short-lived script the garbage collector hides that. In the autohooks daemon, which
serves every event for hours, it is a descriptor leak — one per call, one per hook.

Measured, not inferred:

    with sqlite3.connect(...)   x 50  ->  +50 descriptors still open (gc disabled)
    live daemon, 10 minutes after start -> 29 descriptors on memory.db, growing

Those unclosed connections are also what makes ``database is locked`` reach other
writers: a connection left mid-transaction holds the write lock, and the next writer
waits out ``busy_timeout`` and fails. Proven on the live base — a killed orphan holding
memory.db made ``BEGIN IMMEDIATE`` fail for 2 s from outside while ``CHECKPOINT`` stayed
free; after it was killed, the same probe returned in 0.1 ms.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextlib.contextmanager
def open_db(
    target: Any,
    *,
    uri: bool = False,
    timeout: float = 5.0,
    row_factory: Any = None,
) -> Iterator[sqlite3.Connection]:
    """Open a synchronous connection and **close** it on the way out.

    Transaction semantics match ``with sqlite3.connect(...)``: commit on a clean exit,
    rollback on an exception. The only addition is the ``close()`` that the built-in
    context manager never performs.
    """
    conn = sqlite3.connect(str(target), timeout=timeout, uri=uri)
    try:
        if row_factory is not None:
            conn.row_factory = row_factory
        yield conn
        conn.commit()
    finally:
        # `close()` on a connection with an open transaction rolls it back, so the
        # error path needs no explicit rollback — a mutation that removed one survived
        # the whole suite, because there was nothing left for it to change.
        conn.close()
