"""Task C5 (S13 estate-quality): бэкфилл rag_chunks.memory_kind.

rag_chunks.memory_kind существует, но ingestor его не тегирует (NULL) —
dense_per_kind арм отдаёт нетегированные чанки как 'fact' по COALESCE.
Скрипт классифицирует NULL-чанки по kind_for_text(content) и обновляет
батчами по 100.

Бэкфилл — разовая мелиорация корпуса; тегирование на ingest-path — future
fill (ingestor.ingest), до тех пор новые чанки остаются NULL → 'fact'.

CLI:
    uv run python scripts/backfill_memory_kind.py            # dry-run по умолчанию
    uv run python scripts/backfill_memory_kind.py --apply    # записать
    uv run python scripts/backfill_memory_kind.py --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from typing import Any

from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

BATCH_SIZE = 100


async def backfill_memory_kind(cm: Any, *, dry_run: bool = True, batch_size: int = BATCH_SIZE, limit: int | None = None) -> int:
    """NULL memory_kind → kind_for_text(content). Возвращает количество затронутых строк (уже тегированные не трогает)."""
    from shared.memory_types import kind_for_text

    conn = await cm.get(DB_NAME)
    cur = await conn.execute(
        "SELECT id, content FROM rag_chunks WHERE memory_kind IS NULL ORDER BY id" + (f" LIMIT {int(limit)}" if limit is not None else ""),
    )
    rows = await cur.fetchall()

    if dry_run:
        logger.info("backfill dry-run: %d rag_chunks без memory_kind", len(rows))
        return len(rows)

    updated = 0
    for start in range(0, len(rows), batch_size):
        batch = [(kind_for_text(str(r["content"])).value, int(r["id"])) for r in rows[start : start + batch_size]]
        await conn.executemany("UPDATE rag_chunks SET memory_kind = ? WHERE id = ? AND memory_kind IS NULL", batch)
        await conn.commit()
        updated += len(batch)
        logger.info("backfill batch: %d/%d", min(start + batch_size, len(rows)), len(rows))
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rag_chunks.memory_kind бэкфилл по kind_for_text")
    parser.add_argument("--apply", action="store_true", help="записать изменения (по умолчанию dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="макс. строк за прогон")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    async def run() -> int:
        from shared.connection import connection_manager

        try:
            n = await backfill_memory_kind(
                connection_manager,
                dry_run=not args.apply,
                batch_size=args.batch_size,
                limit=args.limit,
            )
        finally:
            with contextlib.suppress(Exception):
                await connection_manager.close_all()
        print(f"{'applied' if args.apply else 'dry-run'}: {n} chunk(s)")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
