"""Tests for backup path traversal prevention."""

import asyncio
import json

import pytest

from features.backup import BackupManager


@pytest.fixture
def bm(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return BackupManager(base_dir=str(data_dir))


@pytest.mark.parametrize(
    "manifest_files,should_reject",
    [
        (["../../etc/crontab", "memory.db"], True),
        (["/etc/passwd"], True),
    ],
)
def test_restore_rejects_traversal(bm, manifest_files, should_reject):
    backup_dir = bm.backup_dir / "crafted"
    backup_dir.mkdir()
    manifest = {"files": manifest_files, "created_at": "2026-01-01T00:00:00"}
    (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes base directory"):
        asyncio.run(bm.restore("crafted"))
