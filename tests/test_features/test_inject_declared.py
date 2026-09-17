"""Declared normative kinds must surface in the inject important block (D1).

Locks the architecture: kind-default importance (rule 0.85, instruction 0.9)
passes the inject gate (important_min 0.8) while auto-scored content stays
below it (P8.2 normative/descriptive split). If this fails, the D1 premise
(declaration path works) is wrong — do not "fix" the test, reinvestigate.
"""

import asyncio
import sqlite3

import pytest

from shared.connection import connection_manager


@pytest.fixture()
def hermetic_core(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    yield tmp_path
    connection_manager._conns.clear()


def _stored_importance(db_path, key):
    conn = sqlite3.connect(db_path / "memory.db")
    row = conn.execute("SELECT importance FROM core_memory WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_declared_rule_gets_kind_default_importance(hermetic_core):
    from core.memory import CoreMemory

    cm = CoreMemory(cm=connection_manager, layer="user")
    await cm.save("u1", "rule:speak-first-test", "See noise, speak first.", importance=None, memory_kind="rule")
    assert _stored_importance(hermetic_core, "rule:speak-first-test") == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_important_block_picks_declared_rule(hermetic_core):
    from core.memory import CoreMemory
    from features.inject import build_inject_blocks
    from types import SimpleNamespace

    cm = CoreMemory(cm=connection_manager, layer="user")
    await cm.save("u1", "rule:speak-first-test", "See noise, speak first.", importance=None, memory_kind="rule")
    facts = await cm.get_all("u1", 50)
    mem = SimpleNamespace(
        l1=SimpleNamespace(get_recent=lambda n: []),
        l3=SimpleNamespace(search_by_tag=_no_hits),
        l4=SimpleNamespace(get_all=_facts_cb(facts), get_pinned=_no_pinned),
    )
    blocks = await build_inject_blocks(mem, None, "u1", budget=2000)
    assert any(b["kind"] == "important" and "speak-first-test" in b["content"] for b in blocks)


async def _no_hits(user_id, tag, limit=10):
    return []


def _facts_cb(facts):
    async def _get_all(user_id, limit):
        return facts

    return _get_all


async def _no_pinned(user_id, limit):
    return []
