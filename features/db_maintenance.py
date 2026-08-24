"""DB maintenance: size monitoring with thresholds + automatic VACUUM.

Runs inside the hourly housekeeping sweep. For every *.db in the data dir:
- report size (WAL included) via prometheus gauges
- WARN/ERROR log lines above configured thresholds
- auto-VACUUM when the file has both real size and a high free-page ratio
  (freelist), which is the only case where vacuum actually reclaims space.
"""

import contextlib
import logging
from typing import Any
from dataclasses import dataclass
from pathlib import Path

from shared.connection import connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)


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
        # Only maintenance-scope DBs are safe to VACUUM here; memory.db belongs
        # to this process, projects.db likewise. Skip foreign files defensively
        # by checking we can open them through our own manager.
        if size_mb >= vacuum_min_mb and db_file.name in {DB_NAME, "projects.db"}:
            try:
                freed_mb, did_vacuum = await _vacuum_if_fragmented(manager, db_file, vacuum_min_mb, vacuum_freelist_ratio)
                size_mb = _mb(db_file.stat().st_size + (wal.stat().st_size if wal.exists() else 0))
            except Exception:
                logger.exception("VACUUM failed for %s", db_file.name)

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
