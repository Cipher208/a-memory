"""B1.4: DAG summary lineage — promoted facts know their source summaries."""

import asyncio

from lifecycle.consolidation import ConsolidationEngine
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME


def _make(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))

    async def init():
        from core.episodic import EpisodicMemory
        from core.memory import CoreMemory
        from features.audit_trail import AuditTrail

        await EpisodicMemory(cm=cm, layer="user")._init_db()
        await CoreMemory(cm=cm, layer="user")._init_db()
        await AuditTrail(cm=cm)._init_db()
        conn = await cm.get(DB_NAME)
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS importance_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT, chunk_id INTEGER, source TEXT,
                old_importance REAL, new_importance REAL,
                signal_breakdown TEXT, reason TEXT, rescored_at REAL)"""
        )
        await conn.commit()

    asyncio.run(init())
    return cm


def test_episode_promotion_carries_lineage(tmp_path):
    """A fact promoted from an episode records episode:<id> in metadata."""
    cm = _make(tmp_path)

    async def t():
        from core.episodic import EpisodicMemory

        epi = EpisodicMemory(cm=cm, layer="user")
        fact_id = await epi.save("u1", "Chose PostgreSQL over MySQL for the billing service", 0.9)

        engine = ConsolidationEngine(cm=cm, layer="user")
        promoted = await engine.consolidate_episodes("u1")
        assert promoted == 1

        conn = await cm.get(DB_NAME)
        row = await (await conn.execute("SELECT metadata FROM core_memory WHERE source='episode_promotion'")).fetchone()
        assert row is not None
        assert row["metadata"] is not None
        assert f"episode:{fact_id}" in row["metadata"]

        # Reader API
        entry = await (await conn.execute("SELECT entry_id FROM core_memory WHERE source='episode_promotion'")).fetchone()
        lineage = await engine.get_lineage(int(entry[0]))
        assert lineage == [f"episode:{fact_id}"]

    asyncio.run(t())


def test_staging_promotion_records_event_parent(tmp_path):
    """Staging items with an event_id produce facts with event:<id> parents."""
    cm = _make(tmp_path)

    async def t():
        engine = ConsolidationEngine(cm=cm, layer="user")
        result = await engine.consolidate_staging(
            "u1",
            [{"content": "Deploy checklist updated", "importance": 0.8, "memory_kind": "fact", "event_id": "evt-42"}],
        )
        assert result["promoted"] == 1

        conn = await cm.get(DB_NAME)
        row = await (await conn.execute("SELECT entry_id, metadata FROM core_memory WHERE source='staging_promotion'")).fetchone()
        assert "event:evt-42" in row["metadata"]
        assert await engine.get_lineage(int(row["entry_id"])) == ["event:evt-42"]

    asyncio.run(t())


def test_lineage_absent_for_plain_facts(tmp_path):
    """Facts saved without parents return an empty lineage, not an error."""
    cm = _make(tmp_path)

    async def t():
        from core.memory import CoreMemory

        core = CoreMemory(cm=cm, layer="user")
        entry_id = await core.save("u1", "plain", "no lineage here")

        engine = ConsolidationEngine(cm=cm, layer="user")
        assert await engine.get_lineage(entry_id) == []

    asyncio.run(t())
