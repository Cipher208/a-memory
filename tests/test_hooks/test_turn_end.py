"""E14: on_turn_end event — turn-level capture with sync result."""

import pytest

import hooks.user_hooks  # noqa: F401 — registration metadata
from hooks.external import dispatch_event
from hooks.registry import hook_registry
from hooks.user_hooks import UserHooks


@pytest.fixture()
def registered_hooks():
    hook_registry.register_instance(UserHooks())
    yield
    hook_registry._hooks.pop("on_turn_end", None)  # keep other tests' registries clean


def test_on_turn_end_in_known_events():
    from hooks.external import KNOWN_EVENTS

    assert "on_turn_end" in KNOWN_EVENTS


@pytest.mark.asyncio
async def test_turn_end_returns_sync_result(registered_hooks):
    """The handler result comes back to the caller in the same await."""

    class _L1:
        def get_recent(self, n):
            return []

    class _Mem:
        def __init__(self):
            self.l1 = _L1()
            self.saved = []

        async def remember(self, key, value, importance):
            self.saved.append((key, value, importance))
            return 1

        async def recall(self, query, limit=10):
            return []

    mem = _Mem()
    result = await dispatch_event(
        "on_turn_end",
        "user",
        "u1",
        {"text": "D" * 200},  # long text crosses the auto-save threshold
        mem,
        object(),  # graph required by _new_message (None → skip branch)
        None,
    )
    assert result["handler_count"] == 1
    res = result["results"][0]
    assert res["auto_save"]["score"] > 0  # pipeline ran and reports its score
