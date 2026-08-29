"""C1.1: hook registry threads graph to handlers that declare it."""

from __future__ import annotations

from typing import Any

import pytest

from hooks.registry import HookHandler, HookRegistry, hook_registry


@pytest.mark.asyncio
async def test_fire_passes_graph_to_declaring_handler() -> None:
    reg = HookRegistry()
    seen: dict[str, Any] = {}

    class _Probe:
        @reg.mark("graph_probe", layer="both")
        async def handler(self, ctx: dict[str, Any], mem: Any = None, graph: Any = None) -> dict[str, Any]:
            seen["graph"] = graph
            return {"ok": True}

    reg.register_instance(_Probe())
    result = await reg.fire("graph_probe", "user", {}, graph="G1")
    assert seen["graph"] == "G1"
    assert result["handler_count"] == 1


@pytest.mark.asyncio
async def test_legacy_handler_without_graph_param_still_works() -> None:
    reg = HookRegistry()

    class _Probe:
        @reg.mark("legacy_probe", layer="both")
        async def handler(self, ctx: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

    reg.register_instance(_Probe())
    result = await reg.fire("legacy_probe", "user", {}, graph="G1")
    assert result["results"] == [{"ok": True}]


def test_mark_detects_graph_param() -> None:
    reg = HookRegistry()

    @reg.mark("detect_probe", layer="both")
    async def handler(ctx: dict[str, Any], graph: Any = None) -> None:
        return None

    meta = handler._hook_metadata  # type: ignore[attr-defined]
    assert meta["takes_graph"] is True


@pytest.mark.asyncio
async def test_hook_handler_model_takes_graph_field() -> None:
    async def fn(ctx: dict[str, Any], graph: Any = None) -> None:
        return None

    h = HookHandler(func=fn, name="x", layer="both", is_async=True, takes_mem=False, takes_graph=True)
    assert h.takes_graph is True
    hook_registry.list_hooks()  # singleton untouched, smoke
