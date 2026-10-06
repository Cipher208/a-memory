"""backup_cron._cleanup_tmp — temp-dir test-artifact hygiene (2026-09-06 incident).

Полный /tmp на tmpfs встал (pytest-of-murat 2.3G + ariel-test-global-*) и
повесил локальный pre-push pytest-гейт. 06.10.2026 тот же tmpfs упёрся в
пользовательскую квоту: записи падали с EDQUOT (-122) и у песочницы DSH
отвалились все инструменты, потому что харнесс пишет туда свой вывод.
Чистка — часть ежедневного _backup-прохода: строгие префиксы, best-effort,
порог 6 часов (был 2 дня: проход суточный, значит утечка жила двое суток).
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
    fresh_ariel = _mk(tmp_path, "ariel-eval-bbb", 1.2 / 24)  # младше окна — не трогаем
    outsider = _mk(tmp_path, "pytest-of-nobody", 30)  # чужой user — не наш префикс
    keeper = _mk(tmp_path, "unrelated-data", 30)  # не матчится префиксами

    removed = cron._cleanup_tmp(tmp_root=tmp_path)

    assert removed == 2
    assert not stale_pytest.exists() and not stale_ariel.exists()
    assert fresh_pytest.exists() and fresh_ariel.exists()
    assert outsider.exists() and keeper.exists()


def test_cleanup_tmp_window_is_hours_not_days(tmp_path: Path) -> None:
    """Порог измеряется часами: проход суточный, поэтому 2-дневное окно означало,
    что утечка обязана пережить двое суток, а всё это время копится ровно тот
    объём, который и забил tmpfs в обоих инцидентах.

    12 часов — уже мусор (при прежнем пороге он бы выжил), 5 часов — ещё нет.
    """
    cron = BackupCron(base_dir=str(tmp_path))
    twelve_hours = _mk(tmp_path, "ariel-eval-12h", 12 / 24)
    five_hours = _mk(tmp_path, "ariel-eval-5h", 5 / 24)

    removed = cron._cleanup_tmp(tmp_root=tmp_path)

    assert removed == 1
    assert not twelve_hours.exists()
    assert five_hours.exists()


def test_cleanup_tmp_never_removes_the_current_pytest_run(tmp_path: Path) -> None:
    """Каталог, на который pytest указывает `pytest-current`, не удаляется.

    Возраст этого не ловит: прогон, начавшийся минуту назад, и прогон,
    закончившийся минуту назад, выглядят одинаково свежими. Ловит только сама
    ссылка — она и есть то, что делает укороченное окно безопасным для набора,
    который гоняют прямо сейчас.
    """
    import getpass

    cron = BackupCron(base_dir=str(tmp_path))
    pytest_base = tmp_path / f"pytest-of-{getpass.getuser()}"
    live_run = _mk(pytest_base, "pytest-7", 3)  # старый по mtime, но он текущий
    dead_run = _mk(pytest_base, "pytest-6", 3)
    (pytest_base / "pytest-current").symlink_to(live_run)

    removed = cron._cleanup_tmp(tmp_root=tmp_path)

    assert removed == 1
    assert live_run.exists(), "удалён каталог текущего прогона pytest"
    assert not dead_run.exists()


def test_cleanup_tmp_defaults_to_real_tempdir(tmp_path: Path, monkeypatch) -> None:
    """Боевой вызов не передаёт tmp_root — умолчание обязано быть настоящим temp-каталогом.

    Инцидент 2026-10-01: TMPDIR указывал на /var/tmp, а чистка смотрела в /tmp —
    4.4G (pytest-of-murat 3.4G + 74 ariel-eval-*) копились две недели незамеченными.
    Прежние тесты всегда передавали tmp_root=tmp_path, поэтому хардкод не исполнялся.
    rmtree заглушен: до исправления тест не должен трогать настоящий /tmp.
    """
    import shutil
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    _mk(tmp_path, "ariel-eval-ccc", 3)

    seen: list[str] = []
    monkeypatch.setattr(shutil, "rmtree", lambda p, **kw: seen.append(str(p)))

    removed = BackupCron(base_dir=str(tmp_path))._cleanup_tmp()

    assert seen == [str(tmp_path / "ariel-eval-ccc")], f"умолчание не равно tempfile.gettempdir(); чистили бы: {seen}"
    assert removed == 1


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
