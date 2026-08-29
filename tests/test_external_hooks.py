"""External event dispatcher + inject block builder (spec S3/S5)."""

from __future__ import annotations

from typing import Any

import pytest

from features.inject import build_inject_blocks
from hooks.external import KNOWN_EVENTS, auto_save_text, dispatch_event


def test_known_events_exact_set() -> None:
    assert sorted(KNOWN_EVENTS) == sorted(
        {
            "session_started",
            "session_ended",
            "new_message",
            "auto_save_candidate",
            "context_threshold",
            "memory_pressure",
            "post_context_compression",
        }
    )


@pytest.mark.asyncio
async def test_dispatch_unknown_event_raises() -> None:
    with pytest.raises(ValueError, match="unknown event"):
        await dispatch_event(event="nope", layer="user", user_id="u1", payload={}, mem=None, graph=None)


@pytest.mark.asyncio
async def test_dispatch_fires_hook_by_event_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks.registry import HookRegistry

    reg = HookRegistry()
    seen: dict[str, Any] = {}

    class _Probe:
        @reg.mark("new_message", layer="user")
        async def handler(self, ctx: dict[str, Any], mem: Any = None, graph: Any = None) -> dict[str, Any]:
            seen["text"] = ctx.get("text")
            seen["mem"] = mem
            return {"score": 0.9}

    reg.register_instance(_Probe())
    monkeypatch.setattr("hooks.registry.hook_registry", reg)

    fake_mem, fake_graph = _FakeMem(), _FakeGraph()
    result = await dispatch_event(event="new_message", layer="user", user_id="u1", payload={"text": "hi"}, mem=fake_mem, graph=fake_graph)
    assert result["results"] == [{"score": 0.9}]
    assert seen["text"] == "hi" and seen["mem"] is fake_mem


class _FakeL1:
    def get_recent(self, n: int = 10) -> list[Any]:
        return []


class _FakeL3:
    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        self._store.append((user_id, summary, weight, tags))
        return 1


class _FakeMem:
    def __init__(self) -> None:
        self.l1 = _FakeL1()
        self.saved: list[tuple[str, str, float, list[str]]] = []
        self.l3 = _FakeL3(self.saved)
        self.l4 = None

    async def remember(self, key: str, value: str, importance: float) -> int:
        return 2


class _FakeGraph:
    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, str, list[str], float]] = []

    async def add_node(self, user_id: str, content: str, node_type: str, tags: list[str], importance: float) -> int:
        self.nodes.append((user_id, content, node_type, tags, importance))
        return 7


@pytest.mark.asyncio
async def test_auto_save_text_below_threshold_saves_nothing() -> None:
    mem, graph = _FakeMem(), _FakeGraph()
    result = await auto_save_text(mem, graph, "u1", "короткий")  # len<20 → 0.0
    assert result == {"score": 0.0, "saved_l3": False, "saved_l4": False, "saved_graph": False}


@pytest.mark.asyncio
async def test_auto_save_text_mid_score_saves_l3_and_graph() -> None:
    mem, graph = _FakeMem(), _FakeGraph()
    # ? 0.15 + keyword 0.2 + ?+keyword 0.1 + len>100 0.2 = 0.65 (>= 0.5, < 0.8)
    text = "какое решение по кэшу? " + "x" * 80
    result = await auto_save_text(mem, graph, "u1", text)
    assert result["saved_l3"] is True
    assert result["saved_graph"] is True
    assert result["saved_l4"] is False
    assert mem.saved and graph.nodes


@pytest.mark.asyncio
async def test_auto_save_text_high_score_saves_l4_too() -> None:
    mem, graph = _FakeMem(), _FakeGraph()
    text = "?! важно " + "решил " + "x" * 120 + "\n\n\n"  # 0.9 >= 0.8
    result = await auto_save_text(mem, graph, "u1", text)
    assert result["saved_l4"] is True


@pytest.mark.asyncio
async def test_build_inject_blocks_shapes_and_budget() -> None:
    class _FakeRag:
        async def search(self, query: str, user_id: str = "default", limit: int = 5, **kw: Any) -> list[dict[str, Any]]:
            return [{"content": "q" * 40, "score": 0.9}, {"value": "v" * 40, "score": 0.8}]

    class _FakeL4:
        async def get_all(self, user_id: str, limit: int = 50) -> list[Any]:
            from types import SimpleNamespace

            return [
                SimpleNamespace(key="k", value="v", importance=0.9),
                SimpleNamespace(key="k2", value="v2", importance=0.5),
            ]

    class _FakeMem2(_FakeMem):
        def __init__(self) -> None:
            super().__init__()
            self.l4 = _FakeL4()

    blocks = await build_inject_blocks(_FakeMem2(), _FakeRag(), "u1", text="поиск", budget=500)
    kinds = [b["kind"] for b in blocks]
    assert kinds[0] == "relevant" and len([k for k in kinds if k == "relevant"]) == 2
    assert "important" in kinds  # only the 0.9 fact
    assert all(set(b) == {"kind", "content", "score"} for b in blocks)
