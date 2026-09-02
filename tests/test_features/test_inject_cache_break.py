"""E9: stable prefix before <cache:break>, dynamic after; marker is free."""

import time
from types import SimpleNamespace


async def _empty_async(*args, **kwargs):
    return []


def _mem():
    class _L1:
        def get_recent(self, n):
            return [SimpleNamespace(role="user", content="recent chatter", timestamp=time.time())]

    class _L4:
        async def get_all(self, user_id, limit):
            return [SimpleNamespace(key="k1", value="stable fact one", importance=0.9)]

    class _L3:
        async def search_by_tag(self, user_id, tag, limit):
            return []

    return SimpleNamespace(l1=_L1(), l3=_L3(), l4=_L4())


async def test_marker_between_stable_and_dynamic(monkeypatch):
    import features.inject as inj

    monkeypatch.setattr(inj, "_pending_proposals", _empty_async)
    blocks = await inj.build_inject_blocks(_mem(), rag=None, user_id="u", text="", budget=2000)
    kinds = [b["kind"] for b in blocks]
    assert "cache_break" in kinds
    ci = kinds.index("cache_break")
    assert set(kinds[:ci]) <= {"rehydrate", "important"}, "stable kinds must precede the marker"
    assert not (set(kinds[ci + 1 :]) & {"rehydrate", "important"}), "no stable kinds after the marker"


async def test_marker_never_eats_budget(monkeypatch):
    # tight budget: the important block doesn't fit, the L1 dynamic does —
    # one-sided result, so no marker by contract (marker needs both sides)
    import features.inject as inj

    async def _no_facts(user_id, limit):
        return []

    monkeypatch.setattr(inj, "_pending_proposals", _empty_async)
    mem = _mem()
    mem.l4.get_all = _no_facts
    blocks = await inj.build_inject_blocks(mem, rag=None, user_id="u", text="", budget=10000)
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["recent"], "dynamic-only output stays unmarked"

    # with both sides present the marker appears without consuming budget —
    # it is inserted during reorder, after all budget accounting
    blocks = await inj.build_inject_blocks(_mem(), rag=None, user_id="u", text="", budget=10000)
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["important", "cache_break", "recent"]


async def test_no_marker_without_both_sides(monkeypatch):
    """Only dynamic blocks (no important/rehydrate) → no marker, plain list."""
    import features.inject as inj

    async def _no_facts(user_id, limit):
        return []

    monkeypatch.setattr(inj, "_pending_proposals", _empty_async)
    mem = _mem()
    mem.l4.get_all = _no_facts
    blocks = await inj.build_inject_blocks(mem, rag=None, user_id="u", text="", budget=2000)
    assert not any(b["kind"] == "cache_break" for b in blocks)


def test_render_md_marker_is_bare_line():
    from autohooks.inject import _render_md

    out = _render_md(
        [
            {"kind": "important", "content": "fact", "score": 0.9},
            {"kind": "cache_break", "content": "<cache:break>", "score": 0.0},
            {"kind": "recent", "content": "chatter", "score": 0.0},
        ]
    )
    lines = out.splitlines()
    assert lines[1] == "<cache:break>", "marker must be a bare line, not a bullet"


def test_json_fmt_carries_marker_block():
    """json fmt passes the marker through as a block (harness can parse it)."""
    import json as _json

    from autohooks.inject import _collect_blocks

    result = {"results": [{"blocks": [{"kind": "important", "content": "f", "score": 0.9}, {"kind": "cache_break", "content": "<cache:break>", "score": 0.0}]}]}
    blocks = _collect_blocks(result)
    assert [b["kind"] for b in blocks] == ["important", "cache_break"]
    assert _json.dumps(blocks)  # json-serializable
