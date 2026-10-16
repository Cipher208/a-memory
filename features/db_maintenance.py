"""DB maintenance: size monitoring with thresholds + automatic VACUUM.

Runs inside the hourly housekeeping sweep. For every *.db in the data dir:
- report size (WAL included) via prometheus gauges
- WARN/ERROR log lines above configured thresholds
- auto-VACUUM when the file has both real size and a high free-page ratio
  (freelist), which is the only case where vacuum actually reclaims space.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from typing import Any
from dataclasses import dataclass
from pathlib import Path

from shared.connection import connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

# How the WAL truncation behaves when it loses a race.
#
# Read snapshots here are brief, so a couple of retries win where one attempt does
# not; the delay is short enough not to matter to an hourly pass.
_CHECKPOINT_ATTEMPTS = 4
_CHECKPOINT_RETRY_S = 0.5

# Below this the journal is simply small and normal. Warning about it every hour
# would rebuild the "always-on alarm" that was just removed from `db_warn_mb`.
_CHECKPOINT_WARN_MB = 5.0

# How long the dedicated checkpoint connection waits for a lock. Short: this runs on a
# maintenance path, and a reader holding the WAL should be reported (busy=1) rather
# than waited out — the retry loop is what handles a brief reader.
_CHECKPOINT_TIMEOUT_S = 5.0


@dataclass
class DbReport:
    name: str
    size_mb: float
    freelist_ratio: float
    vacuumed_mb: float
    level: str  # "ok" | "warn" | "alert"


def _mb(n_bytes: int) -> float:
    return round(n_bytes / (1024 * 1024), 2)


async def _vacuum_if_fragmented(manager: Any, db_path: Path, min_mb: float, ratio_threshold: float) -> tuple[float, bool]:
    """VACUUM when the DB is big enough and fragmented. Returns freed MB."""
    size_before = db_path.stat().st_size + sum(  # noqa: ASYNC240 — stat only
        p.stat().st_size for p in [db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")] if p.exists()
    )
    conn = await manager.get(db_path.name)

    async def _pragma(sql: str) -> int:
        # Exhaust the cursor: aiosqlite keeps unfinished statements alive and
        # VACUUM refuses to run while any statement is in progress.
        rows = await (await conn.execute(sql)).fetchall()
        return int(rows[0][0])

    page_count = await _pragma("PRAGMA page_count")
    freelist = await _pragma("PRAGMA freelist_count")

    if page_count == 0:
        return 0.0, False
    ratio = freelist / page_count
    if size_before / (1024 * 1024) < min_mb or ratio < ratio_threshold:
        return 0.0, False

    await (await conn.execute("PRAGMA busy_timeout=15000")).fetchall()
    await (await conn.execute("VACUUM")).fetchall()
    await (await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")).fetchall()
    size_after = db_path.stat().st_size + (  # noqa: ASYNC240 — stat only
        db_path.with_name(db_path.name + "-wal").stat().st_size if db_path.with_name(db_path.name + "-wal").exists() else 0
    )
    return max(0.0, _mb(size_before - size_after)), True


def _run_checkpoint(db_path: Path) -> tuple[int, int, int]:
    """Run TRUNCATE on a SHORT-LIVED DEDICATED connection. Returns (busy, log, done).

    Its own connection, not the shared one — and that is the whole point.

    The shared connection is used by every MCP tool handler, and every `await` is a
    yield point. A handler between its `execute()` and its `fetch()` has a statement
    actively stepping, and a checkpoint landing in that gap, on the same connection,
    fails with this:

        sqlite3.OperationalError: database table is locked

    That is not `database is locked` (another writer) — it is a same-connection error,
    and it is why the live checkpoint failed 7 times out of 29 while reporting nothing
    usable. Measured, same base, statement in flight:

        same connection      -> OperationalError: database table is locked
        separate connection  -> ok, busy=1 log=59 done=59

    On its own connection the answer is `busy=1`: SQLite's normal "a reader holds the
    WAL, ask again later". That is precisely what the retry loop was written for, and
    it could never handle anything while every attempt raised instead.

    Synchronous by design: it is called through `asyncio.to_thread`, so the event loop
    is not blocked, and a short-lived connection has no place in the async manager.
    """
    other = sqlite3.connect(str(db_path), timeout=_CHECKPOINT_TIMEOUT_S)
    try:
        row = other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)
    finally:
        other.close()


async def checkpoint_wal(manager: Any, db_path: Path) -> float:
    """Truncate the WAL of one of this process's databases. Returns MB reclaimed.

    Separate from VACUUM on purpose. The only `wal_checkpoint(TRUNCATE)` in this
    codebase used to live inside `_vacuum_if_fragmented`, behind a
    `freelist / page_count >= 0.25` gate — and a base with no dead pages therefore
    never truncated its WAL at all. Measured on the live hermes base 15.10.2026:
    61 MB of WAL, `freelist_count = 0`, unchanged across two restarts.

    Truncating is not "moving pages to the database". A PASSIVE checkpoint does that
    and leaves the file exactly as large (probe: 806 journal pages moved, WAL file
    unchanged). Only TRUNCATE returns the space to the filesystem.

    Runs on its OWN connection (see `_run_checkpoint`), retries, and is loud — the
    first version was none of the three, and each part of that is measured rather than
    stylistic. After the hourly pass had run twice on the live base,
    `run_db_maintenance` still reported 166.7 MB (105 MB database plus the untouched
    61 MB journal) and the log said nothing at all.

    Raising is still not an option: this runs on a maintenance path where an
    exception would cost the whole pass, including the size report that follows.
    """
    wal = db_path.with_name(db_path.name + "-wal")
    if not wal.exists():
        return 0.0
    size_before = wal.stat().st_size
    if size_before == 0:
        return 0.0

    last_reason = "not attempted"
    for attempt in range(1, _CHECKPOINT_ATTEMPTS + 1):
        try:
            busy, _log, _done = await asyncio.to_thread(_run_checkpoint, db_path)
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        else:
            # `busy` means SQLite declined, usually because a reader holds the WAL.
            # The file size afterwards is what actually says whether space came back.
            size_now = wal.stat().st_size if wal.exists() else 0
            if not busy and size_now < size_before:
                return max(0.0, _mb(size_before - size_now))
            last_reason = f"busy={busy}" if busy else "no shrink"

        if attempt < _CHECKPOINT_ATTEMPTS:
            await asyncio.sleep(_CHECKPOINT_RETRY_S)

    size_now = wal.stat().st_size if wal.exists() else 0
    if _mb(size_before) >= _CHECKPOINT_WARN_MB:
        logger.warning(
            "WAL checkpoint for %s reclaimed nothing after %d attempts (%s); still %.1f MB",
            db_path.name,
            _CHECKPOINT_ATTEMPTS,
            last_reason,
            _mb(size_now),
        )
    return 0.0


async def run_db_maintenance(
    cm: Any | None = None,
    warn_mb: float | None = None,
    alert_mb: float | None = None,
    vacuum_min_mb: float | None = None,
    vacuum_freelist_ratio: float | None = None,
) -> list[DbReport]:
    """Args override yaml; yaml is the default source."""
    from config import config

    warn_mb = float(config.get("storage", "db_warn_mb", default=50)) if warn_mb is None else float(warn_mb)
    alert_mb = float(config.get("storage", "db_alert_mb", default=200)) if alert_mb is None else float(alert_mb)
    vacuum_min_mb = float(config.get("storage", "vacuum_min_mb", default=10)) if vacuum_min_mb is None else float(vacuum_min_mb)
    vacuum_freelist_ratio = (
        float(config.get("storage", "vacuum_freelist_ratio", default=0.25)) if vacuum_freelist_ratio is None else float(vacuum_freelist_ratio)
    )

    manager = cm or connection_manager
    data_dir = Path(manager.base_dir)
    reports: list[DbReport] = []
    logger.debug("DB maintenance scanning %s", data_dir)

    try:
        import prometheus_client

        size_gauge = prometheus_client.Gauge("ariel_db_size_bytes", "SQLite DB size incl. WAL", ["db"])
    except Exception:
        size_gauge = None

    for db_file in sorted(data_dir.glob("*.db")):  # noqa: ASYNC240 — listing only
        wal = db_file.with_name(db_file.name + "-wal")
        size = db_file.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
        size_mb = _mb(size)

        freed_mb, did_vacuum = 0.0, False
        # Only maintenance-scope DBs are safe to touch here; memory.db belongs to
        # this process, projects.db likewise. Skip foreign files defensively.
        if db_file.name in {DB_NAME, "projects.db"}:
            if size_mb >= vacuum_min_mb:
                try:
                    freed_mb, did_vacuum = await _vacuum_if_fragmented(manager, db_file, vacuum_min_mb, vacuum_freelist_ratio)
                except Exception:
                    logger.exception("VACUUM failed for %s", db_file.name)

            # Truncate the WAL on its own terms, NOT behind the VACUUM gate above.
            # That gate only opens on fragmentation, so a base with no dead pages
            # never truncated — measured 15.10.2026 with 61 MB of WAL and
            # `freelist_count = 0`, unchanged across two restarts. Truncating is the
            # only thing that returns those pages to the filesystem; a PASSIVE
            # checkpoint moves them into the DB and leaves the file size alone.
            try:
                wal_mb = await checkpoint_wal(manager, db_file)
                if wal_mb:
                    logger.info("WAL checkpoint: %s reclaimed %.1f MB", db_file.name, wal_mb)
            except Exception:
                logger.exception("WAL checkpoint failed for %s", db_file.name)

            size_mb = _mb(db_file.stat().st_size + (wal.stat().st_size if wal.exists() else 0))

        if size_gauge is not None:
            with contextlib.suppress(Exception):
                size_gauge.labels(db=db_file.name).set(size_mb * 1024 * 1024)

        if size_mb >= alert_mb:
            level = "alert"
            logger.error("DB %s is %.1f MB (>= %.0f MB alert threshold)", db_file.name, size_mb, alert_mb)
        elif size_mb >= warn_mb:
            level = "warn"
            logger.warning("DB %s is %.1f MB (>= %.0f MB warn threshold)", db_file.name, size_mb, warn_mb)
        else:
            level = "ok"

        reports.append(DbReport(db_file.name, size_mb, 0.0, freed_mb if did_vacuum else 0.0, level))

    return reports
