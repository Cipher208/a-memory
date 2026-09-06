"""backup_cron._cleanup_tmp — /tmp test-artifact hygiene (2026-09-06 incident).

Полный /tmp на tmpfs встал (pytest-of-murat 2.3G + ariel-test-global-*) и
повесил локальный pre-push pytest-гейт. Чистка теперь часть ежедневного
_backup-прохода: строгие префиксы, порог 2 дня, best-effort.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from features.backup_cron import BackupCron

if TYPE_CHECKING:
    from pathlib import Path


def _mk(base: Path, name: str, age_days: float) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "probe.txt").write_text("x", encoding="utf-8")
    old = time.time() - age_days * 86400
    os.utime(d, (old, old))
    return d


def test_cleanup_tmp_removes_only_stale_known_prefixes(tmp_path: Path) -> None:
    import getpass

    cron = BackupCron(base_dir=str(tmp_path))
    pytest_base = tmp_path / f"pytest-of-{getpass.getuser()}"
    stale_pytest = _mk(pytest_base, "pytest-42", 3)
    fresh_pytest = _mk(pytest_base, "pytest-43", 0.1)  # живая сессия — не трогаем
    stale_ariel = _mk(tmp_path, "ariel-test-global-aaa", 3)
    fresh_ariel = _mk(tmp_path, "ariel-eval-bbb", 0.5)
    outsider = _mk(tmp_path, "pytest-of-nobody", 30)  # чужой user — не наш префикс
    keeper = _mk(tmp_path, "unrelated-data", 30)  # не матчится префиксами

    removed = cron._cleanup_tmp(tmp_root=tmp_path)

    assert removed == 2
    assert not stale_pytest.exists() and not stale_ariel.exists()
    assert fresh_pytest.exists() and fresh_ariel.exists()
    assert outsider.exists() and keeper.exists()


def test_cleanup_tmp_missing_roots_noop(tmp_path: Path) -> None:
    cron = BackupCron(base_dir=str(tmp_path))
    assert cron._cleanup_tmp(tmp_root=tmp_path) == 0


def test_cleanup_old_calls_tmp_cleanup(tmp_path: Path, monkeypatch) -> None:
    """Wire-check: ежедневный проход backup'а тянет tmp-чистку за собой."""
    cron = BackupCron(base_dir=str(tmp_path))
    called = []
    monkeypatch.setattr(cron, "_cleanup_tmp", lambda: called.append(True))
    cron._cleanup_old()
    assert called == [True]
