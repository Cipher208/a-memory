"""HC2 (Phase H closeout): L0 hash-chain tamper-evidence + ts_override.

- capture(ts_override=...) → l0_journal.ts == orig_ts (не now) — import
  preserves original timestamps from chat exports.
- Каждый capture продолжает hash-chain: hash_self = sha256(hash_prev|raw_type|ts|text)[:16].
- verify_chain() пересчитывает chain по всем записям → битые записи
  (tamper-evidence: подмена text/hash детектируется).
"""

import json
from datetime import datetime
from itertools import pairwise
from typing import Any

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


@pytest.fixture
async def import_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    yield tmp_path
    connection_manager._conns.clear()


async def test_capture_ts_override(cm) -> None:
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", "помни: ts фиксирован", ts_override=1.0)
    assert rid is not None
    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT ts FROM l0_journal WHERE id=?", (rid,))).fetchone()
    assert row[0] == 1.0
    # без override — now
    rid2 = await capture("new_message", "user", "u1", "обычная запись")
    row2 = await (await conn.execute("SELECT ts FROM l0_journal WHERE id=?", (rid2,))).fetchone()
    assert abs(row2[0] - datetime.now().timestamp()) < 60


async def test_hash_chain_consistent(cm) -> None:
    from shared.l0 import capture, verify_chain

    for i in range(5):
        rid = await capture("new_message", "user", "u1", f"сообщение {i}", ts_override=100.0 + i)
        assert rid is not None

    conn = await cm.get("memory.db")
    rows = list(await (await conn.execute("SELECT id, hash_prev, hash_self FROM l0_journal ORDER BY id")).fetchall())
    assert rows[0][1] == ""  # первая запись — пустой hash_prev
    for prev, cur in pairwise(rows):
        assert cur[1] == prev[2], "hash_prev каждой записи = hash_self предыдущей"

    assert await verify_chain() == []  # битых 0


async def test_hash_chain_tamper_detected(cm) -> None:
    from shared.l0 import capture, verify_chain

    for i in range(3):
        await capture("new_message", "user", "u1", f"сообщение {i}", ts_override=100.0 + i)
    conn = await cm.get("memory.db")
    ids = [r[0] for r in await (await conn.execute("SELECT id FROM l0_journal ORDER BY id")).fetchall()]

    await conn.execute("UPDATE l0_journal SET text='подменённый текст' WHERE id=?", (ids[1],))
    await conn.commit()
    broken = await verify_chain()
    assert [b["id"] for b in broken] == [ids[1]]


async def test_import_preserves_orig_ts(import_db, tmp_path) -> None:
    from scripts.import_chat import import_records

    p = tmp_path / "claude-conversations.json"
    p.write_text(
        json.dumps(
            [
                {
                    "uuid": "c1",
                    "name": "conv",
                    "messages": [
                        {"role": "user", "content": "помни: я решила перейти на PostgreSQL для проекта", "created_at": "2024-01-15T10:30:00Z"},
                        {"role": "assistant", "content": "хорошо", "created_at": "2024-01-15T10:31:00Z"},
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    res = await import_records("claude", str(p), "u1")
    assert res["captured"] == 2

    conn = await connection_manager.get("memory.db")
    rows: list[Any] = list(await (await conn.execute("SELECT ts FROM l0_journal WHERE event='import' ORDER BY id")).fetchall())
    expected0 = datetime.fromisoformat("2024-01-15T10:30:00+00:00").timestamp()
    expected1 = datetime.fromisoformat("2024-01-15T10:31:00+00:00").timestamp()
    assert rows[0][0] == pytest.approx(expected0)
    assert rows[1][0] == pytest.approx(expected1)
