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

from config import config
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

    Covers the EMA importance threshold (S17 F2: adaptive gate in auto_save),
    the static fallback (hooks.auto_save_threshold) and the rules.yaml content
    (D1.9 boosts/tags). Replay skips rows already processed under the same
    hash; a changed hash re-opens the window.
    """
    import hashlib

    from config import config
    from features.rules import load_rules
    from shared.adaptive import adaptive_threshold

    payload = json.dumps(
        {
            "threshold": float(config.get("hooks", "auto_save_threshold", default=0.5)),
            "ema_threshold": adaptive_threshold._current_value,
            "rules": load_rules(force=True),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def replay(
    *,
    since_days: int = 7,
    gate: str = "g1",
    ids: list[int] | None = None,
    respect_gate: bool | None = None,
    max_rows: int | None = None,
) -> dict[str, int]:
    """Re-run the G1 distiller over the l0_journal window [now-since_days, now].

    Selects rows with status in ('received', 'gated_out'); skips rows whose
    decisions already record this (gate, config_hash) pair — idempotent under
    unchanged config. Each (re)processed row is routed via distill_and_route
    (mem/graph built on connection_manager, user_id/layer from the row,
    extra_tags omitted — rules were applied at first pass) and its status set
    to 'promoted_l4' / 'saved_l3' / 'gated_out' with processed_at=now.

    THE GATE, AND WHY IT IS APPLIED HERE NOW

    This used to distil every selected row without consulting the importance gate
    at all, so the two paths disagreed about the same text: a message the live
    hook refused would be written to memory by a replay. That was tolerable while
    a human typed every replay, and not tolerable once the nightly pass calls it
    automatically — it would have re-admitted the refused backlog, which is the
    exact work the gate exists to do.

    The gate's own scorer is used (`features.importance.evaluate_importance` plus
    the rules boost), NOT `score_text`, so a replayed row is judged by the same
    measure the live path used. The two scorers are not interchangeable:
    measured on live rows, they disagreed on 99 of 365 refused rows.

    The threshold is read through `adaptive_threshold.peek`, not `gate`: `gate`
    trains the EMA, and a backlog of near-zero scores would drag a live threshold
    down to the 0.1 floor until everything passed. See `peek`.

    `ids` narrows the run to exactly those journal rows, ignoring both the window
    and the status filter, AND is treated as a human override of the gate — the
    whole point of naming a row is "this one, despite the gate". The override is
    written into the decision as `gate_override`, so the record shows a person
    chose it rather than implying the gate admitted it.

    `max_rows` bounds a single run. A config change re-opens every row whose
    decisions carry the old config_hash — 564 refused rows on one live base — and
    without a bound the whole backlog would be distilled in one burst. Bounding it
    lets the backlog drain over successive nights.
    """
    from core import MemoryManager
    from graph.epistemic import EpistemicGraph
    from lifecycle.distiller import distill_and_route, score_text

    conn = await connection_manager.get(DB_NAME)
    cutoff = time.time() - since_days * 86400
    chash = config_hash()
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = await (
            await conn.execute(
                f"SELECT id, ts, layer, user_id, text, decisions FROM l0_journal WHERE id IN ({placeholders}) ORDER BY id",
                tuple(ids),
            )
        ).fetchall()
    else:
        rows = await (
            await conn.execute(
                "SELECT id, ts, layer, user_id, text, decisions FROM l0_journal"
                " WHERE ts > ? AND (status IN ('received', 'gated_out') OR (status='processing' AND processed_at < ?)) ORDER BY id",
                (cutoff, time.time() - 600.0),
            )
        ).fetchall()

    # An explicit id list is a human decision to wake that row; a window run is
    # automation and obeys the gate. Callers may override either way.
    if respect_gate is None:
        respect_gate = not ids
    if max_rows is None:
        max_rows = int(config.get("l0", "replay_max_rows", default=200))
    if max_rows > 0:
        rows = rows[:max_rows]

    processed = skipped = conflicts = 0
    gated = 0
    for row in rows:
        decisions: list[dict[str, Any]] = json.loads(row["decisions"] or "[]")
        if any(d.get("gate") == gate and d.get("config_hash") == chash for d in decisions):
            skipped += 1
            continue
        # S6a-1 single entry: rows with skip_distill (remember/think) were
        # already written addressably by their own entry — replay does not distill them.
        if any(d.get("skip_distill") for d in decisions):
            await conn.execute("UPDATE l0_journal SET status='routed_direct', processed_at=? WHERE id=?", (time.time(), row["id"]))
            skipped += 1
            continue
        # Audit 05.09 (P1) replay race: claim by status — a second concurrent
        # replay sees the status already 'processing' with a fresh processed_at
        # and skips the row; a stuck 'processing' (grace 10m) is re-processed.
        # aiosqlite rowcount after UPDATE is unreliable → check-then-update.
        recheck = await (await conn.execute("SELECT status, processed_at FROM l0_journal WHERE id=?", (row["id"],))).fetchone()
        if recheck is None:
            skipped += 1
            continue
        if recheck[0] == "processing" and float(recheck[1] or 0) > time.time() - 600.0:
            skipped += 1
            continue
        await conn.execute("UPDATE l0_journal SET status='processing', processed_at=? WHERE id=?", (time.time(), row["id"]))
        await conn.commit()  # the claim is fixed before distillation

        if respect_gate:
            # Judged by the gate's own measure, not by what the distiller is fed.
            # `peek` reads the threshold without training the EMA (see docstring).
            from features.importance import evaluate_importance
            from features.rules import apply_rules
            from shared.adaptive import adaptive_threshold

            gate_score = evaluate_importance(row["text"])
            rules_out = apply_rules(row["text"])
            if rules_out["importance_boost"]:
                gate_score = min(1.0, gate_score + rules_out["importance_boost"])
            verdict = await adaptive_threshold.peek(gate_score)
            if verdict["bypass"]:
                decisions.append(
                    {
                        "gate": gate,
                        "config_hash": chash,
                        "ts": time.time(),
                        "reason": "importance_gate_bypass",
                        "importance": verdict["importance"],
                        "threshold": verdict["threshold"],
                    }
                )
                await conn.execute(
                    "UPDATE l0_journal SET status='gated_out', processed_at=?, decisions=? WHERE id=?",
                    (time.time(), json.dumps(decisions, ensure_ascii=False), row["id"]),
                )
                gated += 1
                continue

        mem = MemoryManager(cm=connection_manager).get_layer(row["layer"] or "user", row["user_id"])
        graph = EpistemicGraph(cm=connection_manager, layer=row["layer"] or "user")
        # ts from the journal, not now: a replay is BY DEFINITION distilling text
        # that arrived earlier, and `created_at` defaults to insertion time. Without
        # this, one replay re-dated three weeks of history to the hour it ran —
        # measured: 1937 agent-layer episodes spanning 15.09..04.10, all reading
        # 04.10 09:15..13:42. The journal's ts is the original capture time, which
        # `l0_import_source.py` and `import_chat.py` carry through as `ts_override`.
        route = await distill_and_route(
            mem,
            graph,
            row["user_id"],
            row["text"],
            score_text(row["text"], event=gate),
            event=gate,
            source_rid=int(row["id"]),
            ts=row["ts"],
        )
        conflicts += route["conflicts"]
        # C8: novelty_skipped = the fact is already in L4 (a re-run of the same row) —
        # that is an idempotent success, not gated_out.
        new_status = "promoted_l4" if (route["l4_saved"] or route.get("novelty_skipped")) else ("saved_l3" if route["l3_saved"] else "gated_out")
        decision: dict[str, Any] = {"gate": gate, "config_hash": chash, "ts": time.time()}
        if not respect_gate:
            # Record the human override rather than letting the log imply the gate
            # admitted this row.
            decision["gate_override"] = True
        decisions.append(decision)
        await conn.execute(
            "UPDATE l0_journal SET status=?, processed_at=?, decisions=? WHERE id=?",
            (new_status, time.time(), json.dumps(decisions, ensure_ascii=False), row["id"]),
        )
        processed += 1
    await conn.commit()
    return {"processed": processed, "skipped": skipped, "conflicts": conflicts, "gated": gated, "limit": max_rows}
