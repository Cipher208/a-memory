"""B1.5: memory transition state machine + telemetry."""

import asyncio

from lifecycle.transitions import VALID_TRANSITIONS, record_transition
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME


def test_transition_map_covers_real_paths():
    """The map must contain every transition the codebase actually performs."""
    assert "l4" in VALID_TRANSITIONS["episode"]
    assert "l4" in VALID_TRANSITIONS["staging"]
    assert "archived" in VALID_TRANSITIONS["l4"]
    assert "archived" in VALID_TRANSITIONS["episode"]
    # No transitions out of terminal states
    assert not VALID_TRANSITIONS.get("archived")


def test_record_and_list(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))

    async def t():
        n = await record_transition(cm, "u1", "episode", "episode:7", "l4", "core:42", "consolidation")
        assert n == 1
        n = await record_transition(cm, "u1", "l4", "core:42", "archived", "archived:9", "age+importance")
        assert n == 1

        conn = await cm.get(DB_NAME)
        rows = await (await conn.execute("SELECT kind, from_ref, to_ref, reason FROM memory_transitions ORDER BY id")).fetchall()
        assert len(rows) == 2
        assert (rows[0]["kind"], rows[0]["from_ref"], rows[0]["to_ref"]) == ("episode->l4", "episode:7", "core:42")
        assert rows[1]["reason"] == "age+importance"

    asyncio.run(t())


def test_invalid_transition_rejected_but_logged(tmp_path, caplog):
    """Invalid transitions never raise — they log and skip (live paths must not break)."""
    cm = AsyncConnectionManager(base_dir=str(tmp_path))

    async def t():
        n = await record_transition(cm, "u1", "l4", "archived:1", "l4", "core:2", "resurrection?")
        assert n == 0

    asyncio.run(t())
