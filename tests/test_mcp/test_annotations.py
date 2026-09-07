"""Stage 2 Plan B: MCP behavior annotations — карта покрывает registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mcp_server.tools_layer  # noqa: F401 — populates the registry

if TYPE_CHECKING:
    import pytest

from mcp_server.annotations import _ANNOTATIONS, annotations_for, hints_for
from mcp_server.registry import get_all_tools


def test_annotations_cover_entire_registry() -> None:
    """Каждый из 65 тулов в карте; в карте нет stale-имён."""
    tools = set(get_all_tools())
    mapped = set(_ANNOTATIONS)
    assert mapped == tools, f"missing={sorted(tools - mapped)} stale={sorted(mapped - tools)}"


def test_read_only_tools_are_sane() -> None:
    """Читающие — read_only+idempotent, НЕ destructive; write-тулы — не read_only."""
    for name in ("dream", "memory_recall", "memory_search", "memory_query", "wiki_read", "memory_stats", "daily_brief"):
        h = hints_for(name)
        assert h.read_only and h.idempotent and not h.destructive, f"{name}: {h}"
    for name in ("memory_remember", "wiki_add", "memory_graph_add", "memory_episode_save"):
        assert not hints_for(name).read_only, f"{name} пишет, но помечен read-only"


def test_destructive_explicitly_flagged() -> None:
    for name in ("forget", "wiki_delete", "memory_cleanup", "memory_heal", "memory_data"):
        assert hints_for(name).destructive, f"{name} должен быть destructive"


def test_unknown_tool_falls_back_conservative() -> None:
    """Не-картное имя: НЕ read-only, destructive=True (MCP default) — промоутем
    write-тул к безопасным быть не может."""
    h = hints_for("brand_new_unknown_tool")
    assert not h.read_only and h.destructive
    a = annotations_for("brand_new_unknown_tool")
    assert a.read_only_hint is False and a.destructive_hint is True and a.idempotent_hint is False


def test_server_wires_annotations_into_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регистрация в server.py передаёт annotations — на wire Tool несёт hints."""
    monkeypatch.setenv("ARIEL_EXPOSE", "all")
    import importlib
    import mcp_server.server as server_mod

    importlib.reload(server_mod)
    mcp = server_mod.mcp

    async def _probe():
        tools = await mcp.list_tools()
        return {t.name: t for t in tools}

    import asyncio

    wired = asyncio.run(_probe())
    assert wired, "server зарегистрировал тула"
    # read-примитив и destructive-опс получают корректные hints на wire
    dream = wired["dream"]
    assert dream.annotations.read_only_hint is True and dream.annotations.destructive_hint is False
    cleanup = wired["memory_cleanup"]
    assert cleanup.annotations.destructive_hint is True and cleanup.annotations.read_only_hint is False
