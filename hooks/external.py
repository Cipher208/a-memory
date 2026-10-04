"""External event dispatcher — one entry point, two transports (HTTP + MCP tool).

Harnesses (Hermes/MiMoCode/CowAgent) push lifecycle events; ariel-side handlers
do the in-server work. Isolation is inherited: each agent runs its own ariel
instance (own process + MCP_MEMORY_DATA_DIR).

Takes PRE-RESOLVED mem/graph/rag from the calling transport — this module must
not import mcp_server (that recreates the base → context import cycle mypy
chokes on). The HTTP endpoint and the memory_hook tool do the resolution.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
    # 'promoted_l4'. Neither of the two write paths fires → stays 'received'.
    if l0_id is not None:
        new_status = "promoted_l4" if route_stats["l4_saved"] else "saved_l3"
        try:
            conn = await connection_manager.get("memory.db")
            await conn.execute("UPDATE l0_journal SET status=?, processed_at=? WHERE id=?", (new_status, _time.time(), l0_id))
            await conn.commit()
        except Exception as _e:
            logger.debug("l0_journal watermark update failed: %s", _e)
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
