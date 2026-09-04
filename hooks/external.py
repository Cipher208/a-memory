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
    """Validate + fire one external event. Raises ValueError on unknown event."""
    if event not in KNOWN_EVENTS:
        raise ValueError(f"unknown event: {event!r}. Must be one of {sorted(KNOWN_EVENTS)}")
    from shared.metrics import metrics

    metrics.inc(f"hook_event_{event}")
    layer = (layer or "user").strip().lower()
    if layer not in ("user", "agent"):
        raise ValueError(f"invalid layer: {layer!r}")
    context: dict[str, Any] = {"user_id": user_id, "_rag": rag, **payload}
    from hooks.registry import hook_registry

    return await hook_registry.fire(event, layer, context, mem=mem, graph=graph)


async def auto_save_text(
    mem: Any,
    graph: Any,
    user_id: str,
    text: str,
    *,
    event: str = "new_message",
    source_msg_id: int | None = None,
) -> dict[str, Any]:
    """evaluate_importance → threshold-gated saves + one memory_dispatch_log row.

    score >= hooks.auto_save_threshold (default 0.5) → L3 episodic + graph node;
    score >= 0.8 → also L4 core. Never raises past the caller (fire catches).

    The log row is the C1.10 substrate for compute_session_gaps and the
    memory_watch tool's hits_24h counter. `event` is the high-level lifecycle
    event that triggered the save (always "new_message" or "auto_save_candidate"
    in v1; the dispatcher calls auto_save_text with that name so the log
    carries the same tag as metrics.inc("hook_event_<event>")).
    """
    import time as _time
    import sqlite3 as _sqlite3

    from config import config
    from features.importance import evaluate_importance
    from shared.connection import connection_manager

    # Transcript guard: raw harness dumps (JSON message blocks, tool_result
    # blobs, hook echoes) are not memories — prod once filled 52% of the
    # episodes table with them. Narrow variant: no newline rule (legit saved
    # messages wrap freely); only structural heads and tool markers.
    from lifecycle.consolidation import _looks_like_dump

    if _looks_like_dump(text):
        return {"score": 0.0, "saved_l3": False, "saved_l4": False, "saved_graph": False, "skipped": "transcript"}

    # G0 privacy: secrets/PII → typed placeholders (reverse map не персистится).
    # NER недоступен/упал → regex-тир внутри sanitize всё равно отработал.
    from mcp_server.utils.privacy import sanitize

    text, _priv_map = sanitize(text)

    score = evaluate_importance(text)
    result: dict[str, Any] = {"score": score, "saved_l3": False, "saved_l4": False, "saved_graph": False}

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
            with _sqlite3.connect(str(db_path)) as _conn:
                _conn.execute(
                    "INSERT INTO memory_dispatch_log (event, source_msg_id, layer, user_id, score, saved_l3, saved_l4, saved_graph, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event, source_msg_id, "user", user_id, 0.95, 0, 0, 0, _time.time()),
                )
                _conn.commit()
        except Exception as _e:
            logger.debug("memory_dispatch_log insert failed: %s", _e)
        return result

    threshold = float(config.get("hooks", "auto_save_threshold", default=0.5))
    # D1.9 rules engine: declarative user rules adjust the write gate.
    from features.rules import apply_rules

    rule_out = apply_rules(text)
    if rule_out["importance_boost"]:
        score = min(1.0, score + rule_out["importance_boost"])
        result["score"] = score
    if rule_out["matched"]:
        result["rules"] = rule_out["matched"]
    if score < threshold:
        return result
    await mem.l3.save(user_id, text[:500], score, ["auto_save", *rule_out["tags"]])
    result["saved_l3"] = True
    await graph.add_node(user_id, text[:500], "fact", [], score)
    result["saved_graph"] = True
    if score >= 0.8:
        if _staging_enabled():
            try:
                from features.staging import propose

                await propose(
                    "auto_save",
                    "core_write",
                    user_id,
                    "user",
                    {"key": "auto_save", "value": text[:500], "importance": score},
                )
                result["staged_l4"] = True
            except Exception:
                # Bookkeeping must never lose the memory: if the proposals table
                # is missing (mis-migration), fall back to the direct write.
                logger.exception("staging propose failed — falling back to direct L4 write")
                await mem.remember("auto_save", text[:500], score)
                result["saved_l4"] = True
        else:
            await mem.remember("auto_save", text[:500], score)
            result["saved_l4"] = True

    # C1.10: one log row per save path. Best-effort — failure here never
    # blocks the save (the dispatcher catches), but a missing log row silently
    # disables memory_diff for this event.
    try:
        db_path = connection_manager.base_dir / "memory.db"
        with _sqlite3.connect(str(db_path)) as _conn:
            _conn.execute(
                "INSERT INTO memory_dispatch_log (event, source_msg_id, layer, user_id, score, saved_l3, saved_l4, saved_graph, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event,
                    source_msg_id,
                    "user",
                    user_id,
                    score,
                    int(result["saved_l3"]),
                    int(result["saved_l4"]),
                    int(result["saved_graph"]),
                    _time.time(),
                ),
            )
            _conn.commit()
    except Exception as _e:
        logger.debug("memory_dispatch_log insert failed: %s", _e)

    return result
