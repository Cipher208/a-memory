import pytest
from hooks.registry import HookRegistry


@pytest.fixture
def registry():
    return HookRegistry()


@pytest.mark.asyncio
async def test_fire_mixed_handlers(registry):
    results = []

    class Mock:
        @registry.mark("m", layer="user")
        def h1(self, ctx):
            results.append("s")

        @registry.mark("m", layer="user")
        async def h2(self, ctx):
            results.append("a")

    registry.register_instance(Mock())
    await registry.fire("m", "user", {"_test_bypass_config": True})
    assert "s" in results and "a" in results


@pytest.mark.asyncio
async def test_fire_fail_safe(registry):
    class Mock:
        @registry.mark("m")
        def h1(self, ctx):
            raise ValueError("fail")

        @registry.mark("m")
        def h2(self, ctx):
            return "ok"

    registry.register_instance(Mock())
    res = await registry.fire("m", "user", {"_test_bypass_config": True})
    assert res["handler_count"] == 2
    assert "ok" in res["results"]


@pytest.mark.asyncio
async def test_fire_instance_binding(registry):
    class Mock:
        def __init__(self):
            self.val = "bound"

        @registry.mark("test")
        def h(self, ctx):
            return self.val

    registry.register_instance(Mock())
    res = await registry.fire("test", "user", {"_test_bypass_config": True})
    assert res["results"][0] == "bound"
