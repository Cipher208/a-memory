from __future__ import annotations
import asyncio
import inspect
import logging
from typing import Any, TYPE_CHECKING, TypeVar
from collections.abc import Callable  # noqa: TC003
from pydantic import BaseModel, ConfigDict

from shared.sqlite_busy import is_busy

if TYPE_CHECKING:
    F = TypeVar("F", bound=Callable[..., Any])

# Removed direct config dependency to fix tests
# from config import config

logger = logging.getLogger(__name__)


class HookHandler(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    func: Callable[..., Any]
    name: str
    layer: str
    is_async: bool
    takes_mem: bool
    takes_graph: bool = False
    instance: Any | None = None


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookHandler]] = {}
        self._enabled_hooks: set[str] = set()  # Optional explicit enablement

    @staticmethod
    def _identity(handler: HookHandler) -> tuple[str, str | None, str]:
        """Who a registration is: same hook, same class, same underlying function.

        F84: ``AppContext()`` calls ``register_instance`` on every construction,
        so without this the global registry grows a full set per context and
        every hook fires N times. The class name is in the key so two different
        hook classes registering the same hook name both survive.
        """
        func = handler.func
        module = getattr(func, "__module__", "") or ""
        # `__qualname__` keeps two same-named functions from different modules
        # apart; `__name__` is the fallback for a callable without one.
        return (
            handler.name,
            type(handler.instance).__name__ if handler.instance is not None else None,
            f"{module}.{getattr(func, '__qualname__', getattr(func, '__name__', repr(func)))}",
        )

    def register(self, handler: HookHandler) -> None:
        if handler.name not in self._hooks:
            self._hooks[handler.name] = []
        identity = self._identity(handler)
        if any(self._identity(existing) == identity for existing in self._hooks[handler.name]):
            return
        self._hooks[handler.name].append(handler)

    def register_instance(self, obj: Any) -> None:
        for name, method in inspect.getmembers(obj, predicate=inspect.ismethod):
            meta = getattr(method, "_hook_metadata", None)
            if meta:
                handler = HookHandler(
                    func=method,
                    name=meta["name"],
                    layer=meta["layer"],
                    is_async=meta["is_async"],
                    takes_mem=meta["takes_mem"],
                    takes_graph=meta.get("takes_graph", False),
                    instance=obj,
                )
                self.register(handler)

    def mark(self, hook_name: str, layer: str = "both") -> Callable[[F], F]:
        def decorator(func: F) -> F:
            sig = inspect.signature(func)
            func._hook_metadata = {  # type: ignore[attr-defined]
                "name": hook_name,
                "layer": layer,
                "is_async": asyncio.iscoroutinefunction(func),
                "takes_mem": "mem" in sig.parameters,
                "takes_graph": "graph" in sig.parameters,
            }
            return func

        return decorator

    async def fire(self, hook_name: str, layer: str, context: dict[str, Any], mem: Any | None = None, graph: Any | None = None) -> dict[str, Any]:
        """Fire every handler of ``hook_name`` and report what each one did.

        A handler that raises does NOT propagate: the exception is logged and
        reported as a result, so one broken handler cannot take the whole
        dispatch down. That makes this method the place where a failure has
        to be CLASSIFIED, because the caller cannot see the exception any
        more and can only read the result.

        The classification is the ``transient`` key of a failed result:

        * ``{"error": str(e), "transient": True}`` -- the failure is
          transient, i.e. it clears by itself. A BUSY database (another
          writer holds the lock, or this connection's read snapshot went
          stale) is the only such case: the work was not done, and doing
          it again later is the right thing to do.
        * ``{"error": str(e), "transient": False}`` -- the failure is
          permanent, i.e. a bug in the handler or a schema that does not
          match the code. Retrying it forever would stall the caller on
          one poison message.

        The boundary is the CODE, not the class: ``sqlite3.Error`` also
        covers ``ProgrammingError``, ``IntegrityError`` and a missing
        table, and none of those ever clear. ``shared.sqlite_busy`` is
        the single place that answers the question.

        The key is ALWAYS present on a failed result, so "not transient" is
        an explicit False and never a missing key. A caller that has to
        decide between "retry later" and "give up" reads this value; the
        autohooks daemon holds its cursor for a transient failure and moves
        on for a permanent one.
        """
        # Hot path optimization: check if we should skip
        # Note: In production we use config.is_hook_enabled
        try:
            from config import config

            if not config.is_hook_enabled(layer, hook_name) and not context.get("_test_bypass_config"):
                return {"skipped": True, "reason": "hook_disabled"}
        except ImportError:
            pass

        handlers = self._hooks.get(hook_name, [])
        if not handlers:
            return {"skipped": True, "reason": "no_handlers"}

        results = []
        fired_count = 0
        for h in handlers:
            if h.layer not in ("both", layer):
                continue
            fired_count += 1
            try:
                kwargs: dict[str, Any] = {}
                if h.takes_mem:
                    kwargs["mem"] = mem
                if h.takes_graph:
                    kwargs["graph"] = graph
                res = h.func(context, **kwargs) if kwargs else h.func(context)
                if h.is_async:
                    res = await res
                results.append(res)
            except Exception as e:
                logger.exception(f"Hook {hook_name} failed")
                # The marker tells the caller whether the work is worth
                # repeating. A BUSY database clears, and the message was
                # NOT handled. Everything else -- a handler bug, a missing
                # column, a broken constraint -- is permanent, and the
                # caller must not spin on it forever.
                results.append({"error": str(e), "transient": is_busy(e)})
        return {"results": results, "handler_count": fired_count}

    def list_hooks(self) -> dict[str, int]:
        return {n: len(h) for n, h in self._hooks.items()}


hook_registry = HookRegistry()
HookHandler.model_rebuild()
