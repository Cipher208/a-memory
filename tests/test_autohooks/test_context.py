# tests/test_autohooks/test_context.py
"""S18-хвост context_assembly: assemble_context + CLI-команда context."""

from __future__ import annotations

from types import SimpleNamespace

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from autohooks.__main__ import _parse_args, main
from autohooks.context import assemble_context, render_context_md
from autohooks.config import load_config


class _L1:
    # реальный ReflexBuffer.get_recent — sync, поля role/content (core/reflex.py)
    def get_recent(self, n: int) -> list:
        return [SimpleNamespace(role="user", content="только что починили дрейф порога")]


class _Mem:
    def __init__(self) -> None:
        self.l1 = _L1()


class _Rag:
    async def search(self, query, user_id="default", strategy="hybrid", limit=10):
        return []


@pytest.mark.asyncio
async def test_assemble_context_shapes_and_budget() -> None:
    mem, rag = _Mem(), _Rag()
    a = await assemble_context(mem, rag, "cu", text="почему дрейфовал порог важности?", budget=500)
    assert set(a) >= {"relevant", "recent", "budget", "used_tokens"}
    assert a["budget"] == 500
    assert a["recent"] and "дрейф порога" in a["recent"][0]
    md = render_context_md(a)
    assert "<recent>" in md, "L1-блок обёрнут тегом"
    for b in a["relevant"]:
        assert str(b.get("content", "")).strip()


def test_context_cli_subcommand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """CLI: autohooks context --config ... --text ... --format json → JSON на stdout."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    cfg = tmp_path / "a.yaml"
    cfg.write_text(
        f"""
data_dir: {data_dir}
user_id: ctxu
source:
  driver: sqlite
  path: {tmp_path}/conv.db
  table: messages
  cursor_column: id
  order_by: id
  role: {{column: role}}
  text: {{column: content}}
""",
        encoding="utf-8",
    )
    import sqlite3

    src = sqlite3.connect(tmp_path / "conv.db")
    src.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
    src.commit()
    src.close()

    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MCP_MASTER_KEY", "ctx-key")
    monkeypatch.setattr(
        "sys.argv",
        ["autohooks", "context", "--config", str(cfg), "--text", "что решили про деплой?", "--format", "json"],
    )
    from shared.connection import connection_manager

    original = connection_manager.base_dir
    connection_manager.base_dir = data_dir
    try:
        code = main()
    finally:
        connection_manager._conns.clear()
        connection_manager.base_dir = original
    assert code == 0
    out = capsys.readouterr().out.strip()
    parsed = __import__("json").loads(out)
    assert {"relevant", "recent", "budget"} <= set(parsed)


def test_context_args_parse() -> None:
    ns = _parse_args(["context", "--config", "x.yaml", "--text", "q", "--format", "json", "--budget", "777"])
    assert ns.command == "context" and ns.text == "q" and ns.budget == "777"
    load_config  # noqa: B018 — import sanity (config import path unchanged)
