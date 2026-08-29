"""Staged mutation: proposal → review → apply (C1.11 S4/S5).

Risk-tier routing lives at the call-sites (auto_save_text L4 branch,
consolidation hook, forgetting_ritual); this module owns the proposal
lifecycle and the per-kind apply dispatch. decided_by ∈ {'tool', 'expiry'}.
"""

from __future__ import annotations

import json
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

_EXPIRE_DAYS_DEFAULT = 7


def _staging_enabled() -> bool:
    from config import config

    return bool(config.get("staging", "enabled", default=True))


def _expire_days() -> int:
    from config import config

    return int(config.get("staging", "expire_days", default=_EXPIRE_DAYS_DEFAULT))


async def propose(source: str, kind: str, user_id: str, layer: str, payload: dict[str, Any]) -> int:
    """Record one proposal. Returns its id."""
    now = time.time()
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO mutation_proposals (source, kind, user_id, layer, payload, status, proposed_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        (source, kind, user_id, layer, json.dumps(payload, ensure_ascii=False), now, now + _expire_days() * 86400),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def expire_stale() -> int:
    """Flip pending rows past expires_at → expired. Lazy; called on reads."""
    conn = await connection_manager.get(DB_NAME)
    now = time.time()
    cur = await conn.execute("SELECT id, user_id, layer FROM mutation_proposals WHERE status = 'pending' AND expires_at < ?", (now,))
    rows = await cur.fetchall()
    if not rows:
        return 0
    await conn.execute(
        "UPDATE mutation_proposals SET status = 'expired', decided_at = ?, decided_by = 'expiry' WHERE status = 'pending' AND expires_at < ?",
        (now, now),
    )
    await conn.commit()
    from features.audit_trail import AuditTrail

    audit = AuditTrail()
    for r in rows:
        await audit.log(r["user_id"], "proposal_expired", layer=r["layer"], target_id=str(r["id"]), details={"kind": "expiry"})
    return len(rows)


async def list_pending(user_id: str = "default", limit: int = 20) -> list[dict[str, Any]]:
    await expire_stale()
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "SELECT id, source, kind, user_id, layer, payload, status, proposed_at, expires_at FROM mutation_proposals"
        " WHERE status = 'pending' AND user_id = ? ORDER BY id LIMIT ?",
        (user_id, limit),
    )
    rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except Exception:
            payload = {"raw": r["payload"]}
        out.append(
            {
                "id": r["id"],
                "source": r["source"],
                "kind": r["kind"],
                "user_id": r["user_id"],
                "layer": r["layer"],
                "payload": payload,
                "status": r["status"],
                "proposed_at": r["proposed_at"],
                "expires_at": r["expires_at"],
            }
        )
    return out


async def count_pending(user_id: str = "default") -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute("SELECT count(*) FROM mutation_proposals WHERE status = 'pending' AND user_id = ?", (user_id,))
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def decide(proposal_id: int, approve: bool, mem: Any) -> dict[str, Any]:
    """Apply (approve=True) or reject one proposal. Unknown id → ValueError."""
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute("SELECT id, source, kind, user_id, layer, payload, status FROM mutation_proposals WHERE id = ?", (proposal_id,))
    row = await cur.fetchone()
    if row is None:
        raise ValueError(f"unknown proposal id: {proposal_id}")
    if row["status"] != "pending":
        raise ValueError(f"proposal {proposal_id} is not pending (status={row['status']})")
    payload = json.loads(row["payload"])
    if not approve:
        await conn.execute(
            "UPDATE mutation_proposals SET status = 'rejected', decided_at = ?, decided_by = 'tool' WHERE id = ?",
            (time.time(), proposal_id),
        )
        await conn.commit()
        from features.audit_trail import AuditTrail

        await AuditTrail().log(row["user_id"], "proposal_rejected", layer=row["layer"], target_id=str(proposal_id), details={"kind": row["kind"]})
        return {"id": proposal_id, "status": "rejected", "result_ref": None}

    result_ref = await _apply(row["kind"], row["user_id"], row["layer"], payload, mem)
    await conn.execute(
        "UPDATE mutation_proposals SET status = 'applied', decided_at = ?, decided_by = 'tool', result_ref = ? WHERE id = ?",
        (time.time(), result_ref, proposal_id),
    )
    await conn.commit()
    from features.audit_trail import AuditTrail

    await AuditTrail().log(
        row["user_id"], "proposal_applied", layer=row["layer"], target_id=str(proposal_id), details={"kind": row["kind"], "result": result_ref}
    )
    return {"id": proposal_id, "status": "applied", "result_ref": result_ref}


async def _apply(kind: str, user_id: str, layer: str, payload: dict[str, Any], mem: Any) -> str:
    """Execute the write the direct path would have done. Same code path, pinned inputs."""
    if kind == "core_write":
        mem_obj = getattr(mem, "mm", None)
        if mem_obj is not None:
            mem_u = mem_obj.user_memory(user_id) if layer == "user" else mem_obj.agent_memory(user_id)
            entry_id = await mem_u.remember(payload["key"], payload["value"], float(payload["importance"]))
        elif mem is not None:
            entry_id = await mem.remember(payload["key"], payload["value"], float(payload["importance"]))
        else:
            raise ValueError("core_write apply requires mem (AppContext or a memory facade)")
        return str(entry_id)
    if kind == "consolidate_staging":
        from lifecycle.consolidation import ConsolidationEngine

        engine = ConsolidationEngine(layer=layer)
        items = payload.get("items", [])
        min_imp = payload.get("min_importance")
        if min_imp is not None:
            result = await engine.consolidate_staging(user_id, items, min_importance=float(min_imp))
        else:
            result = await engine.consolidate_staging(user_id, items)
        return f"promoted={result.get('promoted', 0)}"
    if kind == "archive":
        from lifecycle.forgetting import ForgettingSystem

        count = await ForgettingSystem(layer=layer).archive_entries([int(i) for i in payload.get("ids", [])])
        return f"archived={count}"
    raise ValueError(f"unknown proposal kind: {kind!r}")
