"""D1.14 memory versioning — named L4 snapshots + per-mutation rollback.

Snapshots: full capture of one (layer, user) fact set; restore makes L4
exactly equal the payload (upsert through CoreMemory.save, delete missing
keys through l4.delete) — every step ledger-traced. Rollback: git revert of
ONE ledger mutation — reinstate its pre-state (old_row_json; legacy rows
fall back to old_value/old_importance).

Ceilings (by design): entry_id/created_at not restored (fresh ids, old blame
chain ends — the ledger keeps the history); restore is not one transaction
(interrupted restore = re-run it, idempotent); no auto-snapshots.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from core.memory import CoreMemory

if TYPE_CHECKING:
    from shared.connection import AsyncConnectionManager

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_BASE_LAYERS = ("user", "agent")


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid snapshot name: {name!r} (need [a-z0-9][a-z0-9_-]{{0,31}})")
    return name


def _validate_base(base_layer: str) -> str:
    if base_layer not in _BASE_LAYERS:
        raise ValueError(f"invalid base_layer: {base_layer!r} (must be one of {_BASE_LAYERS})")
    return base_layer


async def snapshot_create(cm: AsyncConnectionManager, base_layer: str, user_id: str, name: str) -> dict[str, Any]:
    _validate_name(name)
    _validate_base(base_layer)
    conn = await cm.get("memory.db")
    dup = await (
        await conn.execute("SELECT 1 FROM core_memory_snapshots WHERE layer=? AND user_id=? AND name=?", (base_layer, user_id, name))
    ).fetchone()
    if dup:
        raise ValueError(f"snapshot already exists: {name!r}")
    rows = await (
        await conn.execute(
            "SELECT key, value, importance, memory_kind, expires_at, source, metadata FROM core_memory WHERE layer=? AND user_id=?",
            (base_layer, user_id),
        )
    ).fetchall()
    payload = [dict(r) for r in rows]
    cur = await conn.execute(
        "INSERT INTO core_memory_snapshots (layer, user_id, name, payload_json, fact_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (base_layer, user_id, name, json.dumps(payload, ensure_ascii=False), len(payload), time.time()),
    )
    await conn.commit()
    return {"name": name, "snapshot_id": int(cur.lastrowid or 0), "facts": len(payload)}


async def snapshot_list(cm: AsyncConnectionManager, user_id: str, base_layer: str = "") -> list[dict[str, Any]]:
    conn = await cm.get("memory.db")
    sql = "SELECT snapshot_id, layer, user_id, name, fact_count, created_at FROM core_memory_snapshots WHERE user_id=?"
    params: tuple[Any, ...] = (user_id,)
    if base_layer:
        sql += " AND layer=?"
        params = (*params, base_layer)
    sql += " ORDER BY created_at DESC"
    rows = await (await conn.execute(sql, params)).fetchall()
    return [dict(r) for r in rows]


async def snapshot_restore(cm: AsyncConnectionManager, base_layer: str, user_id: str, name: str) -> dict[str, Any]:
    _validate_name(name)
    _validate_base(base_layer)
    conn = await cm.get("memory.db")
    snap = await (
        await conn.execute("SELECT payload_json FROM core_memory_snapshots WHERE layer=? AND user_id=? AND name=?", (base_layer, user_id, name))
    ).fetchone()
    if not snap:
        raise ValueError(f"snapshot not found: {name!r}")
    payload = json.loads(snap["payload_json"])
    snap_keys = {p["key"] for p in payload}

    cur_rows = await (await conn.execute("SELECT key FROM core_memory WHERE layer=? AND user_id=?", (base_layer, user_id))).fetchall()
    cur_keys = {str(r["key"]) for r in cur_rows}

    core = CoreMemory(cm=cm, layer=base_layer)
    deleted: list[str] = []
    for k in sorted(cur_keys - snap_keys):
        if await core.delete(user_id, k, triggered_by=f"snapshot_restore:{name}"):
            deleted.append(k)

    restored: list[str] = []
    for p in payload:
        metadata = json.loads(p["metadata"]) if p["metadata"] else None
        await core.save(
            user_id,
            p["key"],
            p["value"],
            importance=p["importance"],
            memory_kind=p["memory_kind"],
            expires_at=p["expires_at"],
            source=p["source"],
            metadata=metadata,
            triggered_by=f"snapshot_restore:{name}",
        )
        restored.append(p["key"])
    return {"name": name, "restored": len(restored), "deleted": len(deleted)}


async def rollback(cm: AsyncConnectionManager, history_id: int) -> dict[str, Any]:
    """Undo exactly ONE ledger mutation: reinstate its pre-state (git revert, not reset)."""
    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT * FROM core_memory_history WHERE history_id=?", (int(history_id),))).fetchone()
    if not row:
        raise ValueError(f"history_id not found: {history_id}")
    core = CoreMemory(cm=cm, layer=str(row["layer"]))
    tag = f"rollback:{history_id}"

    if row["old_value"] is None and row["old_row_json"] is None:
        # mutation N was an insert → undo = the key must not exist
        ok = await core.delete(str(row["user_id"]), str(row["key"]), triggered_by=tag)
        return {"history_id": int(history_id), "key": str(row["key"]), "action": "deleted", "deleted": ok}

    if row["old_row_json"]:
        old = json.loads(row["old_row_json"])
        eid = await core.save(
            str(row["user_id"]),
            old["key"],
            old["value"],
            importance=old["importance"],
            memory_kind=old["memory_kind"],
            expires_at=old["expires_at"],
            source=old["source"],
            metadata=json.loads(old["metadata"]) if old["metadata"] else None,
            triggered_by=tag,
        )
    else:
        # legacy pre-d114 row: value+importance only
        eid = await core.save(
            str(row["user_id"]),
            str(row["key"]),
            str(row["old_value"]),
            importance=row["old_importance"] if row["old_importance"] is not None else None,
            triggered_by=tag,
        )
    return {"history_id": int(history_id), "key": str(row["key"]), "action": "restored", "entry_id": int(eid)}
