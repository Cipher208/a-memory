"""Recall confirmation signal + CLS replay (nightly L4 boost) + L0 gate replay.

record_recall_useful: dream writes one audit_log row per recalled core fact
(action='recall_useful', target_id=str(entry_id)) — the frequency signal
consumed by ACT-R activation and ImportanceScheduler. Written directly to
audit_log (not via AuditTrail.log) so the signal survives even when the
audit_trail feature flag is off.
cls_replay: nightly 2nd phase — boosts facts confirmed by recall within the
window (Complementary Learning Systems: hippocampal replay consolidates
reactivated traces into neocortex).
replay (L0, Task F6): re-runs the G1 distiller over the l0_journal window.
Idempotency: each processed row records {'gate', 'config_hash', 'ts'} in its
decisions JSON; a row is skipped when the current config-hash is already
recorded for that gate. Rows reset to 'gated_out' (decisions cleared) are
re-processed.
"""

from __future__ import annotations

import json
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME


async def record_recall_useful(cm: Any, layer: str, user_id: str, entries: list[tuple[int, str]]) -> int:
    """Record one recall_useful row per (entry_id, key). Returns rows written."""
    if not entries:
        return 0
    conn = await cm.get(DB_NAME)
    now = time.time()
    await conn.executemany(
        "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?, 'recall_useful', 'core_memory', ?, ?, ?)",
        [(user_id, str(entry_id), json.dumps({"key": key}), now) for entry_id, key in entries],
    )
    await conn.commit()
    return len(entries)


async def cls_replay(cm: Any, user_id: str, layer: str = "user", window_hours: int = 24, boost: float = 0.05) -> dict[str, int]:
    """Boost L4 facts recalled within the window. Returns counters."""
    conn = await cm.get(DB_NAME)
    cutoff = time.time() - window_hours * 3600
    rows = await (
        await conn.execute(
            """SELECT entry_id, importance FROM core_memory
               WHERE layer=? AND user_id=? AND importance < 1.0
                 AND entry_id IN (
                     SELECT DISTINCT CAST(target_id AS INTEGER) FROM audit_log
                     WHERE action='recall_useful' AND layer='core_memory' AND timestamp > ?
                 )""",
            (layer, user_id, cutoff),
        )
    ).fetchall()
    boosted = 0
    now = time.time()
    for r in rows:
        old = float(r["importance"])
        new = min(1.0, old + boost)
        if new <= old:
            continue
        await conn.execute("UPDATE core_memory SET importance=?, updated_at=? WHERE entry_id=?", (new, now, int(r["entry_id"])))
        await conn.execute(
            """INSERT INTO importance_audit (user_id, chunk_id, source, old_importance, new_importance, signal_breakdown, reason, rescored_at)
               VALUES (?, ?, 'core_memory', ?, ?, '{}', 'cls_replay', ?)""",
            (user_id, int(r["entry_id"]), old, new, now),
        )
        boosted += 1
    await conn.commit()
    return {"boosted": boosted}


def config_hash() -> str:
    """Hash of the gate config that determines G1 routing decisions.

    Covers the importance threshold (hooks.auto_save_threshold) and the
    rules.yaml content (D1.9 boosts/tags). Replay skips rows already processed
    under the same hash; a changed hash re-opens the window.
    """
    import hashlib

    from config import config
    from features.rules import load_rules

    payload = json.dumps(
        {
            "threshold": float(config.get("hooks", "auto_save_threshold", default=0.5)),
            "rules": load_rules(force=True),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def replay(*, since_days: int = 7, gate: str = "g1") -> dict[str, int]:
    """Re-run the G1 distiller over the l0_journal window [now-since_days, now].

    Selects rows with status in ('received', 'gated_out'); skips rows whose
    decisions already record this (gate, config_hash) pair — idempotent under
    unchanged config. Each (re)processed row is routed via distill_and_route
    (mem/graph built on connection_manager, user_id/layer from the row,
    extra_tags omitted — rules were applied at first pass) and its status set
    to 'promoted_l4' / 'saved_l3' / 'gated_out' with processed_at=now.
    """
    from core import MemoryManager
    from graph.epistemic import EpistemicGraph
    from lifecycle.distiller import distill_and_route

    conn = await connection_manager.get(DB_NAME)
    cutoff = time.time() - since_days * 86400
    chash = config_hash()
    rows = await (
        await conn.execute(
            "SELECT id, layer, user_id, text, decisions FROM l0_journal WHERE ts > ? AND status IN ('received', 'gated_out') ORDER BY id",
            (cutoff,),
        )
    ).fetchall()

    processed = skipped = conflicts = 0
    for row in rows:
        decisions: list[dict[str, Any]] = json.loads(row["decisions"] or "[]")
        if any(d.get("gate") == gate and d.get("config_hash") == chash for d in decisions):
            skipped += 1
            continue
        mem = MemoryManager(cm=connection_manager).get_layer(row["layer"] or "user", row["user_id"])
        graph = EpistemicGraph(cm=connection_manager, layer=row["layer"] or "user")
        route = await distill_and_route(mem, graph, row["user_id"], row["text"], 0.6, event=gate)
        conflicts += route["conflicts"]
        new_status = "promoted_l4" if route["l4_saved"] else ("saved_l3" if route["l3_saved"] else "gated_out")
        decisions.append({"gate": gate, "config_hash": chash, "ts": time.time()})
        await conn.execute(
            "UPDATE l0_journal SET status=?, processed_at=?, decisions=? WHERE id=?",
            (new_status, time.time(), json.dumps(decisions, ensure_ascii=False), row["id"]),
        )
        processed += 1
    await conn.commit()
    return {"processed": processed, "skipped": skipped, "conflicts": conflicts}
