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
decisions JSON, and a row is skipped only when the CURRENT (gate, config_hash)
pair is already recorded for it — the same configuration already judged it.
A row refused under an older configuration, or closed as an unaccounted exit
(which records no pair: the write never happened), still awaits a verdict and
is claimed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from config import config
from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import get_write_dispatch

logger = logging.getLogger(__name__)

#: Statuses that mean the CONTENT was written (or deliberately routed): replay
#: must never claim one again, or it would duplicate what memory already holds.
#: `parked` is deliberately absent — it is out of scope by the window SELECT,
#: and naming it by id is how an import is woken.
#:
#: `gated_out` is NOT here. A refusal is a verdict only under the configuration
#: that made it, so `gated_out` is refused by `_claim` when its decisions carry
#: the current (gate, config_hash) pair and claimed otherwise. That one rule
#: covers both the changed-config re-open (a fixed distiller must be able to
#: reconsider what the old one refused) and the `unaccounted_exit` close (no
#: pair, so the write never happened and the row still needs rescuing —
#: load-bearing, because `classify_received` closes every evidence-less
#: `received` row exactly that way in the same pass).
WRITTEN_STATUSES = frozenset({"promoted_l4", "saved_l3", "routed_direct", "foreign_agent"})


def _judged_by(decisions_raw: Any, gate: str, chash: str) -> bool:
    """Report whether `decisions` already record a verdict for this (gate, config).

    This is the ONE rule that decides whether a row still awaits a verdict. A
    pair under an older config is not a verdict: the configuration that made it
    no longer exists, so the row is re-judgeable. A malformed blob proves
    nothing and is treated as no verdict.
    """
    try:
        decisions = json.loads(decisions_raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(decisions, list) and any(isinstance(d, dict) and d.get("gate") == gate and d.get("config_hash") == chash for d in decisions)


async def record_recall_useful(cm: Any, layer: str, user_id: str, entries: list[tuple[int, str]]) -> int:
    """Record one recall_useful row per (entry_id, key). Returns rows written."""
    if not entries:
        return 0
    shared = await cm.get(DB_NAME)
    if shared.in_transaction:
        # A best-effort counter: a foreign transaction holds the write lock, and
        # riding it is not an option on a dedicated connection.
        return 0
    now = time.time()
    await get_write_dispatch(cm).write_many(
        DB_NAME,
        [
            (
                ("INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?, 'recall_useful', 'core_memory', ?, ?, ?)"),
                (user_id, str(entry_id), json.dumps({"key": key}), now),
            )
            for entry_id, key in entries
        ],
    )
    return len(entries)


async def cls_replay(cm: Any, user_id: str, layer: str = "user", window_hours: int = 24, boost: float = 0.05) -> dict[str, int]:
    """Boost L4 facts recalled within the window. Returns counters."""
    shared = await cm.get(DB_NAME)
    if shared.in_transaction:
        return {"boosted": 0}
    cutoff = time.time() - window_hours * 3600
    now = time.time()

    async def _txn(conn: Any) -> int:
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
        return boosted

    return {"boosted": await get_write_dispatch(cm).atomic(DB_NAME, _txn)}


def config_hash() -> str:
    """Hash of the gate CONFIGURATION that determines G1 routing decisions.

    Covers the static fallback (hooks.auto_save_threshold) and the rules.yaml
    content (D1.9 boosts/tags). Replay skips rows already processed under the
    same hash; a changed hash re-opens the window.

    RUNTIME STATE MUST NOT ENTER THIS KEY. It used to hash the EMA threshold
    (`adaptive_threshold._current_value`), which `gate()` rewrites on every
    message it judges: measured live, that produced 2295 distinct hashes over
    2319 rows, so the "already processed under this config" skip almost never
    fired and the idempotency mechanism was effectively dead. A key is a pure
    function of what an operator configured, not of what the pipeline has seen.
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
    from shared.l0 import STATUS_RECEIVED, classify_received

    dispatch = get_write_dispatch()
    cutoff = time.time() - since_days * 86400
    chash = config_hash()
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = await dispatch.query(
            DB_NAME,
            f"SELECT id, ts, layer, user_id, text, status, decisions FROM l0_journal WHERE id IN ({placeholders}) ORDER BY id",
            tuple(ids),
        )
    else:
        rows = await dispatch.query(
            DB_NAME,
            "SELECT id, ts, layer, user_id, text, status, decisions FROM l0_journal"
            " WHERE ts > ? AND (status IN ('received', 'gated_out') OR (status='processing' AND processed_at < ?)) ORDER BY id",
            (cutoff, time.time() - 600.0),
        )

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
        # Read-time heal (spec 2026-10-22, S5): classify BEFORE anything
        # judges the row. Case-b (memory already holds the text) must not
        # reach the distiller — it re-emits every clause under one raw:<rid>
        # tag (measured: raw:164 -> 120 rows across three days). Case-a
        # (nothing was written) is closed re-enterable right here and falls
        # through so THIS run claims and distills it. Fresh row or a broken
        # reader (verdict None) -> skip until the next night.
        if str(row["status"] or "") == STATUS_RECEIVED:
            try:
                heal_verdict = await classify_received(int(row["id"]))
            except Exception:
                heal_verdict = None  # classify's contract is never-raise; keep replay's own guarantee local
            if heal_verdict is None or heal_verdict in ("saved_l3", "promoted_l4"):
                skipped += 1
                continue
        decisions: list[dict[str, Any]] = json.loads(row["decisions"] or "[]")
        if _judged_by(row["decisions"], gate, chash):
            skipped += 1
            continue
        # S6a-1 single entry: rows with skip_distill (remember/think) were
        # already written addressably by their own entry — replay does not distill them.
        if any(d.get("skip_distill") for d in decisions):
            await dispatch.write(DB_NAME, "UPDATE l0_journal SET status='routed_direct', processed_at=? WHERE id=?", (time.time(), row["id"]))
            skipped += 1
            continue

        # Audit 05.09 (P1) replay race: claim by status — a second concurrent
        # replay sees the status already 'processing' with a fresh processed_at
        # and skips the row; a stuck 'processing' (grace 10m) is re-processed.
        # F4: a row is claimed only when it still AWAITS a verdict — its content
        # was not written, and no verdict under the CURRENT config is recorded.
        # The read and the write share one BEGIN IMMEDIATE, and the write is a
        # CAS on the status just read, so two claimers cannot both win. The
        # decisions are re-read under that lock: the loop's snapshot was taken
        # before the run started and another writer may have closed the row
        # since — that stale snapshot is the race F4 named.
        #
        # The guard is `in_transaction`, not a swallowed exception: swallowing a
        # BEGIN failure (SQLITE_BUSY) would run the re-read outside any lock, and
        # the CAS would then match a row another writer had already claimed.
        async def _claim(conn: Any, gate_name: str, chash_now: str) -> bool:
            if not conn.in_transaction:
                await conn.execute("BEGIN IMMEDIATE")
            recheck = await (await conn.execute("SELECT status, processed_at, decisions FROM l0_journal WHERE id=?", (row["id"],))).fetchone()
            if recheck is None:
                return False
            current = str(recheck[0] or "")
            if current in WRITTEN_STATUSES:
                return False
            if current == "gated_out" and _judged_by(recheck[2], gate_name, chash_now):
                return False
            if current == "processing" and float(recheck[1] or 0) > time.time() - 600.0:
                return False
            cur = await conn.execute(
                "UPDATE l0_journal SET status='processing', processed_at=? WHERE id=? AND status=?",
                (time.time(), row["id"], current),
            )
            return int(cur.rowcount or 0) == 1

        # The claim is committed before distillation, so the next tick sees it.
        #
        # A claim that cannot take the write lock is ONE row's problem, not the
        # pass's: it must be skipped and visible, never propagate. The nightly
        # caller wraps `replay()` in a bare `contextlib.suppress(Exception)` with
        # no log, so a BUSY escaping from here would abandon the whole night's
        # replay silently — the exact silence class this package removes. Only
        # the claim is contained: a failure while DISTILLING is a different,
        # louder problem and stays the caller's business.
        try:
            claimed = await dispatch.atomic(DB_NAME, lambda conn: _claim(conn, gate, chash))
        except sqlite3.OperationalError as exc:
            # Only a BUSY means "another writer holds the lock, try later". The
            # same exception class carries permanent failures — a malformed image,
            # a corrupt file — and skipping one of those silently would hide a
            # broken base behind a warning per row. Anything that is not busy is
            # re-raised: it is the caller's business, and it must be loud.
            from shared.sqlite_busy import is_busy

            if not is_busy(exc):
                raise
            logger.warning("replay: claim of l0 row %s hit %s — left for the next pass", row["id"], exc)
            skipped += 1
            continue
        if not claimed:
            skipped += 1
            continue

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
                await dispatch.write(
                    DB_NAME,
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
        #
        # F1 defence in depth: rows captured before the door sanitized its input
        # are still raw in the journal, and the replay is the last step before
        # they become memory. The gate above was judged on the raw text on
        # purpose — masking must not change admission — but what reaches the
        # distiller (and therefore episodes and L4) is the masked text.
        from mcp_server.utils.privacy import sanitize

        stored_text, _priv_map = sanitize(row["text"])
        route = await distill_and_route(
            mem,
            graph,
            row["user_id"],
            stored_text,
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
        await dispatch.write(
            DB_NAME,
            "UPDATE l0_journal SET status=?, processed_at=?, decisions=? WHERE id=?",
            (new_status, time.time(), json.dumps(decisions, ensure_ascii=False), row["id"]),
        )
        processed += 1
    return {"processed": processed, "skipped": skipped, "conflicts": conflicts, "gated": gated, "limit": max_rows}
