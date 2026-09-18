"""Single-writer backup_cron: один писатель на базу, остальным — стоять."""

import fcntl
import os

from features.backup_cron import BackupCron


def _due(cron: BackupCron) -> BackupCron:
    cron._last_backup = 0.0
    cron.interval_hours = 0
    cron.jitter_seconds = 0
    return cron


def test_second_holder_skips_backup(tmp_path) -> None:
    cron = _due(BackupCron(base_dir=str(tmp_path)))
    lock_path = tmp_path / ".backup_cron.lock"
    with open(lock_path, "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        cron._check_backup(9999999999.0)
    assert list((tmp_path / "backups").iterdir()) == []


def test_lock_released_after_backup(tmp_path) -> None:
    cron = _due(BackupCron(base_dir=str(tmp_path)))
    fd = cron._acquire_backup_lock()
    assert fd is not None
    assert cron._acquire_backup_lock() is None
    cron._release_backup_lock(fd)
    assert cron._acquire_backup_lock() is not None


def test_cleanup_keeps_newest_n(tmp_path) -> None:
    import time

    cron = BackupCron(base_dir=str(tmp_path))
    cron.retention_days = 365
    cron.keep_count = 5
    base = time.time()
    for i in range(8):
        d = tmp_path / "backups" / f"auto_{i}"
        d.mkdir(parents=True)
        os.utime(d, (base + i, base + i))
    cron._cleanup_old()
    left = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert left == [f"auto_{i}" for i in range(3, 8)]
