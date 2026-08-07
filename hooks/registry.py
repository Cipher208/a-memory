"""
Hook Registry - central dispatch for all hooks

Handlers receive (context, mem) where mem is MemoryManager | None.
Each handler is registered with a layer (user/agent/both).
Only handlers matching the current layer are fired.
"""

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from config import config

logger = logging.getLogger(__name__)


def _discover_hook_names(cls) -> set[str]:
    """Auto-discover hook names from registered handlers."""
    # Only return names that are actual hook handlers, not helper methods
    known_hooks = set()
    for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_") and not name.startswith("__"):
            hook_name = name.lstrip("_")
            # Skip internal methods that aren't hooks
            if hook_name not in ("register_all", "calculate_importance"):
                known_hooks.add(hook_name)
    return known_hooks


def _is_async(func: Callable) -> bool:
    return asyncio.iscoroutinefunction(func)


class HookRegistry:
    def __init__(self):
        self._hooks: dict[str, list[tuple[Callable, str]]] = {}  # handler, layer

    def register(self, hook_name: str, handler: Callable, layer: str = "both"):
        """Register a handler. layer: 'user', 'agent', or 'both'."""
        if hook_name not in self._hooks:
            self._hooks[hook_name] = []
        self._hooks[hook_name].append((handler, layer))

    async def fire(self, hook_name: str, layer: str, context: dict[str, Any], mem=None) -> dict[str, Any]:
        """Fire a hook for the given layer, passing mem to handlers."""
        from hooks.agent_hooks import AgentHooks
        from hooks.user_hooks import UserHooks

        known_user = _discover_hook_names(UserHooks)
        known_agent = _discover_hook_names(AgentHooks)

        if (hook_name in known_user or hook_name in known_agent) and not config.is_hook_enabled(layer, hook_name):
            return {"skipped": True, "reason": "hook_disabled"}

        handlers = self._hooks.get(hook_name, [])
        if not handlers:
            return {"skipped": True, "reason": "no_handlers"}

        results = []
        for handler, handler_layer in handlers:
            # Only fire handlers registered for this layer (or "both")
            if handler_layer != "both" and handler_layer != layer:
                continue

            try:
                sig = inspect.signature(handler)
                if "mem" in sig.parameters:
                    result = handler(context, mem=mem) if _is_async(handler) else handler(context, mem=mem)
                else:
                    result = handler(context) if _is_async(handler) else handler(context)

                if asyncio.iscoroutine(result):
                    result = await result
                results.append(result)
            except Exception as e:
                logger.error(f"Hook {hook_name} failed: {e}")
                results.append({"error": str(e)})

        return {"results": results, "handler_count": len(results)}

    def list_hooks(self) -> dict[str, int]:
        return {name: len(handlers) for name, handlers in self._hooks.items()}


# Global instance
hook_registry = HookRegistry()
