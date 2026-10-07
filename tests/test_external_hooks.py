"""External event dispatcher + inject block builder (spec S3/S5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from features.inject import build_inject_blocks
from hooks.external import KNOWN_EVENTS, auto_save_text, dispatch_event

if TYPE_CHECKING:
    from pathlib import Path


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
            "post_session_diff",  # C1.10
            "on_turn_end",  # E14
            "state_entered",  # MUSE §7.5
            "state_exited",  # MUSE §7.5
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

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str], created_at: float | None = None) -> int:
        self._store.append((user_id, summary, weight, tags))
        return 1


class _FakeMem:
    def __init__(self) -> None:
        self.l1 = _FakeL1()
        self.saved: list[Any] = []
        self.l3 = _FakeL3(self.saved)
        self.l4 = None
        self.canon_calls: list[dict[str, Any]] = []

    async def remember(self, key: str, value: str, importance: float, **kw: Any) -> int:
        self.canon_calls.append({"key": key, "kw": kw})
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
    # The whole dict is still pinned: nothing reached either layer. `gated` names
    # WHICH guard refused, and exists because the refusal used to be visible only
    # as an early return — the L0 row it left behind stayed `received` forever.
    assert result == {
        "score": 0.0,
        "saved_l3": False,
        "saved_l4": False,
        "saved_graph": False,
        "gated": "importance_gate",
    }


@pytest.mark.asyncio
async def test_auto_save_text_mid_score_saves_l3_and_graph() -> None:
    mem, graph = _FakeMem(), _FakeGraph()
    # ? 0.15 + keyword 0.2 + ?+keyword 0.1 + len>100 0.2 = 0.65 (>= 0.5, < 0.8)
    text = "какое решение по кэшу? " + "x" * 80
    result = await auto_save_text(mem, graph, "u1", text)
    assert result["saved_l3"] is True
    assert result["saved_graph"] is True
    assert result["saved_l4"] is False
    # F-G1: граф больше не пишется напрямую из auto_save — наполняют минеры
    assert mem.saved and not graph.nodes


@pytest.mark.asyncio
async def test_auto_save_text_high_score_saves_l4_via_distiller() -> None:
    """Score ≥ 0.8: L4 routing is the distiller's job (canonical keys).
    The 2026-09-11 removal killed the raw-text staging branch — no proposals,
    no "auto_save" core-key overwrites."""
    mem, graph = _FakeMem(), _FakeGraph()
    text = "?! важно " + "решил " + "x" * 120 + "\n\n\n"  # 0.9 >= 0.8
    result = await auto_save_text(mem, graph, "u1", text)
    assert not result.get("staged_l4"), "auto-save staging branch removed"
    assert result["saved_l4"] is True, "distiller L4 routing still fires"
    assert mem.saved == [], "no raw auto_save core-key writes"


@pytest.mark.asyncio
async def test_auto_save_persona_owner_declared_lands_l4() -> None:
    """S10+S4: persona_owner + declarable kind skips G1/G2 straight to L4."""
    from shared import canon_rate as _cr

    _cr._canon_ts.clear()
    mem, graph = _FakeMem(), _FakeGraph()
    text = "Госпожа закрепила в SOUL: я — Госпожа, девочка — моя, форма обращения закреплена"
    res = await auto_save_text(mem, graph, "p1", text, role="assistant", persona_owner=True, kind="preference")
    assert res["saved_l4"] is True
    assert res["canon"] == "persona_owner"
    assert mem.canon_calls and mem.canon_calls[0]["kw"].get("memory_kind") == "preference"  # kind persists
    _cr._canon_ts.clear()


@pytest.mark.asyncio
async def test_auto_save_persona_owner_undeclared_stays_heuristic() -> None:
    mem, graph = _FakeMem(), _FakeGraph()
    text = "какое решение по кэшу? " + "x" * 80
    res = await auto_save_text(mem, graph, "p2", text, role="assistant", persona_owner=True, kind="")
    assert res["saved_l4"] is False
    assert res["saved_l3"] is True
    assert "canon" not in res


@pytest.mark.asyncio
async def test_new_message_forwards_speaker_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """S10: dispatch payload fields reach auto_save_text via the hook."""
    import hooks.external as ext

    seen: dict[str, Any] = {}

    async def fake_auto_save(mem: Any, graph: Any, user_id: str, text: str, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        seen["text"] = text
        return {"score": 0.0, "saved_l3": False, "saved_l4": False, "saved_graph": False}

    monkeypatch.setattr(ext, "auto_save_text", fake_auto_save)
    from hooks.user_hooks import UserHooks

    res = await UserHooks("u9")._new_message(
        {"text": "длинный канонический текст про протокол обращения", "role": "assistant", "persona_owner": True, "kind": "preference"},
        mem=object(),
        graph=object(),
    )
    assert res["auto_save"]["score"] == 0.0
    assert seen["role"] == "assistant" and seen["persona_owner"] is True and seen["kind"] == "preference"


def test_persona_assistant_dispatch_layer_is_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S10: __main__ routes assistant-role persona dispatches to the agent layer."""
    monkeypatch.setenv("HOME", str(tmp_path))
    yaml_body = """data_dir: ~/.d
user_id: u
layer: user
persona_owner: true
source:
  driver: sqlite
  path: ~/.s.db
  table: m
  cursor_column: id
  order_by: id
  role: {column: r}
  text: {column: t}
"""
    cfg = _write_yaml(tmp_path, yaml_body)
    from autohooks.__main__ import _dispatch_layer
    from autohooks.config import load_config as lc

    c = lc(cfg)
    assert _dispatch_layer(c, {"role": "assistant"}) == "agent"
    assert _dispatch_layer(c, {"role": "user"}) == "user"
    c2 = lc(_write_yaml(tmp_path, yaml_body.replace("persona_owner: true", "persona_owner: false"), name="b.yaml"))
    assert _dispatch_layer(c2, {"role": "assistant"}) == "user"


@pytest.mark.asyncio
async def test_persona_declaration_reaches_a_handler_on_the_agent_layer() -> None:
    """S10 regression: the dispatch routed to the agent layer had NO handler there.

    `dispatch_layer` sends an assistant-role persona_owner message to the `agent`
    layer so the persona's own words stop piling into the user's fact heap. That
    routing was tested (`test_persona_assistant_dispatch_layer_is_agent`) and it
    worked — but the destination was empty: `new_message` was marked
    `layer="user"` only, and `HookRegistry.fire` filters by layer with no
    fallback, so the event arrived, matched nothing, and returned
    `handler_count: 0`.

    Every existing test called `auto_save_text` directly, which is why nothing
    caught it: the pipeline was verified while the door into it was shut. The
    persona-canon branch (`persona_owner and kind and role == "assistant"`) was
    unreachable from the CLI and daemon paths and looked alive.

    So this test fires through the REGISTRY, at the layer the dispatcher chooses,
    which is the only place the bug was visible.
    """
    from hooks.registry import HookRegistry
    from hooks.user_hooks import UserHooks

    reg = HookRegistry()
    reg.register_instance(UserHooks("u1"))

    fired: dict[str, Any] = {}

    async def _fake_auto_save(mem: Any, graph: Any, user_id: str, text: str, **kw: Any) -> dict[str, Any]:
        fired.update(kw)
        fired["text"] = text
        return {"score": 0.0, "saved_l3": False, "saved_l4": False, "saved_graph": False}

    import hooks.external as ext

    original = ext.auto_save_text
    ext.auto_save_text = _fake_auto_save
    try:
        result = await reg.fire(
            "new_message",
            "agent",
            {"text": "каноническое решение персоны", "role": "assistant", "persona_owner": True, "kind": "preference"},
            mem=object(),
            graph=object(),
        )
    finally:
        ext.auto_save_text = original

    assert result["handler_count"] == 1, "the agent layer must have a new_message handler"
    assert fired["role"] == "assistant" and fired["persona_owner"] is True


@pytest.mark.asyncio
async def test_persona_declaration_uses_the_agent_l0_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The speaker axis has to reach the journal, because `layer` is in its hash.

    `shared/l0.py:_content_hash` hashes `layer|user_id|text`, and the replay
    guard asks the same question through `find_block`. With the layer hard-coded
    to "user", a persona's declaration that repeated anything the owner had
    already said hashed identically to the owner's block — so `find_block`
    answered "already captured" and the declaration was dropped as
    `duplicate_l0_block` BEFORE the canon branch could promote it. Her canon was
    thrown away for agreeing with the woman she was agreeing with.
    """
    import hooks.external as ext
    import shared.l0 as l0

    captured: list[tuple[str, str]] = []
    looked_up: list[str] = []

    async def _fake_find_block(layer: str, user_id: str, text: str) -> int | None:
        looked_up.append(layer)
        return None

    async def _fake_capture(event: str, layer: str, user_id: str, text: str, **kw: Any) -> int:
        captured.append((layer, text))
        return 42

    monkeypatch.setattr(l0, "find_block", _fake_find_block)
    monkeypatch.setattr(l0, "capture", _fake_capture)

    text = "дом держит двенадцать инструментов"
    await ext.auto_save_text(_FakeMem(), _FakeGraph(), "u1", text, role="assistant", persona_owner=True, kind="preference")
    await ext.auto_save_text(_FakeMem(), _FakeGraph(), "u1", text, role="user")

    assert captured[0][0] == "agent", "her own declaration is journalled on the agent layer"
    assert captured[1][0] == "user", "the owner's words stay on the user layer"
    assert captured[0][1] == captured[1][1], "same text, and the layers are what tell them apart"
    assert looked_up[0] == "agent", "the replay guard asks the same question the hash does"


def _write_yaml(tmp_path: Path, body: str, name: str = "a.yaml") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


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
    # E9 ordering: stable kinds first, then the cache:break marker, then dynamic
    assert kinds[0] == "important" and kinds[1] == "cache_break"
    assert len([k for k in kinds if k == "relevant"]) == 2  # only the 0.9 fact survives the L4 filter
    assert all(set(b) == {"kind", "content", "score"} for b in blocks)
