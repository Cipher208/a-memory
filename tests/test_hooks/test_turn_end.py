"""E14: on_turn_end event — turn-level capture with sync result."""

import pytest

import hooks.user_hooks  # noqa: F401 — registration side effect
from hooks.external import dispatch_event


def test_on_turn_end_in_known_events():
    from hooks.external import KNOWN_EVENTS

    assert "on_turn_end" in KNOWN_EVENTS


@pytest.mark.asyncio
async def test_turn_end_returns_sync_result():
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
        None,
        None,
    )
    assert result["handler_count"] == 1
    res = result["results"][0]
    assert res["score"] > 0  # pipeline ran and reports its score
