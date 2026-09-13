"""CLI exit watchdog: a hung autohooks process must die by itself.

Root cause of the 2026-09-12 orphans: two `autohooks dispatch/recall` CLI
processes finished their work but hung on shutdown (futex wait, ppid=1),
held open FDs on mimocode memory.db/-wal/-shm for a day, starved WAL
auto-checkpoint to 3.9 GB and blocked every subsequent writer. _close_ariel
already existed but only covers clean teardown — it cannot help when the
teardown itself wedges. The faulthandler watchdog is the last line: whatever
is stuck, the process dumps its stack and exits within ARIEL_CLI_GUARD
seconds.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = "import time, sys;sys.path.insert(0, {repo!r});from autohooks.__main__ import _arm_cli_guard, _cancel_cli_guard;_arm_cli_guard(0.3);{tail}"


def _repo_dir() -> str:
    return __file__.rsplit("/tests/", 1)[0]


def test_watchdog_kills_hung_process() -> None:
    tail = "print('still alive'); time.sleep(10)"
    r = subprocess.run(
        [sys.executable, "-c", _PROBE.format(repo=_repo_dir(), tail=tail)],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert r.returncode != 0
    assert "Timeout" in (r.stderr + r.stdout) or "threadid" in (r.stderr + r.stdout).lower()


def test_watchdog_cancelled_on_clean_exit() -> None:
    tail = "_cancel_cli_guard(); time.sleep(0.5); print('done')"
    r = subprocess.run(
        [sys.executable, "-c", _PROBE.format(repo=_repo_dir(), tail=tail)],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert r.returncode == 0
    assert "done" in r.stdout
