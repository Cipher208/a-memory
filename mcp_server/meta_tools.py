"""Stage 2-C: meta-tool dispatchers — one visible tool per exposure tier.

Generated programmatically from server.EXTRA_TIERS (the single source of
truth). The model sees ~13 schemas (7 primitives + 6 dispatchers) instead
of 57 flat tools; per-tool parameters are discovered via action='list'
which returns the Plan-B behavior hints and the slot of each member tool.

Opt-in: ARIEL_META=1 in server._register_all_tools. Default off — the
flat surface (eval harness, existing clients) is unchanged.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from mcp_server.annotations import hints_for
from mcp_server.slots import slot_of

if TYPE_CHECKING:
    from collections.abc import Callable


def _catalog(tool_names: set[str], all_tools: dict[str, Callable[..., Any]]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for name in sorted(tool_names):
        fn = all_tools.get(name)
        if fn is None:
            continue
        doc = inspect.getdoc(fn) or ""
        hints = hints_for(name)
        entries[name] = {
            "description": doc.split("\n")[0],
            "read_only": bool(hints.read_only),
            "destructive": bool(hints.destructive),
            "slot": slot_of(name),
        }
    return entries


def build_meta_tools(
    all_tools: dict[str, Callable[..., Any]],
    expose: set[str],
) -> dict[str, Callable[..., Any]]:
    """Build one dispatcher per EXTRA_TIERS tier present in expose.

    Called only from server._register_all_tools with expose == the resolved
    ARIEL_EXPOSE set. Each dispatcher has the compact wire schema
    (action: str, args: dict | None).
    """
    from mcp_server.server import EXTRA_TIERS

    dispatchers: dict[str, Callable[..., Any]] = {}
    for tier, matcher in EXTRA_TIERS.items():
        tier_names = matcher(tier, set(all_tools)) & expose
        if not tier_names:
            continue
        dispatchers[tier] = _make_dispatcher(tier, tier_names, all_tools)
    return dispatchers


def _make_dispatcher(
    tier: str,
    names: set[str],
    tools: dict[str, Callable[..., Any]],
) -> Callable[..., Any]:
    """Build one tier dispatcher.

    The factory call pins the closure values (the SDK rejects
    underscore-carrying default-arg pins in signatures).
    """

    async def dispatcher(
        action: str,
        args: dict[str, Any] | None = None,
        ctx: Any = None,
    ) -> dict[str, Any]:
        if action == "list":
            return {"tools": _catalog(names, tools)}
        if action not in names:
            return {"error": "unknown action", "available": sorted(names)}
        fn = tools.get(action)
        if fn is None:
            return {"error": "unknown action", "available": sorted(names)}
        kwargs: dict[str, Any] = dict(args or {})
        if "ctx" in inspect.signature(fn).parameters:
            kwargs.setdefault("ctx", ctx)
        # user_id binding must survive the meta hop: flat registration
        # wraps every tool with _scope_tool; dispatchers do the same
        # lazily per call (cheap vs the tool's own DB work).
        from mcp_server.server import _scope_tool

        result = _scope_tool(fn)(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return dict(result) if isinstance(result, dict) else {"result": result}

    dispatcher.__name__ = tier
    dispatcher.__doc__ = (
        f"Meta-tool for the '{tier}' tier. action='list' returns the "
        f"catalog ({len(names)} tools, with Plan-B behavior hints "
        f"and slots); otherwise action=<tool name> and args=<kwargs>."
    )
    return dispatcher
