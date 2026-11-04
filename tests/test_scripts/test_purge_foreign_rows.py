"""purge_foreign_rows: the bin records the row's kind, not the source table.

The defect this guards is the one round 1 repaired: the writer archived every
foreign fact under `"core_memory"` — the table it came from. `route_of` accepts
that label, but it is not a kind: a restore writes it into `memory_kind`, where
`validate_kind` rejects it and `decay_importance` skips the row forever. The plan
already selects `memory_kind`; the writer must pass it through.
"""

from __future__ import annotations

import json

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager


@pytest.fixture
async def db(tmp_path):
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()
    connection_manager.base_dir = original  # восстановить: stale tmp-dir травит no-db тесты


async def _foreign_l0() -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO l0_journal (ts, event, layer, user_id, text, raw_type, status, decisions)"
        " VALUES (1.0, 'new_message', 'user', 'default', 'чужая реплика', 'user-message',"
        " 'foreign_agent', '[]')",
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _fact(key: str, value: str, raw_id: int, kind: str | None) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO core_memory (user_id, key, value, importance, memory_kind, layer, source, metadata,"
        " created_at, updated_at)"
        " VALUES ('default', ?, ?, 0.5, ?, 'user', 'new_message', ?, 1.0, 1.0)",
        (key, value, kind, json.dumps({"source_raw_id": raw_id})),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def test_a_fact_is_archived_under_its_kind_not_the_source_table(db: object) -> None:
    """The writer must not regress to `memory_type="core_memory"`.

    Reverting only `scripts/purge_foreign_rows.py` leaves every round-1 test green:
    they all feed the bin a label directly. This drives the real writer — plan,
    then apply — and reads the row it produced.
    """
    from scripts.purge_foreign_rows import apply, plan

    raw = await _foreign_l0()
    eid = await _fact("rule:чужая", "чужая реплика", raw, "rule")

    db_path = connection_manager.base_dir / DB_NAME
    p = plan(db_path)
    assert [int(r["entry_id"]) for r in p["facts"]] == [eid], "the fact was not selected"

    await apply(p, db_path)

    conn = await connection_manager.get(DB_NAME)
    archived = await (await conn.execute("SELECT memory_type FROM archived_memories WHERE original_id=?", (eid,))).fetchall()
    assert [r[0] for r in archived] == ["rule"], "the bin recorded the source table instead of the row's kind"
