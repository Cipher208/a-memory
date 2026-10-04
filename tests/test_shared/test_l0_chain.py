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


async def test_hash_chain_full_text_no_prefix_collision(cm) -> None:
    """P0 (аудит 05.09): обрезка [:200] давала коллизии — записи с общим
    началом (>150 симв) и разными концами получали одинаковый hash_self.
    v2 хеширует ПОЛНЫЙ текст: разные хвосты → разные хеши, verify не врёт."""
    from shared.l0 import capture

    common = "Очень длинное общее начало записи, которое раньше обрезалось на двухстах символах. " * 3  # >200 симв
    rid_a = await capture("new_message", "user", "u1", common + "ХВОСТ А", ts_override=200.0)
    rid_b = await capture("new_message", "user", "u1", common + "ХВОСТ Б", ts_override=201.0)
    assert rid_a is not None and rid_b is not None

    conn = await cm.get("memory.db")
    rows = list(await (await conn.execute("SELECT id, hash_self FROM l0_journal WHERE id IN (?, ?) ORDER BY id", (rid_a, rid_b))).fetchall())
    assert rows[0][1] != rows[1][1], "разные хвосты (>200 симв) обязаны давать разные hash_self"


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


async def _processed(rid: int) -> None:
    """Mark a captured row as processed, which is the precondition for tiering."""
    conn = await connection_manager.get("memory.db")
    await conn.execute("UPDATE l0_journal SET status='saved_l3' WHERE id=?", (rid,))
    await conn.commit()


async def test_warm_tiering_keeps_the_chain_verifiable(cm) -> None:
    """The warm tier replaces `text` with a preview; the chain covers the original.

    Before this, the digest was recomputed over the preview, so the first row to
    cross 30 days would have broken the chain by being tiered exactly as designed
    — and `verify_chain` is the only tamper detector L0 has. A long text is used
    because a short one's preview equals its text, which hides the defect.
    """
    from shared.l0 import capture, verify_chain
    from lifecycle.l0_tiers import tier_l0

    now = 1_800_000_000.0
    long_text = "Первое предложение про решение. " + "Наполнение. " * 80
    rid = await capture("new_message", "user", "u1", long_text, ts_override=now - 40 * 86400)
    await _processed(rid)

    assert await verify_chain() == []
    result = await tier_l0(now=now)
    assert result["warm"] == 1

    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT LENGTH(text), text_z IS NOT NULL FROM l0_journal WHERE id=?", (rid,))).fetchone()
    assert row[0] < len(long_text), "строка обязана была стать превью, иначе тест ничего не проверяет"
    assert row[1], "полный текст обязан лежать в text_z"

    assert await verify_chain() == []


async def test_a_tampered_warm_row_is_still_detected(cm) -> None:
    """Undoing the warm tier must not blind the detector."""
    from shared.l0 import capture, verify_chain
    from lifecycle.l0_tiers import tier_l0

    now = 1_800_000_000.0
    rid = await capture("new_message", "user", "u1", "Решение. " + "хвост. " * 90, ts_override=now - 40 * 86400)
    await _processed(rid)
    await tier_l0(now=now)
    assert await verify_chain() == []

    conn = await cm.get("memory.db")
    await conn.execute("UPDATE l0_journal SET text_z=? WHERE id=?", (b"not the original text", rid))
    await conn.commit()
    broken = await verify_chain()
    assert [b["id"] for b in broken] == [rid], "подмена сжатого текста обязана быть видна"


async def test_cold_tiering_keeps_the_chain_verifiable(cm) -> None:
    """Archiving DELETES the journal row; the link has to travel with it.

    The deleted row is the oldest, so before this the very first surviving row
    failed: its `hash_prev` named an id that no longer existed. Verified on a
    three-row chain so a surviving middle link is exercised too.
    """
    from shared.l0 import capture, verify_chain
    from lifecycle.l0_tiers import tier_l0

    now = 1_800_000_000.0
    old = await capture("new_message", "user", "u1", "очень старая строка", ts_override=now - 200 * 86400)
    mid = await capture("new_message", "user", "u1", "средняя строка", ts_override=now - 40 * 86400)
    hot = await capture("new_message", "user", "u1", "свежая строка", ts_override=now - 1 * 86400)
    for rid in (old, mid, hot):
        await _processed(rid)

    assert await verify_chain() == []
    result = await tier_l0(now=now)
    assert result["cold"] == 1, "старая строка обязана уйти в архив"
    assert result["warm"] == 1

    conn = await cm.get("memory.db")
    gone = await (await conn.execute("SELECT COUNT(*) FROM l0_journal WHERE id=?", (old,))).fetchone()
    assert gone[0] == 0, "строка обязана быть удалена из журнала — иначе тест не про архив"
    archived = await (await conn.execute("SELECT hash_prev, hash_self, text FROM l0_cold_archive WHERE id=?", (old,))).fetchone()
    assert archived[0] == "" and archived[1], "архив обязан хранить звено цепочки"
    assert archived[2] == "очень старая строка"

    assert await verify_chain() == []


async def test_a_tampered_archived_row_is_still_detected(cm) -> None:
    """The archive is the only copy of that text — tampering there must be visible."""
    from shared.l0 import capture, verify_chain
    from lifecycle.l0_tiers import tier_l0

    now = 1_800_000_000.0
    old = await capture("new_message", "user", "u1", "очень старая строка", ts_override=now - 200 * 86400)
    hot = await capture("new_message", "user", "u1", "свежая строка", ts_override=now - 1 * 86400)
    for rid in (old, hot):
        await _processed(rid)
    await tier_l0(now=now)
    assert await verify_chain() == []

    conn = await cm.get("memory.db")
    await conn.execute("UPDATE l0_cold_archive SET text='подменённый архив' WHERE id=?", (old,))
    await conn.commit()
    broken = await verify_chain()
    assert [b["id"] for b in broken] == [old]


async def test_a_removed_archived_row_breaks_the_chain(cm) -> None:
    """Deleting an archived row must not read as a clean chain.

    The gap is only detectable because the following row still names it, which is
    the whole reason the links are stored rather than the archive being treated as
    outside the chain.
    """
    from shared.l0 import capture, verify_chain
    from lifecycle.l0_tiers import tier_l0

    now = 1_800_000_000.0
    old = await capture("new_message", "user", "u1", "старая первая", ts_override=now - 200 * 86400)
    keep = await capture("new_message", "user", "u1", "свежая вторая", ts_override=now - 1 * 86400)
    for rid in (old, keep):
        await _processed(rid)
    await tier_l0(now=now)

    conn = await cm.get("memory.db")
    await conn.execute("DELETE FROM l0_cold_archive WHERE id=?", (old,))
    await conn.commit()
    broken = await verify_chain()
    assert [b["id"] for b in broken] == [keep], "разрыв обязан всплыть на следующей строке"
