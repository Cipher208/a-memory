"""A2.2 core_memory_history read surface — the mutation ledger behind D1.11 branches and D1.14 snapshots.

Scars stay forever (no retention cap yet; D1.14 owns pruning).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shared.connection import AsyncConnectionManager

logger = logging.getLogger(__name__)

# list() stays slim — the row-JSON blobs are context bloat; get() returns them
_SLIM_COLS = (
    "history_id, layer, user_id, key, old_value, new_value, old_importance, new_importance, commit_hash, triggered_by, created_at"
)


async def list_history(
    cm: AsyncConnectionManager,
    user_id: str,
    layer: str = "user",
    key: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Ledger rows newest-first, scoped to user+layer (optionally one key)."""
    try:
        conn = await cm.get("memory.db")
        if key:
            cursor = await conn.execute(
                f"SELECT {_SLIM_COLS} FROM core_memory_history WHERE user_id=? AND layer=? AND key=? ORDER BY history_id DESC LIMIT ?",
                (user_id, layer, key, int(limit)),
            )
        else:
            cursor = await conn.execute(
                f"SELECT {_SLIM_COLS} FROM core_memory_history WHERE user_id=? AND layer=? ORDER BY history_id DESC LIMIT ?",
                (user_id, layer, int(limit)),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("history read failed: %s", exc)
        return []


async def get_history_row(cm: AsyncConnectionManager, history_id: int) -> dict[str, Any] | None:
    try:
        conn = await cm.get("memory.db")
        cursor = await conn.execute("SELECT * FROM core_memory_history WHERE history_id=?", (int(history_id),))
        row = await cursor.fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.debug("history row read failed: %s", exc)
        return None
