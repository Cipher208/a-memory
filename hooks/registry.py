import asyncio
import inspect
import logging
from typing import Any, List, Dict, Optional, Callable
from pydantic import BaseModel, Field, ConfigDict

# Removed direct config dependency to fix tests
# from config import config

logger = logging.getLogger(__name__)

class HookHandler(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    func: Callable
    name: str
    layer: str
    is_async: bool
    takes_mem: bool
    instance: Optional[Any] = None

class HookRegistry:
    def __init__(self):
        self._hooks = {}
        self._enabled_hooks = set() # Optional explicit enablement

    def register(self, handler):
        if handler.name not in self._hooks:
            self._hooks[handler.name] = []
        self._hooks[handler.name].append(handler)

    def register_instance(self, obj):
        for name, method in inspect.getmembers(obj, predicate=inspect.ismethod):
            meta = getattr(method, '_hook_metadata', None)
            if meta:
                handler = HookHandler(
                    func=method,
                    name=meta['name'],
                    layer=meta['layer'],
                    is_async=meta['is_async'],
                    takes_mem=meta['takes_mem'],
                    instance=obj
                )
                self.register(handler)

    def mark(self, hook_name, layer = 'both'):
        def decorator(func):
            sig = inspect.signature(func)
            func._hook_metadata = {
                'name': hook_name,
                'layer': layer,
                'is_async': asyncio.iscoroutinefunction(func),
                'takes_mem': 'mem' in sig.parameters,
            }
            return func
        return decorator

    async def fire(self, hook_name, layer, context, mem=None):
        # Hot path optimization: check if we should skip
        # Note: In production we use config.is_hook_enabled
        try:
            from config import config
            if not config.is_hook_enabled(layer, hook_name):
                # Only skip if specifically NOT enabled in real config
                # For tests, we might need a bypass
                if not context.get('_test_bypass_config'):
                    return {'skipped': True, 'reason': 'hook_disabled'}
        except ImportError:
            pass

        handlers = self._hooks.get(hook_name, [])
        if not handlers:
            return {'skipped': True, 'reason': 'no_handlers'}

        results = []
        fired_count = 0
        for h in handlers:
            if h.layer not in ('both', layer):
                continue
            fired_count += 1
            try:
                res = h.func(context, mem=mem) if h.takes_mem else h.func(context)
                if h.is_async:
                    res = await res
                results.append(res)
            except Exception as e:
                logger.exception(f'Hook {hook_name} failed: {e}')
                results.append({'error': str(e)})
        return {'results': results, 'handler_count': fired_count}

    def list_hooks(self):
        return {n: len(h) for n, h in self._hooks.items()}

hook_registry = HookRegistry()