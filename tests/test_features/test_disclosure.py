"""E11: disclosure triggers — CRUD, evaluation, recall/inject integration."""

import asyncio
from types import SimpleNamespace

import pytest

from features import disclosure
from shared.connection import connection_manager


@pytest.fixture()
def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    yield tmp_path
    connection_manager._conns.clear()


async def test_crud_roundtrip(hermetic_base):
    rid = disclosure.add_rule("u1", "db rule", ["postgresql", "postgres"], "Check pg_tuning note")
    assert rid > 0
    rules = disclosure.list_rules("u1")
    assert len(rules) == 1 and rules[0]["name"] == "db rule"
    assert rules[0]["trigger_keywords"] == ["postgresql", "postgres"]
    assert disclosure.set_enabled("u1", rid, False)
    assert disclosure.list_rules("u1")[0]["enabled"] is False
    assert disclosure.delete_rule("u1", rid)
    assert disclosure.list_rules("u1") == []


def test_add_validation(hermetic_base):
    with pytest.raises(ValueError, match="required"):
        disclosure.add_rule("u1", "", ["kw"], "content")
    with pytest.raises(ValueError, match="keyword"):
        disclosure.add_rule("u1", "n", [], "content")


async def test_evaluate_hits_and_misses(hermetic_base):
    disclosure.add_rule("u1", "pg", ["postgresql"], "pg content")
    disclosure.add_rule("u1", "rust", ["rustc"], "rust content")
    disclosure.add_rule("u1", "off", ["disabled_kw"], "nope")
    rules = disclosure.list_rules("u1")
    off_id = next(r["id"] for r in rules if r["name"] == "off")
    disclosure.set_enabled("u1", off_id, False)

    hits = disclosure.evaluate_disclosures("u1", "how to tune POSTGRESQL buffer cache")
    assert [h["name"] for h in hits] == ["pg"]
    assert disclosure.evaluate_disclosures("u1", "nothing relevant here") == []
    assert disclosure.evaluate_disclosures("u1", "") == []


async def test_recall_protocol_surfaces_triggered(hermetic_base):
    disclosure.add_rule("u1", "pg rule", ["postgresql"], "pg wisdom")
    from features.recall import recall_protocol

    class _L1:
        def get_recent(self, n):
            return []

    class _L3:
        async def search_by_tag(self, user_id, tag, limit):
            return []

    class _L4:
        async def get_all(self, user_id, limit):
            return []

    mem = SimpleNamespace(l1=_L1(), l3=_L3(), l4=_L4())
    blocks = await recall_protocol(mem, None, "u1", query="postgresql tuning", budget=2000)
    triggered = [b for b in blocks if b["axis"] == "triggered"]
    assert triggered and "pg rule" in triggered[0]["content"]


async def test_inject_triggered_block_is_dynamic(hermetic_base):
    disclosure.add_rule("u1", "pg rule", ["postgresql"], "pg wisdom")
    import time as _time

    from features.inject import build_inject_blocks

    mem = SimpleNamespace(
        l1=SimpleNamespace(get_recent=lambda n: [SimpleNamespace(role="user", content="postgresql question", timestamp=_time.time())]),
        l3=SimpleNamespace(search_by_tag=lambda *a, **k: []),
        l4=SimpleNamespace(get_all=async_dummy([])),
    )
    blocks = await build_inject_blocks(mem, rag=None, user_id="u1", text="postgresql question", budget=2000)
    kinds = [b["kind"] for b in blocks]
    assert "triggered" in kinds
    ci = kinds.index("cache_break") if "cache_break" in kinds else len(kinds)
    assert kinds.index("triggered") > ci or "cache_break" not in kinds  # dynamic side


def async_dummy(return_value):
    async def _fn(*args, **kwargs):
        return return_value

    return _fn


def test_tool_count_65():
    from mcp_server.tools_layer import _register_tools

    assert len(_register_tools) == 65
