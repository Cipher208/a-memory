"""Task 7 (Phase F): session-close extraction + L2 enrichment + E6 lessons."""

from types import SimpleNamespace
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path, monkeypatch) -> Any:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # патчим base_dir, не подменяем объект
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


# фейковые тексты без капсов-имён (sanitize-независимо: extract не зовёт G0,
# но держим конвенцию) — по одному на паттерн A5
_TEXTS = [
    "предпочитаю postgres для новых проектов",
    "оказалось что wal режим работает быстрее",
    "провалилось обновление схемы, больше не делать это в пятницу",
]


async def _proposal_count() -> int:
    conn = await connection_manager.get("memory.db")
    row = await (await conn.execute("SELECT COUNT(*) FROM mutation_proposals WHERE source='session_close'")).fetchone()
    return int(row[0])


@pytest.mark.asyncio
async def test_extract_and_stage_three_patterns(cm: Any) -> None:
    from features.session_close import extract_and_stage

    res = await extract_and_stage(None, "u1", _TEXTS)
    assert res["staged"] == 3
    assert res["patterns"] == {"preference": 1, "experience": 1, "lesson": 1}
    assert await _proposal_count() == 3
    # E6: lesson — kind='fact' + теги lesson/error_pattern в payload
    conn = await cm.get("memory.db")
    rows = await (await conn.execute("SELECT payload FROM mutation_proposals WHERE source='session_close'")).fetchall()
    lesson = [r[0] for r in rows if '"key": "lesson:' in r[0]]
    assert lesson and "error_pattern" in lesson[0] and "lesson" in lesson[0]


@pytest.mark.asyncio
async def test_session_ended_wires_extraction(cm: Any) -> None:
    from hooks.user_hooks import UserHooks

    res = await UserHooks()._session_ended({"user_id": "u1", "session_texts": _TEXTS}, mem=SimpleNamespace(l1=None))
    assert res["extracted"]["staged"] == 3
    assert await _proposal_count() == 3


@pytest.mark.asyncio
async def test_enrich_rebuilds_summary_from_l0(cm: Any) -> None:
    from features.l2_enrich import enrich_sessions
    from shared.l0 import capture

    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO sessions (session_id, user_id, summary, started_at) VALUES (?, ?, ?, ?)",
        ("sess_t7", "u1", "старое резюме", 1.0),
    )
    await conn.commit()
    assert await capture("new_message", "user", "u1", "обсуждали postgres и wal режим") is not None
    assert await capture("new_message", "user", "u1", "настроили backup скрипт") is not None

    res = await enrich_sessions(days=1)
    assert res["sessions_updated"] == 1 and res["l0_bound"] == 2
    row = await (await conn.execute("SELECT summary FROM sessions WHERE session_id='sess_t7'")).fetchone()
    assert "postgres" in row[0] and "backup" in row[0]
    assert "старое резюме" not in row[0]
