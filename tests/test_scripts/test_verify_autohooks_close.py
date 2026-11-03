"""`verify_autohooks.py` must close its connections when the body raises.

The script ends with `os._exit` to sidestep the repo-wide aiosqlite teardown
quirk, and arms `faulthandler.dump_traceback_later(45, exit=True)`. Both are
deliberate. The defect was that `close_all` sat on the success path only: an
exception between the work and the end left the non-daemon aiosqlite workers
alive, so the interpreter parked in `threading._shutdown` until the 45 s guard
fired and the exit code came from faulthandler instead of the real failure.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def close_calls(monkeypatch) -> list[str]:
    """Record close_all() on the shared singleton, leaving the real one unused."""
    from shared.connection import connection_manager

    calls: list[str] = []

    async def _close() -> None:
        calls.append("close")

    monkeypatch.setattr(connection_manager, "close_all", _close)
    return calls


def test_main_closes_when_the_body_raises(close_calls: list[str], monkeypatch) -> None:
    module = importlib.import_module("scripts.verify_autohooks")

    def _boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "make_schemas", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(module.main())
    assert close_calls[-1] == "close"


def test_a_raising_body_exits_promptly_instead_of_waiting_for_the_45s_guard(tmp_path: Path) -> None:
    """Proof of the hang: an async seam opens a real connection, then raises.
    With a close in `finally` the process prints the traceback and exits in
    seconds; without one it is still alive at 20 s, waiting for the 45 s
    faulthandler."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    code = textwrap.dedent(
        f"""
        import asyncio, faulthandler, sys, time
        sys.path.insert(0, {str(REPO)!r})
        import scripts.verify_autohooks as v
        import hooks.external as hx

        async def _boom(*_args, **_kwargs):
            from shared.connection import connection_manager
            from shared.constants import DB_NAME
            await connection_manager.get(DB_NAME)
            raise RuntimeError("boom")

        hx.dispatch_event = _boom
        faulthandler.dump_traceback_later(45, exit=True)
        started = time.monotonic()
        try:
            asyncio.run(v.main())
        finally:
            print(f"ELAPSED={{time.monotonic() - started:.2f}}")
        """
    )

    env = {
        **os.environ,
        "MCP_MEMORY_DATA_DIR": str(data_dir),
        "ARIEL_HASH_EMBEDDINGS": "1",
        "BACKUP_CRON_DISABLED": "1",
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the script did not exit within 20s — the 45s faulthandler guard was still in charge")

    assert proc.returncode != 0, proc.stdout
    assert "RuntimeError: boom" in proc.stderr, proc.stderr
    assert "Timeout (0:00:45)" not in proc.stderr, proc.stderr
    elapsed = float(proc.stdout.split("ELAPSED=")[-1].split()[0])
    assert elapsed < 10, proc.stdout
