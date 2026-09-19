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


@pytest.mark.asyncio
async def test_find_block_keys_on_layer_user_text(cm):
    from shared.l0 import capture, find_block

    assert await find_block("user", "u1", "нет такого блока") is None

    rid = await capture("new_message", "user", "u1", "помни: wal включён")
    assert await find_block("user", "u1", "помни: wal включён") == rid
    # layer and user_id are part of the key — the same text elsewhere is a different block
    assert await find_block("agent", "u1", "помни: wal включён") is None
    assert await find_block("user", "u2", "помни: wal включён") is None


@pytest.mark.asyncio
async def test_capture_replay_returns_original_rid_without_second_row(cm):
    from shared.l0 import capture

    first = await capture("new_message", "user", "u1", "дубль сообщения")
    again = await capture("new_message", "user", "u1", "дубль сообщения")
    assert again == first

    row = await (await (await cm.get("memory.db")).execute("SELECT count(*) FROM l0_journal")).fetchone()
    assert row[0] == 1  # the replay added no row


@pytest.mark.asyncio
async def test_auto_save_skips_replay_of_captured_block(cm):
    """capture() dedups the journal, but the caller ran the distiller anyway —
    every replay re-emitted the full clause set under the same `raw:<rid>` tag
    (raw:164 → 120 rows across three days, decaying 60/40/20). The guard sits
    before capture(), so a replay costs nothing downstream."""
    from hooks.external import auto_save_text
    from shared.l0 import capture

    text = "помни: wal включён, автовакуум выключен"
    assert await capture("new_message", "user", "u1", text) is not None

    out = await auto_save_text(None, None, "u1", text)
    assert out["skipped"] == "duplicate_l0_block"
    assert out["saved_l3"] is False and out["saved_l4"] is False
