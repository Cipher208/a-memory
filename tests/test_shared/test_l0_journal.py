import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


@pytest.mark.asyncio
async def test_capture_writes_row_and_classifies(cm, tmp_path):
    from shared.l0 import capture, classify_raw

    assert classify_raw('[{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]') == "tool_result"
    assert classify_raw('{"type": "tool_use", "name": "f", "input": {}}') == "tool_use"
    assert classify_raw("[ariel recall]\n- [session] x") == "recall"
    assert classify_raw("[EVOLUTION] sweep done") == "evolution"
    assert classify_raw("обычное сообщение про проект") == "user-message"
    rid = await capture("new_message", "user", "u1", "помни: я решил перейти на wal")
    assert rid is not None
    row = await (await (await cm.get("memory.db")).execute("SELECT raw_type, status FROM l0_journal WHERE id=?", (rid,))).fetchone()
    assert row[0] == "user-message" and row[1] == "received"


@pytest.mark.asyncio
async def test_capture_never_raises(cm):
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", "x", raw_type=None)
    assert rid is not None  # даже с None raw_type — классифицирует сам
