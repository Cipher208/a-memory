"""The transient boundary of a SQLite failure, decided by CODE.

A failure is transient when it clears by itself: the work was not done, and
doing it again later is the right thing to do. For SQLite that is exactly two
codes — `SQLITE_BUSY` (another writer holds the lock) and
`SQLITE_BUSY_SNAPSHOT` (this connection's read snapshot went stale and
cannot be upgraded). Everything else is permanent: a missing column, a broken
uniqueness constraint, a table that does not exist. No amount of waiting
brings those back, and a caller that retries them forever stalls on one
undeliverable message.

THE CLASS IS NOT THE BOUNDARY. `sqlite3.Error` covers the permanent failures
too, so "is it a sqlite error" is the wrong question: a daemon that asked it
held its cursor forever on a `ProgrammingError`, which is precisely the
poison stall the classification exists to prevent.

The text fallback answers the question for an error that carries no code.
`sqlite3.OperationalError("database is locked")` built in Python has no
`sqlite_errorcode` attribute at all — the attribute is set by the C layer on
a REAL error — so a hand-built busy error is classified by its text. This is
the behaviour the write dispatcher has always had, kept here so that there is
ONE answer to the question in the repository.

The caller decides what to do with the answer. The registry marks a failed
handler transient or permanent; the daemon holds its cursor for the transient
case and moves on for the permanent one.
"""

from __future__ import annotations

import sqlite3

#: Another connection holds the write lock.
SQLITE_BUSY = 5

#: This connection's read snapshot went stale and cannot be upgraded to a
#: write transaction. SQLite does not consult the busy handler for it, so it
#: arrives as an error rather than as a wait.
SQLITE_BUSY_SNAPSHOT = 517


def is_busy(exc: BaseException) -> bool:
    """Report whether the error means "another writer holds the lock".

    The code decides, when the error carries one. A real SQLite error always
    does: `sqlite_errorcode` is the extended result code. An error built in
    Python does not, and then the text answers it — the repository's
    long-standing fallback, kept because a hand-built
    `OperationalError("database is locked")` is exactly how the busy case is
    reproduced in a test and how a driver may report it.

    A non-SQLite exception is never busy: it is a bug in the caller.
    """
    if not isinstance(exc, sqlite3.Error):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code in (SQLITE_BUSY, SQLITE_BUSY_SNAPSHOT)
    text = str(exc).lower()
    return "locked" in text or "busy" in text
