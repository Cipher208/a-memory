"""Tests for shared/saga_crypto.py — full coverage."""

import json
import warnings
from pathlib import Path

import pytest

from shared.saga import read_state, read_state_legacy_or_encrypted, write_state_atomic


def test_write_state_atomic_creates_encrypted_file(tmp_path):
    """write_state_atomic should create an encrypted file."""
    path = tmp_path / "test.json"
    state = {"key": "value", "nested": {"a": 1}}
    write_state_atomic(path, state)
    assert path.exists()
    data = path.read_bytes()
    assert len(data) > 0
    # Encrypted, not plain JSON. Deliberately NOT asserted on the first byte:
    # the blob opens with the first byte of a random 24-byte nonce, which is `{`
    # in 1 write out of 256, so that assertion failed about once per 256 full
    # runs (measured: 14 of 4096 encrypt_json calls begin with `{`).
    with pytest.raises((UnicodeDecodeError, json.JSONDecodeError)):
        json.loads(data.decode("utf-8"))
    assert read_state(path) == state


def test_write_state_atomic_creates_parent_dirs(tmp_path):
    """write_state_atomic should create parent directories."""
    path = tmp_path / "deep" / "nested" / "dir" / "test.json"
    write_state_atomic(path, {"key": "value"})
    assert path.exists()


def test_write_state_atomic_replaces_existing(tmp_path):
    """write_state_atomic should replace existing file."""
    path = tmp_path / "test.json"
    write_state_atomic(path, {"old": True})
    write_state_atomic(path, {"new": True})
    assert path.exists()
    # Should be re-encrypted
    data = path.read_bytes()
    assert len(data) > 0


def test_read_state_reads_encrypted(tmp_path):
    """read_state should read encrypted file."""
    path = tmp_path / "test.json"
    write_state_atomic(path, {"key": "value"})
    loaded = read_state(path)
    assert loaded == {"key": "value"}


def test_read_state_raises_for_missing():
    """read_state should raise FileNotFoundError for missing file."""
    with pytest.raises(FileNotFoundError):
        read_state(Path("/nonexistent/path.json"))


def test_read_state_legacy_rotates_to_encrypted(tmp_path):
    """read_state_legacy_or_encrypted should rotate legacy JSON to encrypted."""
    path = tmp_path / "legacy.json"
    # Write plain JSON (not encrypted)
    path.write_text(json.dumps({"legacy": True}), encoding="utf-8")

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        loaded = read_state_legacy_or_encrypted(path)
        assert loaded == {"legacy": True}
        # Should have warned about rotation
        assert len(w) == 1
        assert "rotating" in str(w[0].message).lower()

    # File should now be encrypted. Asserted on behaviour, not on the first
    # byte: a second read must not treat it as plain JSON again (no rotation
    # warning) and must decrypt. The old `not data.startswith(b"{")` was the
    # same 1-in-256 flake as above.
    with warnings.catch_warnings(record=True) as w_after:
        warnings.simplefilter("always")
        assert read_state_legacy_or_encrypted(path) == {"legacy": True}
        assert len(w_after) == 0, "a second read must not classify the file as plain JSON"


def test_read_state_legacy_reads_encrypted(tmp_path):
    """read_state_legacy_or_encrypted should read already-encrypted file."""
    path = tmp_path / "encrypted.json"
    write_state_atomic(path, {"encrypted": True})

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        loaded = read_state_legacy_or_encrypted(path)
        assert loaded == {"encrypted": True}
        # No warning for encrypted files
        assert len(w) == 0


def test_read_state_legacy_raises_for_missing():
    """read_state_legacy_or_encrypted should raise FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        read_state_legacy_or_encrypted(Path("/nonexistent.json"))


def test_write_read_roundtrip(tmp_path):
    """Write then read should return same data."""
    path = tmp_path / "roundtrip.json"
    original = {"users": ["alice", "bob"], "count": 42, "nested": {"deep": True}}
    write_state_atomic(path, original)
    loaded = read_state(path)
    assert loaded == original


def test_write_state_atomic_chmod_error(tmp_path):
    """write_state_atomic should handle chmod errors gracefully."""
    path = tmp_path / "test.json"
    # Should not raise even if chmod fails
    write_state_atomic(path, {"key": "value"})
    assert path.exists()


def test_read_state_legacy_rotate_false_does_not_write(tmp_path):
    """`rotate=False` — читаем plain JSON и НЕ переписываем файл.

    06.10: ротация перешифровывает файл ключом ЧИТАТЕЛЯ. Для `_load_state`
    в backup_cron это означало, что импорт модуля сторонним процессом с
    другим ключом молча менял владельца файла: сервер после этого не мог
    прочитать собственное состояние, `_last_backup` становился 0 и срабатывал
    лишний бэкап. Замерено на живой базе: импорт `features.backup_cron`
    превратил файл из 72 байт открытого JSON в 112 байт шифротекста.
    """
    path = tmp_path / "state.json"
    payload = {"last_backup": 1791218696.8360813, "last_wiki_sync": 0.0}
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        loaded = read_state_legacy_or_encrypted(path, rotate=False)
        assert loaded == payload
        assert len(w) == 0, "без ротации предупреждать не о чем"

    assert path.read_bytes() == before, "чтение обязано быть без побочных эффектов"
    assert path.read_bytes().startswith(b"{")


def test_backup_cron_load_state_keeps_plain_state_file(tmp_path):
    """Конструктор BackupCron читает состояние, но не переписывает его.

    Он выполняется на импорте модуля (синглтон `backup_cron`), то есть любой
    процесс, импортировавший features.backup_cron, трогал бы файл живой базы.
    """
    import json as _json

    from features.backup_cron import BackupCron

    state = tmp_path / ".backup_cron_state.json"
    payload = {"last_backup": 1791218696.8360813, "last_wiki_sync": 1791282176.149434}
    state.write_text(_json.dumps(payload), encoding="utf-8")
    before = state.read_bytes()

    cron = BackupCron(base_dir=str(tmp_path))

    assert cron._last_backup == payload["last_backup"]
    assert cron._last_wiki_sync == payload["last_wiki_sync"]
    assert state.read_bytes() == before, "импорт/конструктор не должен трогать файл состояния"
