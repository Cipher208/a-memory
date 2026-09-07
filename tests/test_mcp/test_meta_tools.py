"""Stage 2-C: meta-tool dispatchers generated from EXTRA_TIERS."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING

import mcp_server.tools_layer  # noqa: F401
from mcp_server.meta_tools import build_meta_tools
from mcp_server.registry import get_all_tools
from mcp_server.server import EXTRA_TIERS, resolve_exposure

if TYPE_CHECKING:
    import pytest


def _dispatchers() -> dict[str, object]:
    tools = get_all_tools()
    expose = resolve_exposure("operator", set(tools))  # all tiers incl admin
    return build_meta_tools(tools, expose)


def test_dispatcher_per_tier() -> None:
    d = _dispatchers()
    assert set(d) == set(EXTRA_TIERS)  # context, insight, write, wiki, review, admin
    for fn in d.values():
        assert asyncio.iscoroutinefunction(fn)


def test_wire_schema_is_compact() -> None:
    """Dispatcher signature must be (action, args) — probe-verified SDK shape."""
    d = _dispatchers()
    sig = inspect.signature(d["wiki"])
    params = list(sig.parameters)
    assert params[0] == "action"
    assert params[1] == "args"


def test_catalog_lists_tier_tools_with_hints() -> None:
    d = _dispatchers()
    out = asyncio.run(d["wiki"](action="list"))
    catalog = out["tools"]
    assert set(catalog) == EXTRA_TIERS["wiki"]("wiki", set(get_all_tools()))
    entry = catalog["wiki_search"]
    assert entry["read_only"] is True
    assert entry["destructive"] is False
    assert entry["slot"] == "wiki"
    # destructive member flagged
    assert catalog["wiki_delete"]["destructive"] is True


def test_dispatch_real_tool_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    d = _dispatchers()

    # Route through the real wiki_list: fake the app extraction (the flat
    # tests do the same) — fake app exposes user_wiki like AppContext does.
    class _FakeWiki:
        async def list_all(self, limit: int) -> list[object]:
            from types import SimpleNamespace

            page = SimpleNamespace(title="x", wiki_type="note", tags=[], file_path="/tmp/x.md")
            return [page]

    class _FakeApp:
        user_wiki = _FakeWiki()

    monkeypatch.setattr("mcp_server.tools.wiki._get_ctx", lambda ctx: _FakeApp())
    out = asyncio.run(d["wiki"](action="wiki_list"))
    assert "error" not in out  # wiki_list returns its real payload


def test_unknown_action_and_missing_tool() -> None:
    d = _dispatchers()
    out = asyncio.run(d["wiki"](action="nope"))
    assert out["error"] == "unknown action"
    assert "wiki_search" in out["available"]
    # tool in tier map but not registered: simulate by building with
    # an expose set that includes a name absent from tools
    tools = get_all_tools()
    fake = dict(tools)
    expose = resolve_exposure("operator", set(tools))
    fake.pop("memory_backup", None)
    d2 = build_meta_tools(fake, expose)
    out2 = asyncio.run(d2["admin"](action="memory_backup"))
    assert out2["error"] == "unknown action"


def test_ctx_passthrough_when_target_expects_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the target tool signature has ctx, the dispatcher must inject it."""
    seen: dict[str, object] = {}

    async def fake_memory_stats(ctx: object | None = None) -> dict[str, object]:
        seen["ctx"] = ctx
        return {"ok": True}

    import mcp_server.meta_tools as mt

    tools = dict(get_all_tools())
    tools["memory_stats"] = fake_memory_stats
    expose = resolve_exposure("agent", set(get_all_tools()))
    d2 = mt.build_meta_tools(tools, expose)
    sentinel = object()
    out = asyncio.run(d2["insight"](action="memory_stats", ctx=sentinel))
    assert out == {"ok": True}
    assert seen["ctx"] is sentinel


def test_meta_layer_flag_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ARIEL_META=1 the server module imports fine; flat surface unchanged."""
    import importlib

    import mcp_server.server as server_mod

    monkeypatch.setenv("ARIEL_EXPOSE", "all")
    monkeypatch.delenv("ARIEL_META", raising=False)
    importlib.reload(server_mod)
    # sanity: registry untouched
    assert len(get_all_tools()) == 66
