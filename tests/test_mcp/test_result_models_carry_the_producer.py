"""Result models must carry every key their producer returns.

Each test names the mutation it kills: deleting a field from the model, or
dropping `extra="forbid"` from the shared base.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ValidationError

import mcp_server.models as models
from mcp_server.tools.ops import memory_backup, memory_data

STATUS_KEYS = {
    "running",
    "interval_hours",
    "jitter_seconds",
    "retention_days",
    "keep_count",
    "wiki_sync_interval_minutes",
    "last_backup",
    "next_backup",
    "nightly_failures",
    "next_nightly_retry",
    "backup_count",
}


@pytest.fixture
def cron(tmp_path, monkeypatch):
    """A real BackupCron on a scratch base, bound where memory_backup looks."""
    from features.backup_cron import BackupCron

    instance = BackupCron(base_dir=str(tmp_path))
    monkeypatch.setattr("features.backup_cron.backup_cron", instance)
    return instance


@pytest.fixture
def fake_app(monkeypatch):
    fake_app = MagicMock()
    fake_app.import_export.import_user = AsyncMock(return_value={"core_memory": 1, "episodes": 2, "sessions": 3, "l1": 4})
    monkeypatch.setattr("mcp_server.tools.ops._get_ctx", lambda c: fake_app)
    monkeypatch.setattr("mcp_server.tools.ops._invalidate_cache", lambda layer, uid: None)
    return fake_app


async def test_backup_status_returns_every_producer_key(cron):
    """Kills: a BackupStatusResult missing any of the eleven status() keys.

    Before the fix the shared BackupResult kept two of eleven keys, so nine
    values the producer computed were silently dropped.
    """
    produced = cron.status()
    result = await memory_backup(action="status")

    assert set(produced) == STATUS_KEYS
    for key, value in produced.items():
        assert key in result, f"status key {key!r} was silently dropped"
        assert result[key] == value, f"status key {key!r} lost its value"
        assert result[key] is not None, f"status key {key!r} arrived as None"


async def test_backup_restore_returns_the_restored_files(cron, tmp_path):
    """Kills: a BackupRestoreResult without `restored`/`backup`.

    Before the fix both were None, so a successful restore named neither the
    files it wrote nor the backup it read.
    """
    backup = tmp_path / "backups" / "nightly-1"
    backup.mkdir(parents=True)
    (backup / "manifest.json").write_text('{"files": ["ariel.db"]}', encoding="utf-8")
    (backup / "ariel.db").write_text("payload", encoding="utf-8")

    result = await memory_backup(action="restore", backup_name="nightly-1")

    assert result["restored"] == ["ariel.db"]
    assert result["backup"] == "nightly-1"
    assert result["error"] is None


async def test_backup_restore_refusal_is_visible(cron):
    """Kills: a BackupRestoreResult without `error`.

    This is the test that matters most: before the fix `{"error": ...}` was
    dropped, so a refusal was byte-identical to a success.
    """
    result = await memory_backup(action="restore", backup_name="does-not-exist")

    assert result["error"] is not None
    assert "does-not-exist" in result["error"]
    assert result["restored"] is None
    assert result["backup"] is None


async def test_data_import_reports_sessions_and_l1(fake_app):
    """Kills: a DataResult without `sessions`/`l1`.

    import_user() counts four things; the model kept two, so the caller could
    not tell how many sessions and L1 entries the import restored.
    """
    result = await memory_data(action="import", user_id="u1", file_path="export.json")

    assert result["core_memory"] == 1
    assert result["episodes"] == 2
    assert result["sessions"] == 3
    assert result["l1"] == 4


def test_a_result_model_rejects_an_undeclared_key():
    """Kills: dropping extra="forbid" from the shared base.

    With the pydantic default (`ignore`) this constructor silently succeeds and
    the caller never learns the key was thrown away.
    """
    from mcp_server.models import BackupRestoreResult

    with pytest.raises(ValidationError):
        BackupRestoreResult(not_a_field=1)


def test_every_result_model_forbids_extras():
    """Kills: the next model added to this file inheriting plain BaseModel.

    Iterates every model the module exports, so a new one that inherits plain
    BaseModel fails here instead of in production. No count is asserted: the
    loop already catches a model that forgot the base, and a hardcoded number
    would break on a legitimate addition without telling anyone why.
    """
    exported = [
        obj
        for name, obj in inspect.getmembers(models, inspect.isclass)
        if obj.__module__ == models.__name__ and issubclass(obj, BaseModel) and obj is not BaseModel and not name.startswith("_")
    ]

    assert exported, "no result models found — the introspection filter is broken"
    for model in exported:
        assert model.model_config.get("extra") == "forbid", f"{model.__name__} does not forbid extras"
