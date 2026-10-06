"""tests/_tmp_policy — куда набор пишет временные файлы и как их подчищает.

Повод — 06.10.2026: пользовательская квота на tmpfs /tmp кончилась, записи
падали с EDQUOT (-122), и у песочницы DSH отвалились все инструменты, потому что
харнесс пишет туда свой вывод. Набор оставлял там ~700 МиБ за прогон, а /tmp —
это 7.9 ГиБ RAM на всю машину. Теперь временные файлы уходят на корневую ФС, а
история прогонов подрезается на старте.
"""

from __future__ import annotations

import getpass
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from tests import _tmp_policy

if TYPE_CHECKING:
    import pytest


def _runs(base: Path, ages_hours: list[float]) -> list[Path]:
    """Сделать каталоги прогонов pytest с заданным возрастом (в часах)."""
    pytest_base = base / f"pytest-of-{getpass.getuser()}"
    pytest_base.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for index, age in enumerate(ages_hours, start=1):
        d = pytest_base / f"pytest-{index}"
        d.mkdir()
        (d / "probe.txt").write_text("x", encoding="utf-8")
        stamp = time.time() - age * 3600
        os.utime(d, (stamp, stamp))
        made.append(d)
    return made


def test_default_root_is_not_the_shared_tmpfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """По умолчанию — не /tmp: это RAM-том на всю машину, а прогону нужно ~700 МиБ."""
    monkeypatch.delenv("ARIEL_TEST_TMPDIR", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)

    chosen = _tmp_policy.root()

    assert chosen != Path("/tmp")
    assert str(chosen).startswith(os.path.expanduser("~"))


def test_root_prefers_the_dedicated_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARIEL_TEST_TMPDIR сильнее TMPDIR, а явный TMPDIR уважается."""
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    monkeypatch.delenv("ARIEL_TEST_TMPDIR", raising=False)
    assert _tmp_policy.root() == Path("/var/tmp")

    monkeypatch.setenv("ARIEL_TEST_TMPDIR", "/var/tmp/dedicated")
    assert _tmp_policy.root() == Path("/var/tmp/dedicated")


def test_install_points_tempfile_at_the_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """install() переключает tempfile на заданный каталог и говорит, куда именно.

    Сброс `tempfile.tempdir` здесь не украшение: без него кэш модуля переживает
    смену TMPDIR, переменная выглядит заданной, а набор всё равно пишет в /tmp —
    ровно так и получилось в инциденте.
    """
    destination = tmp_path / "scratch"
    monkeypatch.setenv("ARIEL_TEST_TMPDIR", str(destination))
    monkeypatch.setattr(tempfile, "tempdir", None)

    installed = _tmp_policy.install()

    assert installed == destination
    assert destination.is_dir()
    assert Path(tempfile.gettempdir()) == destination


def test_prune_keeps_the_newest_runs_and_drops_the_rest(tmp_path: Path) -> None:
    # 3 и 3.6 минуты — новейшие, остаются; 18 минут и 40 часов — и за пределами
    # «последних двух», и старше отсрочки, поэтому уходят.
    made = _runs(tmp_path, [0.05, 0.06, 0.3, 40])

    removed = _tmp_policy.prune(tmp_path)

    assert removed == 2
    assert made[0].exists() and made[1].exists()
    assert not made[2].exists() and not made[3].exists()


def test_prune_never_touches_a_run_inside_the_grace(tmp_path: Path) -> None:
    """Два набора могут идти одновременно (гейт и ручной прогон).

    Каталог за пределами «последних двух», но младше отсрочки, принадлежит
    набору, который ещё работает: правило «оставить N новейших» без отсрочки
    снесло бы его на ходу. Здесь все пять младше 10 минут — не уходит никто.
    """
    made = _runs(tmp_path, [1 / 60, 2 / 60, 3 / 60, 5 / 60, 8 / 60])

    removed = _tmp_policy.prune(tmp_path)

    assert removed == 0
    assert all(d.exists() for d in made), "снесён каталог набора, который ещё идёт"


def test_prune_never_touches_the_run_pytest_points_current_at(tmp_path: Path) -> None:
    """`pytest-current` показывает прогон в работе — его не трогаем никогда.

    Возраст этого не ловит: начавшийся минуту назад прогон и закончившийся
    минуту назад выглядят одинаково. Ловит только сама ссылка.

    Живой каталог здесь — САМЫЙ СТАРЫЙ: без защиты его вытеснило бы правилом
    «оставить два новейших», поэтому тест проверяет защиту, а не совпадение.
    Возрасты идут от старого к новому (больше возраст — меньше mtime), а
    предусловие ниже не даёт тесту стать беззубым, если порядок перепутают.
    """
    made = _runs(tmp_path, [43 / 60, 42 / 60, 41 / 60, 40 / 60])
    pytest_base = tmp_path / f"pytest-of-{getpass.getuser()}"
    live, expendable = made[0], made[1]
    (pytest_base / "pytest-current").symlink_to(live)

    newest = sorted(made, key=lambda d: d.stat().st_mtime, reverse=True)[: _tmp_policy.KEEP_PYTEST_RUNS]
    assert live not in newest, "предусловие: живой каталог обязан быть ЗА пределами «последних N», иначе защита не проверяется"

    _tmp_policy.prune(tmp_path)

    assert live.exists(), "удалён каталог прогона, на который указывает pytest-current"
    assert not expendable.exists(), "тест беззубый: соседний каталог тоже выжил"


def test_prune_touches_only_its_own_prefixes(tmp_path: Path) -> None:
    ours = tmp_path / "ariel-eval-old"
    ours.mkdir()
    stamp = time.time() - 30 * 3600
    os.utime(ours, (stamp, stamp))
    stranger = tmp_path / "someone-elses-data"
    stranger.mkdir()
    os.utime(stranger, (stamp, stamp))
    other_user = tmp_path / "pytest-of-nobody"
    other_user.mkdir()
    os.utime(other_user, (stamp, stamp))

    removed = _tmp_policy.prune(tmp_path)

    assert removed == 1
    assert not ours.exists()
    assert stranger.exists() and other_user.exists()


def _bare(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir()
    stamp = time.time() - 30 * 3600
    os.utime(d, (stamp, stamp))
    return d


def test_prune_collects_bare_mkdtemp_only_in_a_directory_we_own(tmp_path: Path) -> None:
    """`tempfile.mkdtemp()` без префикса (test_hypothesis.py) оставляет пустой
    каталог на каждый вызов и сам не убирается.

    Ловим только в своём каталоге: шаблон `tmp????????` слишком общий, чтобы
    что-то удалять там, куда набор направил оператор (например /var/tmp).
    """
    matched = _bare(tmp_path, "tmp005w9pl7")  # ровно 8 символов после tmp
    too_short = _bare(tmp_path, "tmp1234")
    too_long = _bare(tmp_path, "tmp123456789")
    word = _bare(tmp_path, "tmpfiles")

    assert _tmp_policy.prune(tmp_path, owned=True) == 1
    assert not matched.exists()
    assert too_short.exists() and too_long.exists() and word.exists(), "шаблон слишком широкий"


def test_prune_leaves_bare_mkdtemp_alone_when_the_directory_is_not_ours(tmp_path: Path) -> None:
    matched = _bare(tmp_path, "tmp005w9pl7")

    assert _tmp_policy.prune(tmp_path, owned=False) == 0
    assert matched.exists()


def test_install_reports_owned_for_our_default_and_not_for_an_operator_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIEL_TEST_TMPDIR", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    seen: list[bool] = []

    def _record(dest: Path, **kw: object) -> int:
        seen.append(bool(kw.get("owned", False)))
        return 0

    monkeypatch.setattr(_tmp_policy, "prune", _record)

    assert _tmp_policy.install() == Path(os.path.expanduser("~")) / ".cache" / "ariel-test-tmp"
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert _tmp_policy.install() == tmp_path

    assert seen == [True, False], f"owned определено неверно: {seen}"
