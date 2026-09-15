"""L3→L4 promotion window must drain forward, never pin (2026-09-15 audit).

Ground regression: consolidate_episodes selects the newest 10 high-weight
episodes per run (ORDER BY created_at DESC LIMIT 10) but neither promoted
nor permanently-unpromotable (transcript-shaped) episodes ever leave the
candidate set — so the same 10 rows are re-scanned every hour forever and
older promotable episodes are stranded. Live evidence: 300 promotable
episodes in the hermes base vs 14 promotions, frozen since 2026-09-13.

Contract: every episode the sweep looks at is marked as processed, so the
window advances and the whole backlog drains.
"""

import asyncio
import json

from core.episodic import EpisodicMemory
from lifecycle.consolidation import ConsolidationEngine
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME

# Passes every gate: fact-class, not dialogic, not broadcast, keys cleanly.
PROMOTABLE = "Chose PostgreSQL over MySQL for the billing service"
# Transcript-shaped dump: must never reach L4, but must still drain the window.
TRANSCRIPT = '[{"type": "text", "text": "*reading the plan*"}'


def _make_cm(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))

    async def init():
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


def _seed(tmp_path, n_promotable: int, n_transcripts: int):
    """Insert promotable episodes at ts 1000+, transcript junk newer (ts 1M+)."""
    cm = _make_cm(tmp_path)

    async def seed():
        epi = EpisodicMemory(cm=cm, layer="user")
        for i in range(n_promotable):
            eid = await epi.save("u1", f"{PROMOTABLE} #{i}", 0.9)
            conn = await cm.get(DB_NAME)
            await conn.execute(
                "UPDATE episodes SET created_at = ? WHERE episode_id = ?",
                (1_000_000.0 + i, eid),
            )
            await conn.commit()
        for i in range(n_transcripts):
            eid = await epi.save("u1", TRANSCRIPT.replace("*reading*", f"*r{i}*"), 0.9)
            conn = await cm.get(DB_NAME)
            await conn.execute(
                "UPDATE episodes SET created_at = ? WHERE episode_id = ?",
                (2_000_000.0 + i, eid),
            )
            await conn.commit()

    asyncio.run(seed())
    return cm


def _tags_for(cm, episode_id: int) -> list[str]:
    async def get():
        conn = await cm.get(DB_NAME)
        row = await (await conn.execute("SELECT tags FROM episodes WHERE episode_id=?", (episode_id,))).fetchone()
        return json.loads(row["tags"] or "[]")

    return asyncio.run(get())


def test_sweep_advances_past_newer_junk(tmp_path):
    """5 promotable episodes hidden behind 12 newer transcript episodes must
    reach L4 within two sweeps; today's code returns 0 forever."""
    cm = _seed(tmp_path, n_promotable=5, n_transcripts=12)

    async def run():
        engine = ConsolidationEngine(cm=cm, layer="user")
        first = await engine.consolidate_episodes("u1")
        second = await engine.consolidate_episodes("u1")
        return first + second

    assert asyncio.run(run()) == 5


def test_processed_episodes_never_rescanned(tmp_path):
    """After the backlog drains, a further sweep promotes nothing and every
    scanned episode carries the processed marker."""
    cm = _seed(tmp_path, n_promotable=3, n_transcripts=14)
    engine_cm = cm

    async def run():
        engine = ConsolidationEngine(cm=engine_cm, layer="user")
        total = 0
        for _ in range(3):  # 3 sweeps ≥ ceil(17/10) — backlog must be empty
            total += await engine.consolidate_episodes("u1")
        conn = await engine_cm.get(DB_NAME)
        left = await (await conn.execute("SELECT COUNT(*) FROM episodes WHERE tags IS NULL OR tags NOT LIKE '%l4:seen%'")).fetchone()
        # 3rd sweep sees nothing new: no duplicate L4 rows and 0 promotions
        dupes = await (await conn.execute("SELECT COUNT(*) FROM (SELECT key FROM core_memory GROUP BY key HAVING COUNT(*)>1)")).fetchone()
        return total, int(left[0]), int(dupes[0])

    total, unprocessed, dupes = asyncio.run(run())
    assert total == 3
    assert unprocessed == 0
    assert dupes == 0
    # transcripts were scanned and marked, not promoted
    first_transcript_tags = _tags_for(cm, 4)
    assert "l4:seen" in first_transcript_tags
