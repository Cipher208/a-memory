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

# A write that loses the race is retried. `busy_timeout` is not enough on its own:
# when a read snapshot has to be upgraded and another connection has committed since,
# SQLite answers BUSY *without* consulting the handler, so the only remedy inside that
# window is to try again. Measured on the live base 16.10.2026 — the sweep failed on
# `UPDATE core_memory` in ~0.1 ms with a 15 s timeout configured.
_BUSY_RETRIES = 3
_BUSY_RETRY_SLEEP_S = 0.25


def _is_lock_error(exc: Exception) -> bool:
    """Report whether an exception is one of the two lock errors.

    `database is locked` is another writer on the file; `database table is locked` is a
    statement still in flight on this very connection. Both are worth retrying, and
    conflating them is how a same-connection defect was mistaken for a writer storm
    once already (see the WAL checkpoint fix).
    """
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


async def _execute_with_retry(conn: Any, sql: str, params: tuple[Any, ...]) -> None:
    """One write, retried while a competing writer holds the file."""
    for attempt in range(_BUSY_RETRIES + 1):
        try:
            await conn.execute(sql, params)
            return
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc) or attempt == _BUSY_RETRIES:
                raise
            await asyncio.sleep(_BUSY_RETRY_SLEEP_S * (attempt + 1))


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
        """Run one sweep. Callers hold the maintenance lock."""
        stats = {"rescored": 0, "skipped": 0, "errors": 0}

        cm = connection_manager
        conn = await cm.get("memory.db")

        # The user list is materialised, not iterated. A cursor left mid-`fetchmany`
        # keeps a read statement in flight for the whole pass, and SQLite cannot
        # upgrade that read snapshot to a write once another connection has committed
        # — it answers BUSY immediately and does NOT consult `busy_timeout`. Measured
        # on the live base: the sweep failed on `UPDATE core_memory`
        # (`OperationalError: database is locked`) on nearly every pass for four days,
        # every 30 minutes, while a second process served the same base — and that is
        # the shape this loop had. One extra small query removes the window.
        users = [
            r["user_id"]
            for r in await (
                await conn.execute(
                    "SELECT DISTINCT user_id FROM core_memory WHERE updated_at > ?",
                    (time.time() - self.cfg.only_recent_days * 86400,),
                )
            ).fetchall()
        ]

        # Still processed in batches, but the list is already in memory: batching now
        # only decides how often we commit, not how long a statement stays open.
        for start in range(0, len(users), self.cfg.user_batch_size):
            for uid in users[start : start + self.cfg.user_batch_size]:
                try:
                    await self._rescore_user(uid, conn, stats)
                    # Committed per user. One commit for the whole pass means a single
                    # collision discards every row the pass had already rescored, and
                    # holds a write transaction across hundreds of statements, which is
                    # what makes the collision likely in the first place.
                    await conn.commit()
                except Exception:
                    logger.exception("rescore for user=%s failed", uid)
                    stats["errors"] += 1
                    with contextlib.suppress(Exception):
                        await conn.rollback()

        # Logged, because it was not: a pass that rescored nothing and a pass that
        # failed on every user looked identical from the outside, and the only trace
        # of the failure was a traceback per pass.
        logger.info(
            "importance sweep: rescored=%d skipped=%d errors=%d users=%d",
            stats["rescored"],
            stats["skipped"],
            stats["errors"],
            len(users),
        )
        return stats

    async def _rescore_user(self, user_id: str, conn: Any, stats: dict[str, int]) -> None:
        # Retrieval counts batched once per user instead of one COUNT query
        # per row (the audit_log table is scanned for every fact otherwise).
        rc_map: dict[int, int] = {
            int(r["target_id"]): int(r["c"])
            for r in await (
                await conn.execute(
                    """SELECT target_id, COUNT(*) c FROM audit_log
                       WHERE action='recall_useful' AND layer='core_memory'
                         AND target_id IN (
                             SELECT entry_id FROM core_memory
                             WHERE user_id=? AND updated_at > ?
                         )
                       GROUP BY target_id""",
                    (user_id, time.time() - self.cfg.only_recent_days * 86400),
                )
            ).fetchall()
        }
        rows = await (
            await conn.execute(
                """SELECT entry_id, "key", value, importance, memory_kind
                   FROM core_memory
                   WHERE user_id=? AND updated_at > ?""",
                (user_id, time.time() - self.cfg.only_recent_days * 86400),
            )
        ).fetchall()
        sources: dict[int, str] = {}
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(core_memory)")).fetchall()]
        if "source" in cols:
            for srow in await (
                await conn.execute(
                    "SELECT entry_id, source FROM core_memory WHERE user_id=?",
                    (user_id,),
                )
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
            await _execute_with_retry(
                conn,
                "UPDATE core_memory SET importance=?, updated_at=? WHERE entry_id=?",
                (new_score, now, int(r["entry_id"])),
            )
            await _execute_with_retry(
                conn,
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
