"""Verify that saga._load_state and backup_cron._load_state use saga_crypto functions."""

import inspect

from features import backup_cron
from shared import saga


def test_saga_load_state_uses_read_state():
    """saga._load_state should call read_state or read_state_legacy_or_encrypted."""
    source = inspect.getsource(saga.Saga._load_state)
    assert "read_state" in source, "saga._load_state doesn't use saga_crypto.read_state"


def test_backup_cron_load_state_uses_read_state():
    """backup_cron._load_state should call read_state or read_state_legacy_or_encrypted."""
    source = inspect.getsource(backup_cron.BackupCron._load_state)
    assert "read_state" in source, "backup_cron._load_state doesn't use saga_crypto.read_state"
