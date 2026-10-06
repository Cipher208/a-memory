"""Keep the test suite's temporary files off tmpfs, and keep them bounded.

`/tmp` on this host is a 7.9 GiB tmpfs — RAM, not disk — shared by the whole
machine and carrying a per-user quota. On 2026-10-06 that quota ran out: every
write began failing with EDQUOT (-122), and the DSH tool sandbox lost *all* of
its tools, because the harness writes its own command output to `/tmp` and so
could not run even the command that would have cleaned up.

A full suite run leaves roughly 700 MiB there, one memory instance per test,
which is far too much to keep on a RAM-backed volume shared with everything
else on the machine. So the suite writes its temporary files to the root
filesystem instead, and prunes its own history before each run.

Pruning is deliberately conservative. A tree is removed only when it is both
beyond the keep-count *and* older than a grace period, and never when pytest's
`pytest-current` symlink points at it: two suites can run at once (a pre-commit
gate and a hand-run one), and the keep-count alone would happily delete the older
suite's tree out from under it while it was still running.

An explicitly set `TMPDIR` is honoured — an operator may have a reason — and
`ARIEL_TEST_TMPDIR` overrides the destination in the default case. When the
destination is *ours* (we picked it, nobody pointed us at it) bare `mkdtemp`
leftovers are collected too, because `tests/test_hypothesis.py` calls
`tempfile.mkdtemp()` with no prefix and leaves an empty directory per call. That
sweep is deliberately NOT applied to an operator-chosen directory: a pattern as
generic as `tmp????????` has no business deleting things out of `/var/tmp`.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

# How many superseded run trees to keep for post-mortem debugging. One full-suite
# tree is ~725 MiB, so this constant is what bounds the scratch directory: two
# runs hold ~1.5 GiB, which is why it is 2 rather than a larger, tidier number.
KEEP_PYTEST_RUNS = 2

# `mkdtemp()` with no prefix is `tmp` plus exactly 8 characters. Pinning the
# length keeps the pattern narrow; a loose `tmp*` would match `tmpfiles`,
# `tmpwatch`, or anything else a neighbour happened to put in the same directory.
_BARE_MKDTEMP = re.compile(r"^tmp[a-z0-9_]{8}$")

# A tree is evicted by the keep-count only once it is this old: ~4.5x the longest
# full-suite run measured here (2.2 min), so a suite that started while another was
# finishing is never deleted mid-run. This works together with the `pytest-current`
# check below rather than instead of it — that check protects the run *in progress*
# exactly, this one protects a second suite that started minutes ago and whose tree
# `pytest-current` no longer points at. Measured cost of a longer grace: with 1 hour,
# four consecutive runs grew the directory to 2.2 GiB and nothing was ever evicted,
# because a burst of runs never ages past the threshold.
GRACE_MINUTES = 10.0

# Backstop for crash leftovers of the one-directory-per-session artifacts. They
# delete themselves now (the hermetic fixture removes its own, and the eval
# harness removes its own), so anything left is from a run that was killed.
ARTIFACT_MAX_AGE_HOURS = 6.0


def root() -> Path:
    """Where this suite should keep its temporary files."""
    configured = os.environ.get("ARIEL_TEST_TMPDIR")
    if configured:
        return Path(configured)
    if os.environ.get("TMPDIR"):
        return Path(os.environ["TMPDIR"])
    return Path(os.path.expanduser("~")) / ".cache" / "ariel-test-tmp"


def prune(destination: Path, *, owned: bool = False) -> int:
    """Remove superseded run trees under `destination`. Returns how many went.

    `owned` means this suite chose the directory itself, so bare `mkdtemp`
    leftovers in it are known to be ours and may be collected.
    """
    removed = 0

    try:
        pytest_base = destination / f"pytest-of-{getpass.getuser()}"
    except (KeyError, OSError):
        pytest_base = None
    if pytest_base is not None and pytest_base.is_dir() and not pytest_base.is_symlink():
        # pytest points `pytest-current` at the run in progress. An age test alone
        # cannot recognise it — a run that started a minute ago and one that
        # finished a minute ago look identical — and this is what keeps the short
        # grace safe even for a suite slower than any run we have measured.
        current = pytest_base / "pytest-current"
        live = {os.path.realpath(current)} if current.is_symlink() else set()
        runs = sorted(
            (d for d in pytest_base.glob("pytest-*") if d.is_dir() and not d.is_symlink()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        grace = time.time() - GRACE_MINUTES * 60
        for stale in runs[KEEP_PYTEST_RUNS:]:
            try:
                if os.path.realpath(stale) not in live and stale.stat().st_mtime < grace:
                    shutil.rmtree(stale, ignore_errors=True)
                    removed += 1
            except OSError:
                continue

    cutoff = time.time() - ARTIFACT_MAX_AGE_HOURS * 3600
    patterns = ["ariel-test-global-*", "ariel-eval-*"]
    if owned:
        patterns.append("tmp*")  # filtered by _BARE_MKDTEMP below, not by glob
    for pattern in patterns:
        for stale in destination.glob(pattern):
            try:
                if pattern == "tmp*" and not _BARE_MKDTEMP.match(stale.name):
                    continue
                if stale.is_dir() and not stale.is_symlink() and stale.stat().st_mtime < cutoff:
                    shutil.rmtree(stale, ignore_errors=True)
                    removed += 1
            except OSError:
                continue

    return removed


def install() -> Path | None:
    """Point TMPDIR at a real-disk directory and prune older runs. Best-effort.

    Returns the directory in use, or None if it could not be prepared — in which
    case TMPDIR is left alone and the suite runs as it did before. The caller
    gets a working suite either way; silently filling a tmpfs is the only outcome
    worth avoiding, and the caller reports the path so this stays visible.
    """
    # Decided before the call sets TMPDIR, because that assignment is what would
    # otherwise make an operator-chosen directory look like one of ours.
    owned = not (os.environ.get("ARIEL_TEST_TMPDIR") or os.environ.get("TMPDIR"))
    destination = root()
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    os.environ["TMPDIR"] = str(destination)
    # tempfile caches its answer in a module global; without this reset, a value
    # computed before this call (by a plugin, or by an earlier import) sticks and
    # TMPDIR is silently ignored — which is exactly how the suite ended up on
    # tmpfs with the variable apparently set.
    tempfile.tempdir = None
    if Path(tempfile.gettempdir()).resolve() != destination.resolve():
        return None

    # Hygiene must never be the reason a suite fails to start.
    with contextlib.suppress(OSError):
        prune(destination, owned=owned)
    return destination
