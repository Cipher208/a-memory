"""Every operator script must close its aiosqlite connections on the failure path.

aiosqlite worker threads are non-daemon: `threading._shutdown` joins them at
interpreter exit, so a script that only closes *after* its work call hangs
forever once that call raises. Reproduced on the exact module-level shape of
`warm_ner_cache.py` / `warm_embedding_cache.py`: exit 124 (external timeout)
without the guard, exit 1 with it.

Each test drives a real failure through the script's own entry point and
requires the close to be the last thing that happens — a close that ran only on
the happy path leaves the failing call as the last event.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
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


@pytest.mark.parametrize(
    "module_name",
    ["scripts.dedup_core_twins", "scripts.dedup_episode_twins", "scripts.resolve_conflicts"],
)
async def test_run_closes_when_the_work_call_raises(module_name: str, close_calls: list[str], monkeypatch) -> None:
    module = importlib.import_module(module_name)

    def _boom(_cm):
        close_calls.append("fail")
        raise RuntimeError("boom")

    monkeypatch.setattr("shared.write_dispatch.get_write_dispatch", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        await module.run(False)
    assert close_calls[-1] == "close"


async def test_run_manifest_closes_when_drift_check_raises(tmp_path: Path, close_calls: list[str], monkeypatch) -> None:
    from shared.connection import connection_manager

    module = importlib.import_module("scripts.mirror_identity")
    target = tmp_path / "AGENT.md"
    target.write_text("## Alpha\n- a body long enough to be a chunk.\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps([{"base": str(tmp_path / "base"), "file": str(target), "kind": "rule", "layer": "agent"}]),
        encoding="utf-8",
    )
    original_base = connection_manager.base_dir

    async def _boom(*_args, **_kwargs):
        close_calls.append("fail")
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "check_drift", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        await module.run_manifest(manifest)
    assert close_calls[-1] == "close"
    assert connection_manager.base_dir == original_base


def test_mirror_main_closes_when_mirror_raises(tmp_path: Path, close_calls: list[str], monkeypatch) -> None:
    module = importlib.import_module("scripts.mirror_identity")
    target = tmp_path / "AGENT.md"
    target.write_text("## Alpha\n- a body long enough to be a chunk.\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["mirror_identity.py", "--file", str(target)])

    class _Memory:
        def agent_memory(self, _user: str) -> object:
            return object()

    class _App:
        mm = _Memory()

    async def _boom(*_args, **_kwargs):
        close_calls.append("fail")
        raise RuntimeError("boom")

    monkeypatch.setattr("mcp_server.context.AppContext", _App)
    monkeypatch.setattr(module, "mirror", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        module.main()
    assert close_calls[-1] == "close"


def test_docs_to_wiki_closes_when_sync_raises(tmp_path: Path, close_calls: list[str], monkeypatch) -> None:
    module = importlib.import_module("scripts.docs_to_wiki")
    page = tmp_path / "page.md"
    page.write_text("# page\n", encoding="utf-8")
    monkeypatch.setattr(module, "convert_to_ssot", lambda _source, _ssot: [page])

    class _WikiManager:
        def __init__(self, **_kwargs) -> None:
            pass

        async def sync_external(self, _dirs) -> dict[str, int]:
            close_calls.append("fail")
            raise RuntimeError("boom")

    monkeypatch.setattr("wiki.manager.WikiManager", _WikiManager)
    with pytest.raises(RuntimeError, match="boom"):
        module.sync_one(tmp_path)
    assert close_calls[-1] == "close"


def test_purge_foreign_rows_closes_when_apply_raises(tmp_path: Path, close_calls: list[str], monkeypatch) -> None:
    module = importlib.import_module("scripts.purge_foreign_rows")
    db = tmp_path / "memory.db"
    db.write_bytes(b"")
    monkeypatch.setattr(sys, "argv", ["purge_foreign_rows.py", "--apply"])
    monkeypatch.setattr(module, "_db_path", lambda: db)
    monkeypatch.setattr(module, "plan", lambda _db: {})
    monkeypatch.setattr(module, "report", lambda _plan, _db: None)
    monkeypatch.setattr(
        module,
        "_counts",
        lambda _db: {"episodes": 0, "core_memory": 0, "epi_nodes": 0, "epi_edges": 0, "l0_journal": 0},
    )

    async def _boom(_plan, _db):
        close_calls.append("fail")
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "apply", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(module.main())
    assert close_calls[-1] == "close"


@pytest.mark.parametrize("script", ["warm_ner_cache.py", "warm_embedding_cache.py"])
def test_module_level_scripts_exit_after_the_work_call_raises(script: str, tmp_path: Path) -> None:
    """The real repro: no tables in a fresh dir → main() raises after opening a
    connection. Without a close on the failure path the process never exits."""
    data_dir = tmp_path / "base"
    data_dir.mkdir()
    env = {**os.environ, "MCP_MEMORY_DATA_DIR": str(data_dir)}
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        cwd=REPO,
        check=False,
    )
    assert proc.returncode == 1, proc.stderr
    assert "no such table" in proc.stderr


async def test_backfill_transitions_closes_when_dispatch_raises(tmp_path: Path, close_calls: list[str], monkeypatch) -> None:
    """`--backfill-transitions` is a tenth reachable path: the dispatch calls
    open connections and used to close only after them."""
    module = importlib.import_module("scripts.dedup_episode_twins")
    monkeypatch.setattr(module, "ARCHIVE_DIR", tmp_path)
    (tmp_path / "episode-twins-20260101-000000.jsonl").write_text("{}\n", encoding="utf-8")

    class _Dispatch:
        async def query(self, *_args, **_kwargs):
            close_calls.append("fail")
            raise RuntimeError("boom")

    monkeypatch.setattr("shared.write_dispatch.get_write_dispatch", lambda _cm: _Dispatch())
    with pytest.raises(RuntimeError, match="boom"):
        await module.backfill_transitions(False)
    assert close_calls[-1] == "close"


def test_close_failure_on_success_path_is_logged(caplog, monkeypatch) -> None:
    """A close that raises after successful work must leave a trace, not vanish
    behind `contextlib.suppress` — and the return code stays as it was."""
    module = importlib.import_module("scripts.resolve_conflicts")
    from shared.connection import connection_manager

    class _Dispatch:
        async def query(self, *_args, **_kwargs):
            return []

    async def _boom() -> None:
        raise RuntimeError("close boom")

    monkeypatch.setattr("shared.write_dispatch.get_write_dispatch", lambda _cm: _Dispatch())
    monkeypatch.setattr(connection_manager, "close_all", _boom)
    with caplog.at_level(logging.WARNING):
        rc = asyncio.run(module.run(False))
    assert rc == 0
    assert any("close_all" in record.getMessage() for record in caplog.records), caplog.text
