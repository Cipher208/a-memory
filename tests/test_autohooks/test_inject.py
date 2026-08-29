# tests/test_autohooks/test_inject.py
"""Inject subcommand: dispatch session_started, render blocks (spec S5)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from autohooks.config import AgentConfig, FieldMap, SourceConfig
from autohooks.inject import run_inject

if TYPE_CHECKING:
    from pathlib import Path


def _cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        data_dir=tmp_path / "data",
        user_id="u1",
        layer="user",
        source=SourceConfig(
            driver="sqlite",
            path=tmp_path / "conv.db",
            table="messages",
            cursor_column="id",
            order_by="id",
            role=FieldMap(column="role"),
            text=FieldMap(column="content"),
            ts=None,
            filter=None,
        ),
        state_file=tmp_path / "cursor.json",
    )


async def test_md_output_contains_block_content(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        seen["event"] = event
        seen["payload"] = payload
        return {"results": [{"blocks": [{"kind": "core", "content": "Fact A", "score": 0.9}]}], "handler_count": 1}

    out = await run_inject(_cfg(tmp_path), mem=None, graph=None, rag=None, dispatch=_dispatch)
    assert seen["event"] == "session_started"
    assert "Fact A" in out
    assert "[core]" in out


async def test_json_output_shape(tmp_path: Path) -> None:
    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        return {"results": [{"blocks": [{"kind": "recent", "content": "L1 line", "score": 0.5}]}], "handler_count": 1}

    out = await run_inject(_cfg(tmp_path), mem=None, graph=None, rag=None, fmt="json", dispatch=_dispatch)
    parsed = json.loads(out)
    assert parsed["blocks"][0]["kind"] == "recent"


async def test_no_blocks_yields_empty_marker(tmp_path: Path) -> None:
    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        return {"results": [], "handler_count": 0}

    out = await run_inject(_cfg(tmp_path), mem=None, graph=None, rag=None, dispatch=_dispatch)
    assert out == "—"
