"""`kill -USR1` must land a real stack dump on disk — the no-root debugging path.

2026-10-06: the nightly pass was dying and "where is the maintenance thread
stuck" needed an answer that py-spy could not give without root
(`ptrace_scope=1`). `_install_faulthandler` is that answer, so it is worth
proving it actually writes a stack rather than merely not raising.
"""

import os
import signal
import threading

from mcp_server import server


def test_sigusr1_writes_a_stack_dump_to_the_log_dir(tmp_path, monkeypatch):
    """The whole point: a signal produces a readable stack in the data dir."""
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(tmp_path))
    previous_file = server._STACK_DUMP_FILE
    try:
        server._install_faulthandler()
        dump = tmp_path / "logs" / "stack-dump.txt"
        assert dump.exists(), "the dump target is created at install time"

        # Start a thread with a recognisable name and park it, so the dump has a
        # frame to show beyond the test itself.
        release = threading.Event()
        worker = threading.Thread(target=release.wait, name="wedged-worker", daemon=True)
        worker.start()

        os.kill(os.getpid(), signal.SIGUSR1)
        # The signal lands at the next bytecode boundary, so allow a moment for
        # the C-level handler to flush.
        for _ in range(200):
            text = dump.read_text(encoding="utf-8")
            if "Current thread" in text:
                break
            threading.Event().wait(0.01)
        release.set()
        worker.join(timeout=5)

        assert f"pid={os.getpid()}" in text, "each install records which process armed it"
        assert "File " in text, "the dump carries real stack frames"
        # The property that matters: the parked thread shows up even though the
        # signal was handled elsewhere. With all_threads=False the dump would
        # hold exactly one entry — the thread that took the signal — which is
        # useless for "a maintenance thread is wedged".
        #
        # Note what this CANNOT assert: faulthandler prints bare "Thread 0x<id>"
        # headers and never thread names (names are a `threading` concept it does
        # not reach). Names only surface in a py-spy dump, which needs root here.
        entries = text.count("Thread 0x") + text.count("Current thread")
        assert entries >= 2, f"expected the parked thread in the dump, saw {entries} thread(s)"
    finally:
        # Leave the pytest process exactly as found: an armed SIGUSR1 handler
        # would outlive this test and surprise later ones.
        import faulthandler

        faulthandler.unregister(signal.SIGUSR1)
        if server._STACK_DUMP_FILE is not None:
            server._STACK_DUMP_FILE.close()
        server._STACK_DUMP_FILE = previous_file


def test_unwritable_log_dir_does_not_stop_the_server(tmp_path, monkeypatch):
    """Best-effort by contract: a bad data dir must never block startup."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("this is a file, so mkdir under it must fail", encoding="utf-8")
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(blocker))

    previous_file = server._STACK_DUMP_FILE
    try:
        server._install_faulthandler()  # must not raise
    finally:
        server._STACK_DUMP_FILE = previous_file


def test_backup_cron_thread_is_named_for_py_spy():
    """faulthandler cannot show names, so the name is for py-spy dumps only.

    py-spy prints `Thread 12345 (idle): "backup-cron"` (the format seen in this
    repo's own past debugging sessions), which is what makes the maintenance
    thread identifiable when someone does have root.
    """
    import inspect

    source = inspect.getsource(__import__("features.backup_cron", fromlist=["x"]))
    assert 'name="backup-cron"' in source
