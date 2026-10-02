"""Episodic save must not store byte-identical duplicates.

Ground: 468 duplicate rows (17.5% of the episodes table) accumulated in the
live base, every pair written seconds apart with identical
(user_id, layer, summary). The table has no UNIQUE guard and the insert path
never checked, so each repeat landed as a new row.
"""

from core.episodic import EpisodicMemory
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME


def _make_cm(tmp_path):
    return AsyncConnectionManager(base_dir=str(tmp_path))


async def _count(cm, user_id):
    conn = await cm.get(DB_NAME)
    cur = await conn.execute("SELECT COUNT(*) FROM episodes WHERE user_id=?", (user_id,))
    return (await cur.fetchone())[0]


async def test_save_returns_existing_id_on_identical_summary(tmp_path):
    cm = _make_cm(tmp_path)
    epi = EpisodicMemory(cm=cm, layer="user")
    await epi._init_db()
    first = await epi.save("u1", "the same observation")
    second = await epi.save("u1", "the same observation")
    assert second == first
    assert await _count(cm, "u1") == 1


async def test_save_still_inserts_different_summaries(tmp_path):
    cm = _make_cm(tmp_path)
    epi = EpisodicMemory(cm=cm, layer="user")
    await epi._init_db()
    first = await epi.save("u1", "first observation")
    second = await epi.save("u1", "second observation")
    assert second != first
    assert await _count(cm, "u1") == 2


async def test_save_scopes_dedup_by_user(tmp_path):
    cm = _make_cm(tmp_path)
    epi = EpisodicMemory(cm=cm, layer="user")
    await epi._init_db()
    await epi.save("u1", "shared observation")
    await epi.save("u2", "shared observation")
    assert await _count(cm, "u1") == 1
    assert await _count(cm, "u2") == 1
