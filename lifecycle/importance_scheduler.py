"""Periodic re-scoring importance based on retrieval usage.

Runs as background daemon thread (similar to saga_watchdog and backup_cron).

Algorithm:
    Every scheduler.interval_seconds:
      For each (user_id, item) in core_memory:
        retrieval_count = audit_trail count of 'recall_useful'
        new_score = ImportanceScorer.score(...).total()
        If |Δ| > delta_threshold → UPDATE + INSERT INTO importance_audit
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from shared.connection import connection_manager
from shared.importance import ImportanceScorer
from shared import maintenance_lock

logger = logging.getLogger(__name__)

# How long the sweep's own connection waits for another writer before giving up. The
# same 15 s the shared connection uses (`shared/connection.py: BUSY_TIMEOUT_MS`), but
# applied to a connection nobody else can queue behind.
_SWEEP_TIMEOUT_S = 15.0

# The SQLite error codes the sweep reacts to (`sqlite3.OperationalError.sqlite_errorcode`,
# Python 3.11+):
#   5   SQLITE_BUSY           - another connection holds the write lock. The wait was
#                                the busy timeout, so the lock was held for at least that.
#   517 SQLITE_BUSY_SNAPSHOT  - this connection's read snapshot went stale. It is
#                                instant and never consults the busy timeout.
_SQLITE_BUSY = 5
_SQLITE_BUSY_SNAPSHOT = 517

# How many times one user's rescore may be retried after a busy failure: two retries,
# so three attempts in all. A competing writer that releases the lock is worth waiting
# for; one that never releases it is not, and the pass must give up and say so.
_MAX_USER_RETRIES = 2

# Short pause between two attempts, so a writer that is about to let go of the lock
# has done so before the next attempt re-enters the busy wait.
_RETRY_BACKOFF_S = 0.1

# REVERTED — a retry around the write. It was added on the theory that a momentary BUSY
# would clear if the statement were repeated. Measured since: when the connection holds a
# stale read snapshot, SQLite answers BUSY in well under a millisecond and **never
# consults `busy_timeout`**, so repeating the statement cannot succeed — the transaction
# has to be abandoned, and on a shared connection abandoning it is not this pass's right.
# All the retry did was turn one 15 s wait into four, holding the single aiosqlite worker
# and stalling every tool handler for the duration.
#
# The retry that IS here is a different one: it repeats the statement on a FRESH
# connection, and only for the sweep's own connection, which no handler is waiting on.
# The REVERTED retry repeated it on the same connection, where a stale snapshot cannot
# be shaken off — that is what the measurement above rules out, and it is not what the
# retry below does.


@dataclass
class SchedulerConfig:
    interval_seconds: int = 1800
    user_batch_size: int = 50
    delta_threshold: float = 0.15
    only_recent_days: int = 30
    enabled: bool = True


class ImportanceScheduler:
    """Authored entries keep their authored weight.

    Sources a human (or calibration) wrote by hand are never steamrolled by
    content heuristics — the scheduler only rescores organic rows (E1 18.09).
    """

    CURATED_SOURCES = frozenset(
        {
            "calibration",
            "manual",
            "user_explicit",
            "declared_canon",
            "identity_mirror",
            "staging_promotion",
        }
    )

    def __init__(
        self,
        scorer: ImportanceScorer | None = None,
        scheduler_config: SchedulerConfig | None = None,
    ):
        self.scorer = scorer or ImportanceScorer()
        self.cfg = scheduler_config or SchedulerConfig()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.cfg.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="importance-scheduler")
        self._thread.start()
        logger.info("ImportanceScheduler started (interval=%ds)", self.cfg.interval_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                asyncio.run(self.run_once())
            except Exception:
                logger.exception("Scheduler iteration failed")
            self._stop_event.wait(timeout=self.cfg.interval_seconds)

    async def run_once(self) -> dict[str, int]:
        """Single async iteration, under this base's maintenance lock.

        Every process serving a base started its own sweep and they all wrote
        the same SQLite file: `database is locked` on one side, a WAL that grew
        to 35 MB without checkpointing on the other (measured 14.10.2026). One
        sweep at a time per base. A sweep that cannot claim the lock reports
        zero instead of waiting — the leader is already doing this work, and
        waiting would add a second kind of stall to the one this removes.
        """
        fd = maintenance_lock.acquire(connection_manager.base_dir)
        if fd is None:
            logger.debug("importance sweep skipped: another process is the maintenance leader")
            return {"rescored": 0, "skipped": 0, "errors": 0}
        try:
            return await self._rescore_pass()
        finally:
            maintenance_lock.release(fd)

    async def _rescore_pass(self) -> dict[str, int]:
        """Run one sweep on a connection of its own, off the event loop.

        The pass used to borrow `connection_manager.get("memory.db")` — the one connection
        every tool handler shares. Two measured consequences, both fixed by not borrowing:

        * it could not own a transaction boundary. A `commit()` there publishes a handler's
          uncommitted row; a `rollback()` there discards it (a borrowed row was published
          and another vanished, measured on a throwaway base);
        * while its `UPDATE` waited out `busy_timeout`, aiosqlite held its single worker, so
          a handler's `SELECT count(*)` on the same connection waited 11.75 s behind it.

        Now the sweep opens a short-lived `sqlite3` connection and runs in
        `asyncio.to_thread`, exactly the shape #127 gave the WAL checkpoint: it inherits
        nobody's transaction, `busy_timeout` applies to it alone, and a commit or rollback
        reaches nothing but its own work.

        Per-user commit is safe here and is what #129 was reaching for: on this connection
        one collision no longer costs the rows already rescored.
        """
        db_path = connection_manager.base_dir / "memory.db"
        return await asyncio.to_thread(self._rescore_sync, db_path)

    def _rescore_sync(self, db_path: Any) -> dict[str, int]:
        """Run the sweep itself, synchronously, through `to_thread`."""
        stats = {"rescored": 0, "skipped": 0, "errors": 0}

        conn = self._open_sweep_connection(db_path)
        try:
            # Materialised, not iterated: a cursor left mid-`fetchmany` keeps a read
            # statement in flight, and SQLite refuses to upgrade that snapshot to a write
            # once another connection has committed — BUSY in well under a millisecond,
            # without consulting `busy_timeout`. That was this loop's shape for four days.
            users = [
                r["user_id"]
                for r in conn.execute(
                    "SELECT DISTINCT user_id FROM core_memory WHERE updated_at > ?",
                    (time.time() - self.cfg.only_recent_days * 86400,),
                ).fetchall()
            ]

            for start in range(0, len(users), self.cfg.user_batch_size):
                for uid in users[start : start + self.cfg.user_batch_size]:
                    conn = self._rescore_one_user(uid, conn, stats, db_path)
        finally:
            conn.close()

        # Logged, because it was not: a pass that rescued twelve rows and a pass that
        # failed on every user looked identical from the outside, and the failure's only
        # trace was a traceback per pass.
        logger.info(
            "importance sweep: rescored=%d skipped=%d errors=%d users=%d",
            stats["rescored"],
            stats["skipped"],
            stats["errors"],
            len(users),
        )
        return stats

    def _open_sweep_connection(self, db_path: Any) -> Any:
        """Open the sweep's own connection to the base.

        `busy_timeout` is this connection's alone — no handler queues behind it, so the
        wait it imposes is the wait of the sweep and of nobody else.
        """
        conn = sqlite3.connect(str(db_path), timeout=_SWEEP_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(_SWEEP_TIMEOUT_S * 1000)}")
        return conn

    def _rescore_one_user(self, user_id: str, conn: Any, stats: dict[str, int], db_path: Any) -> Any:
        """Rescore one user, reconnecting and retrying when the write lock is lost.

        The sweep writes on a connection of its own (#132) and therefore sits outside the
        process's single-writer discipline: any other writer on the same base — another
        process, or a raw connection inside this one — can hold the write lock, and the
        sweep's write then fails with `OperationalError: database is locked`. Measured on
        the hermes base 21.10.2026: the whole pass was lost that way, twice in an hour
        and 14 times over two days before, with one user in the base and half an hour
        between passes.

        Two error classes are worth another attempt, and they mean different things:

        * 5 (`SQLITE_BUSY`) — another connection holds the write lock. The wait was the
          busy timeout, so the lock was held for at least that long.
        * 517 (`SQLITE_BUSY_SNAPSHOT`) — this connection's read snapshot went stale.
          It is instant and never consults the busy timeout.

        Both are answered the same way: close the connection, open a fresh one, and try
        the same user again. A fresh connection holds no stale snapshot and re-enters
        the busy wait from scratch. Bounded at two retries, so a writer that never
        releases the lock cannot hold the pass open forever — the failure is then
        counted once and logged with its SQLite error code and the attempt count.

        Returns the connection the next user must run on: a retry replaces it.
        """
        attempts = 0
        while True:
            attempts += 1
            # The counters describe committed work. An attempt that is rolled back
            # contributes nothing, and the retry below would count its rows twice.
            before = dict(stats)
            try:
                self._rescore_user(user_id, conn, stats)
                conn.commit()
                return conn
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                name = getattr(exc, "sqlite_errorname", None) or "SQLITE_UNKNOWN"
                if code not in (_SQLITE_BUSY, _SQLITE_BUSY_SNAPSHOT):
                    logger.exception("rescore for user=%s failed: sqlite error %s (%s)", user_id, code, name)
                    self._restore_counters(stats, before)
                    stats["errors"] += 1
                    self._rollback(conn)
                    return conn
                if attempts > _MAX_USER_RETRIES:
                    logger.exception(
                        "rescore for user=%s failed after %d attempts: sqlite error %s (%s)",
                        user_id,
                        attempts,
                        code,
                        name,
                    )
                    self._restore_counters(stats, before)
                    stats["errors"] += 1
                    self._rollback(conn)
                    return conn
                logger.warning(
                    "rescore for user=%s hit sqlite error %s (%s); reconnecting and retrying (attempt %d of %d)",
                    user_id,
                    code,
                    name,
                    attempts,
                    _MAX_USER_RETRIES + 1,
                )
                self._restore_counters(stats, before)
                conn = self._reconnect(conn, db_path)
                time.sleep(_RETRY_BACKOFF_S)
            except Exception:
                logger.exception("rescore for user=%s failed", user_id)
                self._restore_counters(stats, before)
                stats["errors"] += 1
                self._rollback(conn)
                return conn

    @staticmethod
    def _restore_counters(stats: dict[str, int], before: dict[str, int]) -> None:
        """Undo the counts of an attempt whose transaction was rolled back."""
        stats.clear()
        stats.update(before)

    def _reconnect(self, conn: Any, db_path: Any) -> Any:
        """Close the current connection and open a fresh one on the same base.

        Fresh is the point: a new connection carries no read snapshot, so it cannot
        fail with 517, and it enters the busy wait from scratch against the writer
        that is holding the lock.
        """
        self._rollback(conn)
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        return self._open_sweep_connection(db_path)

    @staticmethod
    def _rollback(conn: Any) -> None:
        """Undo the failed attempt's transaction, if the connection still has one."""
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()

    def _rescore_user(self, user_id: str, conn: Any, stats: dict[str, int]) -> None:
        # Retrieval counts batched once per user instead of one COUNT query
        # per row (the audit_log table is scanned for every fact otherwise).
        rc_map: dict[int, int] = {
            int(r["target_id"]): int(r["c"])
            for r in conn.execute(
                """SELECT target_id, COUNT(*) c FROM audit_log
                   WHERE action='recall_useful' AND layer='core_memory'
                     AND target_id IN (
                         SELECT entry_id FROM core_memory
                         WHERE user_id=? AND updated_at > ?
                     )
                   GROUP BY target_id""",
                (user_id, time.time() - self.cfg.only_recent_days * 86400),
            ).fetchall()
        }
        rows = conn.execute(
            """SELECT entry_id, "key", value, importance, memory_kind
               FROM core_memory
               WHERE user_id=? AND updated_at > ?""",
            (user_id, time.time() - self.cfg.only_recent_days * 86400),
        ).fetchall()
        sources: dict[int, str] = {}
        cols = [r[1] for r in conn.execute("PRAGMA table_info(core_memory)").fetchall()]
        if "source" in cols:
            for srow in conn.execute(
                "SELECT entry_id, source FROM core_memory WHERE user_id=?",
                (user_id,),
            ).fetchall():
                sources[int(srow["entry_id"])] = str(srow["source"] or "")

        for r in rows:
            if sources.get(int(r["entry_id"]), "") in self.CURATED_SOURCES:
                stats["skipped"] += 1
                continue
            rc = rc_map.get(int(r["entry_id"]), 0)
            signals = self.scorer.score(
                text=r["value"] or "",
                kind=r["memory_kind"] or "fact",
                retrieval_count=rc,
            )
            new_score = signals.total()
            old_score = float(r["importance"])
            if abs(new_score - old_score) < self.cfg.delta_threshold:
                stats["skipped"] += 1
                continue
            now = time.time()
            conn.execute(
                "UPDATE core_memory SET importance=?, updated_at=? WHERE entry_id=?",
                (new_score, now, int(r["entry_id"])),
            )
            conn.execute(
                """INSERT INTO importance_audit
                   (user_id, chunk_id, source, old_importance, new_importance,
                    signal_breakdown, reason, rescored_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    int(r["entry_id"]),
                    "core_memory",
                    old_score,
                    new_score,
                    json.dumps(
                        {
                            "base": signals.base,
                            "length": signals.length,
                            "tech": signals.tech_keyword,
                            "novelty": signals.novelty,
                            "retrieval_signal": signals.retrieval_signal,
                        }
                    ),
                    "scheduled",
                    now,
                ),
            )
            stats["rescored"] += 1


# Singleton
importance_scheduler = ImportanceScheduler()
