"""Two processes on one base must not both run the nightly pass.

`backup_cron._check_nightly` read the cycles gate *before* taking its single-writer lock:

    gate = nightly_gate(state_path)      # read, unguarded
    if gate["action"] != "run": return
    fd = self._acquire_backup_lock()     # only now

Both processes see "due", both proceed, and the loser has already touched the database by
the time it loses. That window is what matters: the nightly pass writes `core_memory`
through the forgetting path, and the loser's read transaction is what makes the winner's
write fail.

Live evidence for the shape: the cowagent base has two MCP processes and 566 double
`backup_cron` log lines within two seconds of each other (1644 lines over 662 distinct
minutes); hermes and mimocode have one process each and zero doubles.

TWO CALLS IN A ROW DO NOT MODEL THIS. Sequentially, the first pass has already released
the lock by the time the second asks, so the test passes on broken code. The overlap is
what to model: the second instance asks *while the first is inside its pass*. These tests
therefore run the second instance from the first one's hook — the same instant, which is
the only shape in which the defect exists.
"""

import time

import pytest


@pytest.fixture
def two_crons(tmp_path, monkeypatch):
    """Two BackupCron instances on one base, gate forced to 'run'."""
    from features import cycles
    from features.backup_cron import BackupCron

    monkeypatch.setattr(
        cycles,
        "nightly_gate",
        lambda state_path: {"action": "run", "budget": "allow", "reason": "due"},
    )

    def build():
        c = BackupCron()
        c.base_dir = tmp_path
        c._lock_path = tmp_path / ".backup_cron.lock"
        c._state_file = tmp_path / ".backup_cron_state.json"
        return c

    return build, tmp_path


def test_the_second_instance_cannot_enter_while_the_first_is_inside(two_crons):
    """The property: whoever is inside the pass excludes the other, at that instant."""
    build, _ = two_crons
    a, b = build(), build()

    fired: list[str] = []

    b._fire_nightly_hooks = lambda *x, **k: (fired.append("b"), "ok")[1]

    def a_hook(state_path=None, *, gate_checked=False):
        # The second instance asks WHILE the first is inside.
        b._check_nightly(time.time())
        fired.append("a")
        return "ok"

    a._fire_nightly_hooks = a_hook

    a._check_nightly(time.time())

    assert fired == ["a"], f"both passes entered: {fired}"


def test_the_second_instance_does_not_even_read_the_gate(two_crons):
    """Reading is where the collision begins, so the excluded instance must not read."""
    from features import cycles

    build, _ = two_crons
    a, b = build(), build()

    reads: list[str] = []
    orig = cycles.nightly_gate

    def counting_gate(state_path):
        reads.append(str(state_path))
        return orig(state_path)

    cycles.nightly_gate = counting_gate
    try:
        b._fire_nightly_hooks = lambda *x, **k: "ok"

        def a_hook(state_path=None, *, gate_checked=False):
            b._check_nightly(time.time())
            return "ok"

        a._fire_nightly_hooks = a_hook
        a._check_nightly(time.time())
    finally:
        cycles.nightly_gate = orig

    assert len(reads) == 1, f"the gate was read {len(reads)} times; only the leader may read it"


def test_the_lock_is_released_after_the_pass(two_crons):
    """Excluding the second instance must not mean wedging the base afterwards."""
    from shared import maintenance_lock

    build, tmp_path = two_crons
    a = build()
    a._fire_nightly_hooks = lambda *x, **k: "ok"

    a._check_nightly(time.time())

    fd = maintenance_lock.acquire(tmp_path)
    assert fd is not None, "the nightly lock was not released"
    maintenance_lock.release(fd)


def test_a_failing_pass_still_releases_the_lock(two_crons):
    """An exception in the pass must not leave the base locked forever."""
    from shared import maintenance_lock

    build, tmp_path = two_crons
    a = build()

    def explode(*x, **k):
        raise RuntimeError("boom")

    a._fire_nightly_hooks = explode

    with pytest.raises(RuntimeError):
        a._check_nightly(time.time())

    fd = maintenance_lock.acquire(tmp_path)
    assert fd is not None, "the lock leaked on the error path"
    maintenance_lock.release(fd)


def test_an_already_locked_base_blocks_the_pass_entirely(two_crons):
    """Direct check of the ordering: lock held by someone else => the hook never runs.

    The overlap test above models the same instant through a nested call; this one pins
    the ordering itself, with the lock taken from outside. On the old code the gate was
    read and the hooks fired before the lock was ever consulted, so this fails there.
    """
    import fcntl
    import os

    build, tmp_path = two_crons
    a = build()

    fired: list[str] = []
    a._fire_nightly_hooks = lambda *x, **k: (fired.append("a"), "ok")[1]

    lock_path = tmp_path / ".backup_cron.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        a._check_nightly(time.time())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert fired == [], f"the pass ran while another writer held the lock: {fired}"
