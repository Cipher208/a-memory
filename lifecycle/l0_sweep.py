"""TTL sweep: soft-expiry of expired L4 facts (expires_at < now) with B5 protections.

S5 (Memanto): expiry is soft — expired rows are not destroyed but archived into
archived_memories, and `ForgettingSystem.restore_entries` can bring one back.
Archiving a row is not on its own a promise that it returns: the restore wants the
id **and** the owner, it refuses a row whose `archive_reason` is a purge reason, and
it refuses a row whose recorded kind names no table it can write to. A row archived
here is restorable; a row archived by a purge, or one whose kind was never recorded,
is not — and the whole difference lives in `archive_reason` and `memory_type`.

Protections (never touch more than is safe):
- min_remain: never shrink a layer below min_remain live rows — the batch is
  trimmed (sweep stops at the floor).
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
from shared.write_dispatch import get_write_dispatch


async def sweep_expired(
    *,
    min_remain: int = 50,
    stop_pct: float = 0.8,
    layer: str | None = None,
    cm: AsyncConnectionManager | None = None,
) -> dict[str, int | str | None]:
    """Delete expired L4 rows (expires_at < now), honoring B5 protections."""
    dispatch = get_write_dispatch(cm or connection_manager)
    now = time.time()

    where_layer = "layer=?" if layer else "1=1"
    # The bound value is the layer name. It used to be `now` — a float epoch — so a
    # layer-scoped sweep matched nothing and returned "0 deleted" instead of running.
    params: tuple[object, ...] = (layer,) if layer else ()

    total = int(await dispatch.scalar(DB_NAME, f"SELECT COUNT(*) FROM core_memory WHERE {where_layer}", params) or 0)

    # never_archive kinds (rule/instruction/commitment) are exempt even when expired
    from shared.memory_types import MemoryKind, get_policy

    protected = [k.value for k in MemoryKind if get_policy(k).never_archive]
    placeholders = ",".join(["?"] * len(protected))

    expired = int(
        await dispatch.scalar(
            DB_NAME,
            f"""SELECT COUNT(*) FROM core_memory
                WHERE {where_layer} AND expires_at IS NOT NULL AND expires_at < ?
                  AND (memory_kind IS NULL OR memory_kind NOT IN ({placeholders}))""",
            (*params, now, *protected),
        )
        or 0
    )

    # The denominator is the sweepable population, not the whole layer: protected
    # kinds sit in `total` but can never appear in `expired`, so once they exceed
    # 20% of the table the ratio could never reach 0.8 and the guard went dead
    # (cowagent measured 18% protected, one release away). With nothing sweepable
    # there is no ratio to judge — fall through to the `batch <= 0` early return.
    protected_total = int(
        await dispatch.scalar(
            DB_NAME,
            f"SELECT COUNT(*) FROM core_memory WHERE {where_layer} AND memory_kind IN ({placeholders})",
            (*params, *protected),
        )
        or 0
    )
    sweepable = total - protected_total

    if sweepable > 0 and expired / sweepable > stop_pct:
        summary: dict[str, int | str | None] = {"deleted": 0, "skipped_reason": "mass_expiry", "remaining": total}
        await _journal(summary, layer)
        return {"skipped": "mass_expiry"}

    to_delete = max(0, total - min_remain)
    batch = min(expired, to_delete)
    if batch <= 0:
        summary = {"deleted": 0, "skipped_reason": None, "remaining": total}
        await _journal(summary, layer)
        return summary

    # S5: archive (restorable) instead of erasing — the TTL-test contract is
    # unchanged (rows leave core_memory), but the data stays in archived_memories.
    from lifecycle.forgetting import ForgettingSystem

    ids_rows = await dispatch.query(
        DB_NAME,
        f"""SELECT entry_id FROM core_memory
            WHERE {where_layer} AND expires_at IS NOT NULL AND expires_at < ?
              AND (memory_kind IS NULL OR memory_kind NOT IN ({placeholders}))
            LIMIT ?""",
        (*params, now, *protected, batch),
    )
    ids = [int(r["entry_id"]) for r in ids_rows]
    deleted = await ForgettingSystem(cm=cm or connection_manager, layer=layer or "user").archive_entries(ids)

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
