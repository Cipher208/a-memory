"""E15: memory_kind weights in ACT-R core scoring — working kinds outrank facts."""

import asyncio

import pytest

from rag.multi_source import MultiSourceRAG, _kind_weight
from shared.connection import connection_manager


@pytest.fixture()
def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    yield tmp_path
    connection_manager._conns.clear()


def test_kind_weight_table():
    assert _kind_weight("instruction") == 1.1
    assert _kind_weight("rule") == 1.1
    assert _kind_weight("commitment") == 1.1
    assert _kind_weight("fact") == 1.0
    assert _kind_weight(None) == 1.0
    assert _kind_weight("unknown_kind") == 1.0


def test_kind_weight_config_override(hermetic_base, monkeypatch):
    from config import config

    monkeypatch.setattr(config, "_data", {"retrieval": {"kind_weights": {"fact": 2.0}}}, raising=False)
    try:
        assert _kind_weight("fact") == 2.0
        assert _kind_weight("instruction") == 1.1  # defaults merge under overrides
    finally:
        monkeypatch.undo()


async def test_scoring_applies_kind_weight(hermetic_base):
    """Same importance/recency, different kind → instruction outranks fact."""
    from core.memory import CoreMemory

    core = CoreMemory(cm=connection_manager)
    await core.save("u1", "kw_do_x", "how to do x properly", importance=0.5, memory_kind="instruction")
    await core.save("u1", "kw_fact_x", "how to do x properly", importance=0.5, memory_kind="fact")

    rag = MultiSourceRAG(rag=None, wiki=None, cm=connection_manager)
    hits = await rag.search("do x properly", user_id="u1", limit=10)
    by_title = {h["title"]: h["score"] for h in hits if h["source"] == "core"}
    assert by_title["kw_do_x"] > by_title["kw_fact_x"]
    assert by_title["kw_do_x"] == pytest.approx(by_title["kw_fact_x"] * 1.1, rel=1e-6)
