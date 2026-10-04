"""External event dispatcher — one entry point, two transports (HTTP + MCP tool).

Harnesses (Hermes/MiMoCode/CowAgent) push lifecycle events; ariel-side handlers
do the in-server work. Isolation is inherited: each agent runs its own ariel
instance (own process + MCP_MEMORY_DATA_DIR).

Takes PRE-RESOLVED mem/graph/rag from the calling transport — this module must
not import mcp_server (that recreates the base → context import cycle mypy
chokes on). The HTTP endpoint and the memory_hook tool do the resolution.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _close_l0_row(l0_id: int, status: str, *, reason: str | None = None) -> None:
    """Stamp the L0 watermark for one captured row, recording WHY it closed.

    THE DEFECT THIS FIXES. `auto_save_text` captured the row, then returned early
    whenever the adaptive importance gate said `bypass` — before the watermark
    block, which is further down. Nothing ever wrote a status, so the row stayed
    `received` for good. Measured on one live base: 348 such rows, and 161 of them
    had EMPTY `decisions`, i.e. the pipeline had never touched them at all. They
    were not queued for anything: 'received' is the status `l0_tiers` promises it
    will NEVER tier or archive, so they accumulated forever with no reader.

    WHY `reason` MATTERS AS MUCH AS THE STATUS. A closed row is only honest if a
    later `replay` can tell "deliberately closed" from "never processed". Replay
    skips rows whose `decisions` already record the current (gate, config_hash)
    pair, so the same pair is written here. The effect is exactly right: replay
    under an unchanged config leaves the row alone, and replay after the config
    changed re-opens it — which is the whole point of `config_hash`, and the
    reason a fixed distiller can reconsider rows a broken one refused.

    `gate` is recorded as "g1" to match every `gated_out` row already in the live
    bases and the default of `l0_cli.py replay --gate`. The true cause is kept in
    `reason`, because the importance gate is what refused these, not the distiller.

    WHY THE IMPORTS ARE INSIDE. `connection_manager` and `_time` are imported
    inside `auto_save_text` (lines 217/221), not at module level, so a module-level
    helper cannot see them. The first version of this function referenced them
    anyway; the resulting `NameError` was swallowed by the `except` below and the
    watermark silently did nothing at all — which is precisely the failure mode
    this function exists to remove. The tests caught it; keep the imports here.
    """
    import time as _time

    from shared.connection import connection_manager

    try:
        conn = await connection_manager.get("memory.db")
        now = _time.time()
        if reason is None:
            await conn.execute("UPDATE l0_journal SET status=?, processed_at=? WHERE id=?", (status, now, l0_id))
        else:
            from features.replay import config_hash

            row = await (await conn.execute("SELECT decisions FROM l0_journal WHERE id=?", (l0_id,))).fetchone()
            try:
                decisions = json.loads((row["decisions"] if row else None) or "[]")
            except (json.JSONDecodeError, TypeError):
                decisions = []
            decisions.append({"gate": "g1", "config_hash": config_hash(), "ts": now, "reason": reason})
            await conn.execute(
                "UPDATE l0_journal SET status=?, processed_at=?, decisions=? WHERE id=?",
                (status, now, json.dumps(decisions, ensure_ascii=False), l0_id),
            )
        await conn.commit()
    except Exception as _e:
        logger.debug("l0_journal watermark update failed: %s", _e)


def _staging_enabled() -> bool:
    from config import config

    return bool(config.get("staging", "enabled", default=True))


def _dream_markers_enabled() -> bool:
    from config import config

    return bool(config.get("staging", "dream_markers", default=True))


KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        "session_started",
        "session_ended",
        "new_message",
        "auto_save_candidate",
        "context_threshold",
        "memory_pressure",
        "post_context_compression",
        "post_session_diff",
        "on_turn_end",
        # MUSE §7.5 integration surface (muse-engine-spec v1.1): state
        # lifecycle → L3 episodes tagged muse_state. L4 facts about a state
        # being useful stay the agent's deliberate think() call.
        "state_entered",
        "state_exited",
    }
)


async def dispatch_event(
    event: str,
    layer: str,
    user_id: str,
    payload: dict[str, Any],
    mem: Any,
    graph: Any,
    rag: Any = None,
) -> dict[str, Any]:
    """Validate + fire one external event. Raises ValueError on unknown event.

    The input is protected by the middleware pipeline (S13): rate-limit/dedup/
    audit on external events — the only path without the tool layer's own defenses.
    """
    if event not in KNOWN_EVENTS:
        raise ValueError(f"unknown event: {event!r}. Must be one of {sorted(KNOWN_EVENTS)}")
    from shared.metrics import metrics

    metrics.inc(f"hook_event_{event}")
    layer = (layer or "user").strip().lower()
    if layer not in ("user", "agent"):
        raise ValueError(f"invalid layer: {layer!r}")
    context: dict[str, Any] = {"user_id": user_id, "_rag": rag, **payload}

    from shared.middleware import MiddlewareContext, default_pipeline

    mw_ctx = MiddlewareContext(tool_name=event, user_id=user_id or "default", args={"layer": layer, **payload})

    async def _fire(c: MiddlewareContext) -> dict[str, Any]:
        from hooks.registry import hook_registry

        return await hook_registry.fire(event, layer, context, mem=mem, graph=graph)

    result = await default_pipeline.execute(mw_ctx, _fire)
    if mw_ctx.blocked:
        # The pipeline runs AHEAD of the handler, so a block here means nothing
        # downstream ever saw the event: no L0 row (the door is inside the
        # handler), no dispatch-log row, no operator-visible warning. That is
        # where the fifth bug lived — 2608 rate-limit blocks, 2590 in one replay,
        # each one a message deleted from a source that cannot be re-read from
        # the middle. The daemon now reads the verdict by value and holds the
        # cursor; this writes it down so an operator can see the rate instead of
        # reconstructing it after the fact.
        from shared.door_log import record_rejection

        await record_rejection(
            mw_ctx.block_reason,
            layer=layer,
            user_id=user_id or "default",
            event=event,
            source_msg_id=payload.get("source_msg_id"),
            text=str(payload.get("text") or ""),
        )
        return {"skipped": True, "reason": mw_ctx.block_reason}
    return dict(result) if isinstance(result, dict) else {"results": result}


_preview_column_ensured: set[str] = set()


def _ensure_preview_column(db_path: Any) -> None:
    """PRAGMA-first migration: text_preview on memory_dispatch_log (idempotent).

    Written at dispatch time because compute_session_gaps can never recover
    the text retroactively (source_msg_id is a harness conversation id, not
    an L3 episode id — the old lookup produced 13k empty-preview markers).
    Cached PER PATH: one process may touch several bases (tests, multi-agent
    CLI runs); a global bool would silently skip the column on base #2.
    """
    import sqlite3

    key = str(db_path)
    if key in _preview_column_ensured:
        return
    try:
        with sqlite3.connect(key) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_dispatch_log)")}
            if cols and "text_preview" not in cols:
                conn.execute("ALTER TABLE memory_dispatch_log ADD COLUMN text_preview TEXT NOT NULL DEFAULT ''")
                conn.commit()
        _preview_column_ensured.add(key)
    except Exception as _e:
        logger.debug("text_preview column migration failed: %s", _e)


async def _close_unaccounted(tracker: dict[str, Any]) -> None:
    """Close a row the capture path captured but never stamped. Never raises.

    `_auto_save_text_body` names the row it wrote into `tracker`; this checks
    afterwards that the row reached a terminal status. Reaching this function with
    the row still `received` means the body returned — or raised — without giving it
    one, which is the exact defect that stranded 353 rows on a live base, the oldest
    from 26.07, with no reader and no expiry.

    Three early returns were found and fixed by hand (gate bypass, empty route,
    DREAM marker). Listing exits does not scale: it repairs the branches someone
    already wrote, not the one added next month. This is the part that survives the
    next branch.

    `gated_out` is the honest status — nothing was saved — and `unaccounted_exit`
    names the bug instead of dressing it as a plausible verdict. WARNING, not debug:
    this should never fire, and a silent one is how the original went unnoticed for
    months.
    """
    l0_id = tracker.get("l0_id")
    if not l0_id:
        return
    try:
        from shared.connection import connection_manager

        conn = await connection_manager.get("memory.db")
        row = await (await conn.execute("SELECT status FROM l0_journal WHERE id=?", (l0_id,))).fetchone()
        if row is None or row[0] != "received":
            return
        logger.warning("auto_save_text left l0 row %s unaccounted for — closing it", l0_id)
        await _close_l0_row(int(l0_id), "gated_out", reason="unaccounted_exit")
    except Exception as _e:
        logger.debug("unaccounted-exit close failed: %s", _e)


async def auto_save_text(
    mem: Any,
    graph: Any,
    user_id: str,
    text: str,
    *,
    event: str = "new_message",
    source_msg_id: int | None = None,
    role: str = "",
    persona_owner: bool = False,
    kind: str = "",
    ts: float | None = None,
) -> dict[str, Any]:
    """Public entry point — runs the body, then proves the L0 row was not left behind.

    The contract lives in `_auto_save_text_body` below. This wrapper exists only
    because that contract was broken three separate times by three separate early
    returns, each one stranding its journal row. The exits are listed and fixed, but
    the wrapper is what makes the guarantee independent of that list.
    """
    tracker: dict[str, Any] = {}
    try:
        return await _auto_save_text_body(
            mem,
            graph,
            user_id,
            text,
            event=event,
            source_msg_id=source_msg_id,
            role=role,
            persona_owner=persona_owner,
            kind=kind,
            ts=ts,
            tracker=tracker,
        )
    finally:
        # `finally`, not the normal path: an exception mid-pipeline strands the row
        # exactly as an early return does.
        await _close_unaccounted(tracker)


async def _auto_save_text_body(
    mem: Any,
    graph: Any,
    user_id: str,
    text: str,
    *,
    event: str = "new_message",
    source_msg_id: int | None = None,
    role: str = "",
    persona_owner: bool = False,
    kind: str = "",
    ts: float | None = None,
    tracker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """evaluate_importance → threshold-gated saves + one memory_dispatch_log row.

    score >= EMA threshold (S17: adaptive_threshold, F2 — "EMA + rules") →
    L3 episodic + graph node; score >= 0.8 → also L4 core. Never raises past
    the caller (fire catches).

    The log row is the C1.10 substrate for compute_session_gaps and the
    memory_watch tool's hits_24h counter. `event` is the high-level lifecycle
    event that triggered the save (always "new_message" or "auto_save_candidate"
    in v1; the dispatcher calls auto_save_text with that name so the log
    carries the same tag as metrics.inc("hook_event_<event>")).

    ts: when the SOURCE message was written, when that is not now. The daemon
    has always put it in the dispatch payload (`daemon.py`, `"ts": msg.ts`) —
    and this function had no parameter to receive it, so `capture()` fell back
    to `time.time()` (`shared/l0.py`) and the journal, and every episode built
    from it, were dated by the backfill instead of by the message. Three
    historical layers collapsed into one afternoon that way. None means "now",
    so the live path is unchanged.
    """
    import time as _time
    import sqlite3 as _sqlite3

    from features.importance import evaluate_importance
    from shared.connection import connection_manager

    # Transcript guard: raw harness dumps (JSON message blocks, tool_result
    # blobs, hook echoes) are not memories — prod once filled 52% of the
    # episodes table with them. Narrow variant: no newline rule (legit saved
    # messages wrap freely); only structural heads and tool markers.
    from lifecycle.consolidation import (
        _looks_like_dump,
        _looks_like_harness_limit,
        _looks_like_system_injection,
    )

    # S10 speaker axis has to reach the journal as well, and this is not
    # bookkeeping. `layer` is an INPUT TO THE L0 CONTENT HASH
    # (`shared/l0.py:_content_hash`), and the replay guard below asks
    # `find_block` the same question. Hard-coding "user" here therefore cost
    # more than a mislabelled row: a persona's declaration that repeated
    # anything the owner had already said hashed identically to the owner's
    # block, `find_block` answered "already captured", and the declaration was
    # dropped as `duplicate_l0_block` — BEFORE the persona-canon branch below
    # could promote it to L4. Her canon was silently thrown away for agreeing
    # with the woman she was agreeing with.
    #
    # It is computed HERE, above the guards, because the layer belongs to the
    # message and not to the capture step: a refusal has to be recorded against
    # the same layer the save would have used, or the log splits one message's
    # history across two names.
    l0_layer = "agent" if persona_owner and role == "assistant" else "user"

    async def _refuse(reason: str) -> dict[str, Any]:
        """Return the refusal AND write it down.

        Every guard below used to return without a trace. The door runs before
        L0 capture and before the dispatch-log insert, so a refused message
        existed nowhere at all — not in `l0_journal`, not in
        `memory_dispatch_log`, not in a warning. The sixth bug hid in exactly
        that silence: the markdown class in `_TRANSCRIPT_HEAD` discarded 591 of
        one persona's 734 substantive messages and the loss was findable only by
        re-deriving the number from the source database by hand.

        Returning and recording are one function on purpose. Six bugs in this
        repository came from one value being written in two places and diverging;
        the refusal verdict and its record must not be the seventh.
        """
        from shared.door_log import record_rejection

        await record_rejection(
            reason,
            layer=l0_layer,
            user_id=user_id,
            event=event,
            source_msg_id=source_msg_id,
            text=text,
        )
        return {"score": 0.0, "saved_l3": False, "saved_l4": False, "saved_graph": False, "skipped": reason}

    if _looks_like_dump(text):
        return await _refuse("transcript")

    # Harness budget chatter is the runtime talking to itself, not memory.
    if _looks_like_harness_limit(text):
        return await _refuse("harness_limit")

    # A1: system-injected boilerplate (skill bodies, cron preambles) is not user
    # memory — cut it at the input so it never poisons L3 recall.
    if _looks_like_system_injection(text):
        return await _refuse("system_injection")

    # L0 intake (F): append-only raw journal BEFORE sanitize — the journal is
    # the raw door of the pipeline. Best-effort (capture never raises); the id
    # drives the status watermark after distillation.
    from shared.l0 import capture, find_block

    # A replay of an already-captured block. capture() would return the original
    # rid and stop there — but the distiller ran unconditionally after it, so
    # every replay re-emitted the full clause set under the same `raw:<rid>` tag.
    # capture() guards the journal, not the pipeline; the pipeline guard belongs
    # here, before it.
    if await find_block(l0_layer, user_id, text) is not None:
        return await _refuse("duplicate_l0_block")

    l0_id: int | None = await capture(event, l0_layer, user_id, text, source_msg_id=source_msg_id, ts_override=ts)
    # Report the captured row to the public wrapper, which checks after this body
    # returns (or raises) that the row reached a terminal status. Only the wrapper
    # needs this: it is the one place that can see an exit nobody remembered to
    # handle. See `_close_unaccounted`.
    if tracker is not None and l0_id is not None:
        tracker["l0_id"] = l0_id

    # G0 privacy: secrets/PII → typed placeholders (the reverse map is not persisted).
    # NER unavailable/crashed → the regex tier inside sanitize still ran.
    from mcp_server.utils.privacy import sanitize

    text, _priv_map = sanitize(text)

    score = evaluate_importance(text)
    result: dict[str, Any] = {"score": score, "saved_l3": False, "saved_l4": False, "saved_graph": False}

    # S10+S4: persona-owner declared canon — origin certifies content, so it
    # skips the G1 EMA and the distiller gates straight to L4 at the 0.8
    # floor. Rate-limited with the same shared window as think(kind).
    if persona_owner and kind and role == "assistant":
        from shared.canon_rate import canon_rate_ok
        from shared.memory_types import L4_DECLARABLE_KINDS, validate_kind

        if validate_kind(kind) and kind in L4_DECLARABLE_KINDS and canon_rate_ok(user_id):
            import hashlib as _hl

            key = f"canon:{kind}:{_hl.sha1(text.encode('utf-8')).hexdigest()[:12]}"
            await mem.remember(key, text[:2000], max(score, 0.8), memory_kind=kind)
            result["saved_l4"] = True
            result["canon"] = "persona_owner"
            if l0_id is not None:
                try:
                    _conn_w = await connection_manager.get("memory.db")
                    await _conn_w.execute("UPDATE l0_journal SET status=?, processed_at=? WHERE id=?", ("promoted_l4", _time.time(), l0_id))
                    await _conn_w.commit()
                except Exception as _e:
                    logger.debug("l0 watermark failed (persona): %s", _e)
            try:
                db_path = connection_manager.base_dir / "memory.db"
                _ensure_preview_column(db_path)
                with _sqlite3.connect(str(db_path)) as _conn:
                    _conn.execute(
                        "INSERT INTO memory_dispatch_log (event, source_msg_id, layer, user_id, score, saved_l3, saved_l4, saved_graph, text_preview, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (event, source_msg_id, "agent", user_id, float(max(score, 0.8)), 0, 1, 0, text[:200], _time.time()),
                    )
                    _conn.commit()
            except Exception as _e:
                logger.debug("memory_dispatch_log insert failed (persona): %s", _e)
            return result

    # C1.12: DREAM: markers are durable signals — route through staging at 0.95.
    # Toggle: staging.dream_markers (default true) — disabled → plain heuristic path.
    from features.importance import detect_dream_marker

    marker = detect_dream_marker(text) if _dream_markers_enabled() else None
    if marker is not None:
        if _staging_enabled():
            try:
                from features.staging import propose

                await propose(
                    "dream",
                    "core_write",
                    user_id,
                    "user",
                    {
                        "key": f"dream_{marker['target']}_{int(_time.time())}",
                        "value": marker["content"],
                        "importance": 0.95,
                    },
                )
                if marker["target"] == "skill":
                    await mem.l3.save(user_id, marker["content"], 0.95, ["dream_skill"])
                result["dream"] = {"target": marker["target"], "staged": True}
            except Exception:
                logger.exception("dream marker staging failed — falling through to heuristics")
        else:
            await mem.remember(f"dream_{marker['target']}", marker["content"], 0.95)
            if marker["target"] == "skill":
                await mem.l3.save(user_id, marker["content"], 0.95, ["dream_skill"])
            result["dream"] = {"target": marker["target"], "staged": False}
        result["score"] = 0.95
        try:
            db_path = connection_manager.base_dir / "memory.db"
            _ensure_preview_column(db_path)
            with _sqlite3.connect(str(db_path)) as _conn:
                _conn.execute(
                    "INSERT INTO memory_dispatch_log (event, source_msg_id, layer, user_id, score, saved_l3, saved_l4, saved_graph, text_preview, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event, source_msg_id, "user", user_id, 0.95, 0, 0, 0, text[:200], _time.time()),
                )
                _conn.commit()
        except Exception as _e:
            logger.debug("memory_dispatch_log insert failed: %s", _e)
        # Second early return, same defect class as the gate above: the marker was
        # routed by its own protocol, but the journal row was never stamped, so it
        # stayed `received` forever. `routed_direct` is the right terminal status —
        # it is exactly what `replay` uses for rows whose own entry already wrote
        # them, and replay excludes it from its window, so the marker text will not
        # be re-atomized into episodes. No live row has ever carried a DREAM marker
        # (checked: 0 rows across three bases), so this is latent, not observed.
        if l0_id is not None:
            await _close_l0_row(l0_id, "routed_direct")
        return result

    # S17 (F2): the EMA gate is back in auto_save — adaptive_threshold.gate reads
    # the threshold and feeds the EMA with the same contract as the importance_gate handlers.
    # D1.9 rules engine: declarative user rules adjust the write gate.
    from features.rules import apply_rules
    from shared.adaptive import adaptive_threshold

    rule_out = apply_rules(text)
    if rule_out["importance_boost"]:
        score = min(1.0, score + rule_out["importance_boost"])
        result["score"] = score
    if rule_out["matched"]:
        result["rules"] = rule_out["matched"]
    verdict = await adaptive_threshold.gate(score)
    if verdict["bypass"]:
        # The gate refused the message, so nothing will be written — close the row
        # as `gated_out` instead of leaving it `received` forever. See
        # `_close_l0_row`: this early return was the whole reason 348 rows sat
        # unprocessed on a live base, with no reader and no expiry.
        result["gated"] = "importance_gate"
        if l0_id is not None:
            await _close_l0_row(l0_id, "gated_out", reason="importance_gate_bypass")
        return result

    # G1 distiller: atomize → canonical key → kind-routing (invariants → L4,
    # events → L3 via mem.l3.save). The graph is not written directly — the miners fill it.
    from lifecycle.distiller import distill_and_route

    route_stats = await distill_and_route(mem, graph, user_id, text, score, event=event, extra_tags=rule_out["tags"], source_rid=l0_id, ts=ts)
    result["saved_l3"] = route_stats["l3_saved"] > 0
    result["saved_graph"] = route_stats["l3_saved"] > 0
    result["routes"] = route_stats
    if route_stats["l4_saved"] > 0:
        result["saved_l4"] = True
    # S17 A2-advisory: near-dup/conflict keys — the agent decides itself whether to rephrase.
    similar_to: list[str] = list(route_stats.get("similar_to") or [])
    if similar_to:
        result["similar_to"] = similar_to
    # L0 watermark (F): close the captured row — replay skips 'saved_l3'/
    # 'promoted_l4'. The status must be what the route ACTUALLY did: this used to
    # be `"promoted_l4" if route_stats["l4_saved"] else "saved_l3"`, so a message
    # that wrote nothing at all — a refused machine report, or one whose every
    # atom was below the length floor — was stamped `saved_l3` regardless, and
    # the journal then claimed a save that never happened. Same vocabulary as
    # `features/replay.py`, which already says `gated_out` for "nothing written".
    #
    # `gated_out` rather than leaving it `received`: the causes here are
    # deterministic (the same text produces the same empty route), so 'received'
    # would only invite replay to re-refuse the same row forever and multiply the
    # rejection count.
    if l0_id is not None:
        if route_stats["l4_saved"]:
            new_status = "promoted_l4"
        elif route_stats["l3_saved"]:
            new_status = "saved_l3"
        else:
            new_status = "gated_out"
        # `reason` only for the empty route: a save that DID happen needs no
        # explanation, and recording one would make `decisions` noisy. The empty
        # route is the case replay must be able to recognise as deliberate.
        await _close_l0_row(l0_id, new_status, reason=None if new_status != "gated_out" else "empty_route")
    # 2026-09-11: the score>=0.8 auto_save staging branch is REMOVED.
    # It staged raw chat text under the literal core key "auto_save" for
    # manual review — 52 same-key proposals flooded the review queue in a
    # week, none of them distinct decisions, and no code reads that key.
    # L4 routing stays with the distiller above (canonical keys, dedup,
    # conflict detection). Staging remains for deliberate mutations:
    # dream markers, consolidation promotions, agent-side propose, conflicts.

    # C1.10: one log row per save path. Best-effort — failure here never
    # blocks the save (the dispatcher catches), but a missing log row silently
    # disables memory_diff for this event.
    #
    # The layer is the one the routing chose, not a literal. This column exists
    # to be read (`memory_watch`, operator introspection), and it was written as
    # "user" on every path — so an agent-layer save was logged under the owner's
    # layer, and the row contradicted the persona branch above, which logs
    # "agent" for the very same message. No reader filters on this column today,
    # which is why it went unnoticed; the cost was a column that lies.
    try:
        db_path = connection_manager.base_dir / "memory.db"
        _ensure_preview_column(db_path)
        with _sqlite3.connect(str(db_path)) as _conn:
            _conn.execute(
                "INSERT INTO memory_dispatch_log (event, source_msg_id, layer, user_id, score, saved_l3, saved_l4, saved_graph, text_preview, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event,
                    source_msg_id,
                    l0_layer,
                    user_id,
                    score,
                    int(result["saved_l3"]),
                    int(result["saved_l4"]),
                    int(result["saved_graph"]),
                    text[:200],
                    _time.time(),
                ),
            )
            _conn.commit()
    except Exception as _e:
        logger.debug("memory_dispatch_log insert failed: %s", _e)

    return result
