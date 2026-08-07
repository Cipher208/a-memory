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
import json
import logging
import threading
import time
from dataclasses import dataclass

from shared.connection import connection_manager
from shared.importance import ImportanceScorer

logger = logging.getLogger(__name__)


@dataclass
class SchedulerConfig:
    interval_seconds: int = 1800
    user_batch_size: int = 50
    delta_threshold: float = 0.15
    only_recent_days: int = 30
    enabled: bool = True


class ImportanceScheduler:
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
            except Exception as exc:
                logger.exception("Scheduler iteration failed: %s", exc)
            self._stop_event.wait(timeout=self.cfg.interval_seconds)

    async def run_once(self) -> dict[str, int]:
        """Single async iteration. Returns counters."""
        stats = {"rescored": 0, "skipped": 0, "errors": 0}

        cm = connection_manager
        conn = await cm.get("memory.db")

        # Batched user processing
        cursor = await conn.execute(
            "SELECT DISTINCT user_id FROM core_memory WHERE updated_at > ?",
            (time.time() - self.cfg.only_recent_days * 86400,),
        )
        while True:
            batch = await cursor.fetchmany(self.cfg.user_batch_size)
            if not batch:
                break
            for u in batch:
                uid = u["user_id"]
                try:
                    await self._rescore_user(uid, conn, stats)
                except Exception as exc:
                    logger.error("rescore for user=%s failed: %s", uid, exc)
                    stats["errors"] += 1

        await conn.commit()
        return stats

    async def _rescore_user(self, user_id: str, conn, stats: dict) -> None:
        rows = await (
            await conn.execute(
                """SELECT entry_id, "key", value, importance, memory_kind
                   FROM core_memory
                   WHERE user_id=? AND updated_at > ?""",
                (user_id, time.time() - self.cfg.only_recent_days * 86400),
            )
        ).fetchall()

        for r in rows:
            rc = await self._lookup_retrieval_count(conn, "core_memory", int(r["entry_id"]))
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
            await conn.execute(
                "UPDATE core_memory SET importance=?, updated_at=? WHERE entry_id=?",
                (new_score, now, int(r["entry_id"])),
            )
            await conn.execute(
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

    @staticmethod
    async def _lookup_retrieval_count(conn, source: str, source_id: int) -> int:
        row = await (
            await conn.execute(
                """SELECT COUNT(*) c FROM audit_log
                   WHERE action='recall_useful' AND layer=? AND target_id=?""",
                (source, str(source_id)),
            )
        ).fetchone()
        return int(row["c"]) if row else 0


# Singleton
importance_scheduler = ImportanceScheduler()
