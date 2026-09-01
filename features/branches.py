"""D1.11 memory branches — A/B persona staging for L4 core facts.

A branch is a namespace in the existing `layer` column: "<base>@<name>"
(base = 'user' | 'agent'). Branch rows live in core_memory next to main and
are invisible to RAG / inject / smart_context / stats (those exact-match the
base layer). Merge = cherry-pick upsert into base with source='branch_merge'
(D1.6 provenance) and triggered_by='branch_merge:<name>' (A2.2 ledger).

Ceilings (by design): merge does not propagate deletions (delete on main
directly); no conflict detection (cherry-pick is deliberate key selection);
no checkout semantics — branch access only through this module; inject
always follows main, switch = merge.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from core.memory import CoreMemory

if TYPE_CHECKING:
    from shared.connection import AsyncConnectionManager

logger = logging.getLogger(__name__)

_BRANCH_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_BASE_LAYERS = ("user", "agent")


def validate_branch_name(name: str) -> str:
    if not _BRANCH_NAME_RE.match(name or ""):
        raise ValueError(f"invalid branch name: {name!r} (need [a-z0-9][a-z0-9_-]{{0,31}})")
    return name


def branch_layer(base_layer: str, name: str) -> str:
    """Composite layer value for the branch, e.g. 'user@exp1'."""
    validate_branch_name(name)
    if base_layer not in _BASE_LAYERS:
        raise ValueError(f"invalid base_layer: {base_layer!r} (must be one of {_BASE_LAYERS})")
    return f"{base_layer}@{name}"


async def create_branch(cm: AsyncConnectionManager, base_layer: str, user_id: str, name: str) -> dict[str, Any]:
    """Full clone: copy every base-layer fact for this user into the branch."""
    layer = branch_layer(base_layer, name)
    conn = await cm.get("memory.db")
    existing = await (await conn.execute("SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=?", (layer, user_id))).fetchone()
    if existing and int(existing[0]) > 0:
        raise ValueError(f"branch already exists: {name!r}")
    cur = await conn.execute(
        """INSERT INTO core_memory
             (layer, user_id, key, value, importance, memory_kind, expires_at, source, metadata, created_at, updated_at)
           SELECT ?, user_id, key, value, importance, memory_kind, expires_at, source, metadata, created_at, updated_at
           FROM core_memory WHERE layer=? AND user_id=?""",
        (layer, base_layer, user_id),
    )
    await conn.commit()
    return {"name": name, "layer": layer, "copied": int(cur.rowcount or 0)}


async def write_branch(
    cm: AsyncConnectionManager,
    base_layer: str,
    user_id: str,
    name: str,
    key: str,
    value: str,
    importance: float | None = None,
) -> dict[str, Any]:
    """Save/update one fact in the branch layer (how persona experiments land)."""
    core = CoreMemory(cm=cm, layer=branch_layer(base_layer, name))
    eid = await core.save(user_id, key, value, importance=importance, triggered_by=f"branch_write:{name}")
    return {"name": name, "key": key, "entry_id": eid}


async def read_branch(cm: AsyncConnectionManager, base_layer: str, user_id: str, name: str, limit: int = 200) -> list[dict[str, Any]]:
    core = CoreMemory(cm=cm, layer=branch_layer(base_layer, name))
    entries = await core.get_all(user_id, limit=int(limit))
    return [
        {
            "key": e.key,
            "value": e.value,
            "importance": e.importance,
            "memory_kind": e.memory_kind,
            "updated_at": e.updated_at,
        }
        for e in entries
    ]


async def diff_branch(cm: AsyncConnectionManager, base_layer: str, user_id: str, name: str) -> dict[str, Any]:
    """Branch vs base per key: added / changed / unchanged (+ changed values)."""
    layer = branch_layer(base_layer, name)
    conn = await cm.get("memory.db")
    b_rows = await (await conn.execute("SELECT key, value FROM core_memory WHERE layer=? AND user_id=?", (layer, user_id))).fetchall()
    m_rows = await (await conn.execute("SELECT key, value FROM core_memory WHERE layer=? AND user_id=?", (base_layer, user_id))).fetchall()
    b_map = {str(r["key"]): str(r["value"]) for r in b_rows}
    m_map = {str(r["key"]): str(r["value"]) for r in m_rows}
    added = sorted(k for k in b_map if k not in m_map)
    changed = sorted(k for k in b_map if k in m_map and b_map[k] != m_map[k])
    unchanged = sorted(k for k in b_map if k in m_map and b_map[k] == m_map[k])
    return {
        "added": added,
        "changed": changed,
        "unchanged": unchanged,
        "changed_values": [{"key": k, "base": m_map[k], "branch": b_map[k]} for k in changed],
    }


async def merge_branch(
    cm: AsyncConnectionManager,
    base_layer: str,
    user_id: str,
    name: str,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """Cherry-pick upsert listed keys (default: all differing) from branch into base.

    Provenance: branch_merge. Unchanged/unknown keys are skipped.
    """
    layer = branch_layer(base_layer, name)
    diff = await diff_branch(cm, base_layer, user_id, name)
    differing = set(diff["added"]) | set(diff["changed"])
    target = sorted(differing) if keys is None else list(keys)

    merged: list[str] = []
    skipped: list[str] = []
    b_core = CoreMemory(cm=cm, layer=layer)
    m_core = CoreMemory(cm=cm, layer=base_layer)
    for k in target:
        if k not in differing:
            skipped.append(k)
            continue
        row = await b_core.get(user_id, k)
        if row is None:  # race: branch row vanished since diff
            skipped.append(k)
            continue
        await m_core.save(
            user_id,
            k,
            row.value,
            importance=row.importance,
            source="branch_merge",
            triggered_by=f"branch_merge:{name}",
        )
        merged.append(k)
    return {"name": name, "merged": merged, "skipped": skipped}


async def delete_branch(cm: AsyncConnectionManager, base_layer: str, user_id: str, name: str) -> dict[str, Any]:
    """Drop the branch's rows. Deleting an absent (valid-name) branch is a no-op."""
    layer = branch_layer(base_layer, name)
    conn = await cm.get("memory.db")
    cur = await conn.execute("DELETE FROM core_memory WHERE layer=? AND user_id=?", (layer, user_id))
    await conn.commit()
    return {"name": name, "layer": layer, "deleted": int(cur.rowcount or 0)}


async def list_branches(cm: AsyncConnectionManager, user_id: str = "") -> list[dict[str, Any]]:
    conn = await cm.get("memory.db")
    sql = "SELECT layer, COUNT(*) AS n FROM core_memory WHERE layer LIKE '%@%'"
    params: list[Any] = []
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " GROUP BY layer ORDER BY layer"
    rows = await (await conn.execute(sql, params)).fetchall()
    out = []
    for r in rows:
        base, _, name = str(r["layer"]).rpartition("@")
        out.append({"layer": str(r["layer"]), "base_layer": base, "name": name, "facts": int(r["n"])})
    return out
