"""Task C5: memory_kind бэкфилл — rag_chunks NULL → kind_for_text(content).

S13 (estate-quality): dense_per_kind арм видит только тегированные чанки;
нетегированный корпус деградирует до 'fact'.
"""

from __future__ import annotations

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


async def _chunk(content: str, kind: str | None = None) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute("INSERT INTO rag_pages (layer, user_id, title, content, wiki_type) VALUES ('user', 'bf', 't', 'x', 'note')")
    page_id = int(cur.lastrowid or 0)
    cur = await conn.execute(
        "INSERT INTO rag_chunks (page_id, chunk_index, content, memory_kind) VALUES (?, 0, ?, ?)",
        (page_id, content, kind),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def test_backfill_null_chunks_tagged_by_kind(db) -> None:
    from scripts.backfill_memory_kind import backfill_memory_kind

    c1 = await _chunk("Я обещаю сделать это к пятнице")  # commitment (обещаю)
    c2 = await _chunk("Просто заметка про борщ со свёклой")  # fact
    c3 = await _chunk("Уже тегировано вручную", kind="decision")  # не трогать
    c4 = await _chunk("Запрещено деплоить в пятницу")  # rule (запрещено)

    n = await backfill_memory_kind(connection_manager, dry_run=False, batch_size=2)
    assert n == 3, f"только NULL-чанки бэкфиллится, got {n}"

    conn = await connection_manager.get(DB_NAME)
    rows = await (await conn.execute("SELECT id, memory_kind FROM rag_chunks WHERE id IN (?, ?, ?, ?)", (c1, c2, c3, c4))).fetchall()
    kinds = {r["id"]: r["memory_kind"] for r in rows}
    assert kinds[c1] == "commitment"
    assert kinds[c2] == "fact"
    assert kinds[c3] == "decision", "уже тегированный чанк не перезаписывается"
    assert kinds[c4] == "rule"

    # Повторный прогон — 0 (идемпотентен)
    assert await backfill_memory_kind(connection_manager, dry_run=False) == 0


async def test_backfill_dry_run_counts_without_writing(db) -> None:
    from scripts.backfill_memory_kind import backfill_memory_kind

    await _chunk("нужно сделать бэкап")
    n = await backfill_memory_kind(connection_manager, dry_run=True)
    assert n == 1
    conn = await connection_manager.get(DB_NAME)
    row = await (await conn.execute("SELECT COUNT(*) c FROM rag_chunks WHERE memory_kind IS NOT NULL")).fetchone()
    assert row["c"] == 0, "dry-run не пишет"


async def test_backfill_limit(db) -> None:
    from scripts.backfill_memory_kind import backfill_memory_kind

    await _chunk("нужно сделать раз")
    await _chunk("нужно сделать два")
    assert await backfill_memory_kind(connection_manager, dry_run=False, limit=1) == 1
    assert await backfill_memory_kind(connection_manager, dry_run=False, limit=1) == 1
