import pytest
import asyncio
from hooks.registry import HookRegistry

@pytest.fixture
def registry():
    return HookRegistry()

def test_manual_registration(registry):
    from hooks.models import HookHandler
    def dummy_hook(context): pass
    handler = HookHandler(func=dummy_hook, name='test_hook', layer='user', is_async=False, takes_mem=False)
    registry.register(handler)
    assert 'test_hook' in registry._hooks

def test_mark_sync_no_mem(registry):
    class MockHooks:
        @registry.mark('sync_hook', layer='agent')
        def sync_hook(self, context): return 'ok'
    hooks = MockHooks()
    registry.register_instance(hooks)
    assert 'sync_hook' in registry._hooks
    assert registry.list_hooks()['sync_hook'] == 1

@pytest.mark.asyncio
async def test_mark_async_with_mem(registry):
    class MockHooks:
        @registry.mark('async_hook', layer='both')
        async def async_hook(self, context, mem=None): return 'async_ok'
    hooks = MockHooks()
    registry.register_instance(hooks)
    res = await registry.fire('async_hook', 'user', {'_test_bypass_config': True})
    assert res['results'][0] == 'async_ok'

def test_mark_takes_mem_detection(registry):
    class MockHooks:
        @registry.mark('mem_hook')
        def hook_with_mem(self, context, mem): pass
        @registry.mark('no_mem_hook')
        def hook_without_mem(self, context, other): pass
    hooks = MockHooks()
    registry.register_instance(hooks)
    h_mem = registry._hooks['mem_hook'][0]
    assert h_mem.takes_mem is True
    h_no = registry._hooks['no_mem_hook'][0]
    assert h_no.takes_mem is False