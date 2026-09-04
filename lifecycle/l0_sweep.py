"""TTL sweep: delete expired L4 facts (expires_at < now) with B5 protections.

Protections (never delete more than is safe):
- min_remain: never shrink a layer below min_remain live rows — the batch is
  trimmed (deletion stops at the floor).
- stop_pct: if expired rows exceed stop_pct of all rows, the sweep refuses to
  run entirely (mass-expiry signal — likely a clock/batch bug, not real decay).
never_archive kinds (rule/instruction/commitment) are always exempt, even when
expired. Every run writes a cleaner_summary into the l0_journal.
"""

from __future__ import annotations

import time
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


async def sweep_expired(
    *,
    min_remain: int = 50,
    stop_pct: float = 0.8,
    layer: str | None = None,
    cm: AsyncConnectionManager | None = None,
) -> dict[str, int | str | None]:
    """Delete expired L4 rows (expires_at < now), honoring B5 protections."""
    conn = await (cm or connection_manager).get(DB_NAME)
    now = time.time()

    where_layer = "layer=?" if layer else "1=1"
    params: tuple[object, ...] = (now,) if layer else ()

    total_row = await (await conn.execute(f"SELECT COUNT(*) FROM core_memory WHERE {where_layer}", params)).fetchone()
    total = int(total_row[0]) if total_row else 0

    # never_archive kinds (rule/instruction/commitment) are exempt even when expired
    from shared.memory_types import MemoryKind, get_policy

    protected = [k.value for k in MemoryKind if get_policy(k).never_archive]
    placeholders = ",".join(["?"] * len(protected))

    expired_row = await (
        await conn.execute(
            f"""SELECT COUNT(*) FROM core_memory
                WHERE {where_layer} AND expires_at IS NOT NULL AND expires_at < ?
                  AND (memory_kind IS NULL OR memory_kind NOT IN ({placeholders}))""",
            (*params, now, *protected),
        )
    ).fetchone()
    expired = int(expired_row[0]) if expired_row else 0

    if total > 0 and expired / total > stop_pct:
        summary: dict[str, int | str | None] = {"deleted": 0, "skipped_reason": "mass_expiry", "remaining": total}
        await _journal(summary, layer)
        return {"skipped": "mass_expiry"}

    to_delete = max(0, total - min_remain)
    batch = min(expired, to_delete)

    cur = await conn.execute(
        f"""DELETE FROM core_memory WHERE entry_id IN (
                SELECT entry_id FROM core_memory
                WHERE {where_layer} AND expires_at IS NOT NULL AND expires_at < ?
                  AND (memory_kind IS NULL OR memory_kind NOT IN ({placeholders}))
                LIMIT ?)""",
        (*params, now, *protected, batch),
    )
    deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    await conn.commit()

    remaining = total - deleted
    summary = {"deleted": deleted, "skipped_reason": None, "remaining": remaining}
    await _journal(summary, layer)
    return summary


async def _journal(summary: dict[str, Any], layer: str | None) -> None:
    """Best-effort cleaner_summary into l0_journal — never blocks the sweep."""
    import contextlib

    with contextlib.suppress(Exception):
        from shared.l0 import capture

        await capture("l0_sweep", layer or "user", "default", f"cleaner_summary: {summary}", decisions=[summary])
