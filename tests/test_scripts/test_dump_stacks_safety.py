"""`scripts/dump_stacks.sh` must never kill the server it is inspecting.

2026-10-06 14:21:28 CEST: an earlier version of the script sent SIGUSR1 to three
live memory servers that predated the faulthandler code. SIGUSR1's default
disposition is to TERMINATE, so all three died — the command meant to look at
them took them down. The script only noticed afterwards, when the dump file
failed to grow, and reported it as "the process has no handler" without ever
connecting the two facts.

These tests pin the property that was missing: a process WITHOUT a SIGUSR1
handler is never signalled at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dump_stacks.sh"

# A server-looking argv: the script requires "mcp_server" in the cmdline and a
# python interpreter as the FIRST field. The extra argv element is what a real
# `python3 .../mcp_server/server.py` invocation looks like to /proc.
FAKE_ARGV = ["mcp_server/server.py"]


def _spawn(base: str, code: str, data_root: Path) -> tuple[subprocess.Popen[bytes], Path]:
    """Start a stand-in memory server, named so the script accepts it."""
    data_dir = data_root / f".mcp-ariel-memory-{base}"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["MCP_MEMORY_DATA_DIR"] = str(data_dir)
    proc = subprocess.Popen(
        [sys.executable, "-c", code, *FAKE_ARGV],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, data_dir


def _run_script(base: str, data_root: Path) -> subprocess.CompletedProcess[str]:
    # The script resolves dirs from the processes' own environ, so only the
    # target name is passed.
    return subprocess.run(
        ["bash", str(SCRIPT), base],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_unarmed_process_is_never_signalled(tmp_path: Path) -> None:
    """The regression: no handler means no signal, so the process survives."""
    proc, _ = _spawn("pytestunarmed", "import time; time.sleep(30)", tmp_path)
    try:
        time.sleep(1.0)
        assert proc.poll() is None, "the stand-in server did not start"

        result = _run_script("pytestunarmed", tmp_path)

        # Give the kernel a moment: a signal would land almost immediately.
        time.sleep(0.5)
        assert proc.poll() is None, f"dump_stacks.sh killed a server without a SIGUSR1 handler — stdout={result.stdout!r} stderr={result.stderr!r}"
        combined = result.stdout + result.stderr
        assert "БЕЗ обработчика SIGUSR1" in combined
        assert "процесс цел" in combined
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_armed_process_yields_a_dump_and_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the handler installed the same run produces a stack, not a corpse."""
    code = (
        "import sys, time; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        "from mcp_server.server import _install_faulthandler; "
        "_install_faulthandler(); "
        "time.sleep(30)"
    )
    proc, data_dir = _spawn("pytestarmed", code, tmp_path)
    try:
        dump = data_dir / "logs" / "stack-dump.txt"
        deadline = time.time() + 20
        while time.time() < deadline and not dump.exists():
            time.sleep(0.2)
        assert dump.exists(), "the armed stand-in never created its dump target"

        result = _run_script("pytestarmed", tmp_path)

        assert proc.poll() is None, "an armed server must survive the signal"
        text = dump.read_text(encoding="utf-8")
        assert "most recent call first" in text, "the dump holds no thread stacks"
        assert "дамп добавлен" in result.stdout, result.stdout
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_shell_wrapper_is_not_mistaken_for_the_server(tmp_path: Path) -> None:
    """mimocode's launcher is `sh -c '... python3 .../mcp_server/server.py'`.

    That shell also carries MCP_MEMORY_DATA_DIR (its `set -a` exports it) and has
    "mcp_server" in its cmdline, so it passes both of the naive checks. The
    script must require a python interpreter as the first cmdline field; dumping
    the shell would show the launcher, and on old code killing it would orphan
    the real server.
    """
    python_proc, data_dir = _spawn("pytestwrapper", "import time; time.sleep(30)", tmp_path)
    wrapper = subprocess.Popen(
        ["sh", "-c", f"set -a; MCP_MEMORY_DATA_DIR={data_dir}; set +a; echo up; sleep 30"],
        # The wrapper's own cmdline must mention mcp_server, like the real one.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.0)
        result = _run_script("pytestwrapper", tmp_path)
        # Whatever it chose, the python server must still be alive and the run
        # must not have signalled the unarmed shell (which would kill it).
        time.sleep(0.5)
        assert python_proc.poll() is None
        assert wrapper.poll() is None, "the unarmed shell wrapper was signalled and died"
        assert "дамп добавлен" in result.stdout or "БЕЗ обработчика" in result.stdout + result.stderr
    finally:
        for p in (wrapper, python_proc):
            p.kill()
            p.wait(timeout=10)
